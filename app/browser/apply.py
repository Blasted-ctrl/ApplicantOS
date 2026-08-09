"""The apply driver — the one place a browser really submits an application.

Every submission-capable provider (Greenhouse, Lever, Ashby) ends up here. Each one calls
:func:`app.jobs._apply.run_browser_apply` with the name of its
:class:`~app.browser.selectors.SelectorPack`, that seam applies the kill switch and resolves
this module by name, and this function drives the attempt:

.. code-block:: text

    open → dismiss the consent banner → discover fields → fill → upload documents
         → submit (only past both switches) → VERIFY → map the verdict onto an ApplyResult

**Verification is the point of this module.** :class:`~app.browser.verification.ApplicationVerifier`
was fully built and had no caller, which meant ``docs/SAFETY.md``'s promise of proof of
submission was not actually kept: a click was being read as a submission. So the last step
here is not "we clicked" but "the page proved it", and the mapping is deliberately
asymmetric (golden rule #2):

* ``confirmed=True`` → :attr:`~app.models.enums.ApplicationStatus.CONFIRMED`, carrying the
  confirmation id, the employer's own confirmation copy and the evidence screenshot.
* an **error marker** → :attr:`~app.models.enums.ApplicationStatus.FAILED`, quoting what the
  page said, because the ATS explicitly rejected the submission.
* **inconclusive** → ``NEEDS_REVIEW`` with
  :attr:`~app.models.enums.ReviewReason.VERIFICATION_FAILED`. Silence is never read as
  success. A false positive writes ``CONFIRMED``, golden rule #1 then refuses to ever apply
  to that posting again, and a job the user wanted is lost with nothing in the product ever
  noticing. A false negative costs a human ten seconds.

**Verification runs only after a real click.** :meth:`~app.browser.autofill.AutoFiller.submit`
returns ``False`` without touching the DOM when either switch is closed, and this module keys
off :attr:`~app.browser.autofill.AutoFiller.submit_clicked` rather than off the return value —
so a dry run performs no verification, takes no evidence screenshot, and reports
:attr:`~app.models.enums.ReviewReason.POLICY_BLOCK` instead (golden rule #3).

**File inputs are reconciled here, not in the filler.**
:meth:`~app.browser.autofill.AutoFiller.fill` skips :attr:`~app.models.enums.FieldKind.FILE`
outright, so a required resume upload appears in *neither* list it returns
(``docs/OPEN_QUESTIONS.md`` section J). A driver that iterated only the needs-review list
would therefore submit an application with **no resume attached** — worse than not applying,
because the employer sees it and forms an impression. So this module reads the file fields off
:meth:`~app.browser.autofill.AutoFiller.discover_fields` itself, plans a document for each
(:func:`plan_documents`), uploads through :meth:`~app.browser.autofill.AutoFiller.upload` —
which proves the file attached rather than trusting ``set_input_files`` — and routes every
failure to :attr:`~app.models.enums.ReviewReason.FILE_UPLOAD_FAILED`.

Playwright is never imported at module scope: :class:`~app.browser.playwright_runner.BrowserSession`
imports it inside ``__aenter__``, and an installation without it raises
:class:`~app.jobs.base.UnsupportedFlowError` naming the URL a human can apply at. The session
is also injectable, which is what lets the whole flow be tested against a recording double
with no browser on the machine.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import structlog

from app.browser.autofill import (
    AutoFiller,
    FieldResolver,
    UploadFailedError,
    page_url,
)
from app.browser.selectors import GENERIC, SelectorPack, pack_for
from app.browser.verification import (
    METHOD_ERROR_MARKER,
    ApplicationVerifier,
    VerificationResult,
)
from app.jobs.base import (
    ApplyContext,
    ApplyResult,
    FormField,
    UnsupportedFlowError,
)
from app.models.enums import ApplicationStatus, FieldKind, ReviewReason

__all__ = [
    "COVER_LETTER_HINTS",
    "RESUME_HINTS",
    "SCREENSHOT_FORM_LOADED",
    "DocumentPlan",
    "plan_documents",
    "run_apply",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Artifact name for the capture taken once the form has loaded and before anything is
#: typed. The two captures §12 invariant 3 requires around the click itself are taken by
#: :meth:`~app.browser.autofill.AutoFiller.submit`; this one is what a human looks at when a
#: review item says "a required field could not be answered".
SCREENSHOT_FORM_LOADED: Final[str] = "form_loaded"

#: Label fragments that mark a file input as wanting the cover letter. Checked **before**
#: :data:`RESUME_HINTS` because "Cover letter (PDF or DOC, in place of a resume)" contains
#: both and the cover-letter reading is the correct one.
COVER_LETTER_HINTS: Final[tuple[str, ...]] = (
    "cover letter",
    "coverletter",
    "cover_letter",
    "covering letter",
    "motivation",
)

#: Label fragments that mark a file input as wanting the resume.
RESUME_HINTS: Final[tuple[str, ...]] = (
    "resume",
    "résumé",
    "cv",
    "curriculum vitae",
)

#: Reported on the ``browser_log`` entry for each step of the attempt, so an auditor can see
#: what happened without re-reading this file.
_STEP_NAVIGATE: Final[str] = "navigate"
_STEP_DISCOVER: Final[str] = "discover"
_STEP_FILL: Final[str] = "fill"
_STEP_UPLOAD: Final[str] = "upload"
_STEP_SUBMIT: Final[str] = "submit"
_STEP_VERIFY: Final[str] = "verify"

#: Characters of the ATS's own rejection copy kept in :attr:`~app.jobs.base.ApplyResult.error`.
#: Enough to identify the marker; not the whole page.
MAX_ERROR_DETAIL_CHARS: Final[int] = 300


# ======================================================================================
# Document planning
# ======================================================================================


class DocumentPlan:
    """Which document each file input on the form should receive.

    Attributes:
        uploads: ``(field, path)`` pairs that can be satisfied from the rendered documents.
        missing: Required file inputs with no document to give them. Every one of these is a
            :attr:`~app.models.enums.ReviewReason.FILE_UPLOAD_FAILED` escalation — submitting
            the form regardless would send an application with a document slot the employer
            asked for and did not get.
        skipped: Optional file inputs with no document, which are simply left empty.
    """

    __slots__ = ("missing", "skipped", "uploads")

    def __init__(
        self,
        uploads: list[tuple[FormField, Path]],
        missing: list[FormField],
        skipped: list[FormField],
    ) -> None:
        """Record the planned assignment.

        Args:
            uploads: ``(field, path)`` pairs to attach.
            missing: Required file inputs that cannot be satisfied.
            skipped: Optional file inputs that cannot be satisfied.
        """
        self.uploads = uploads
        self.missing = missing
        self.skipped = skipped

    def __repr__(self) -> str:
        """Return ``<DocumentPlan uploads=… missing=… skipped=…>`` for logs."""
        return (
            f"<DocumentPlan uploads={len(self.uploads)} missing={len(self.missing)} "
            f"skipped={len(self.skipped)}>"
        )


def _mentions(field: FormField, hints: Sequence[str]) -> bool:
    """Return whether a file input's own text names one of *hints*.

    Args:
        field: The discovered file input.
        hints: Lower-case fragments to look for.

    Returns:
        Whether the label, hint or selector contains any of them. The selector is included
        because React uploaders routinely render an unlabelled ``<input type="file">`` whose
        only clue is ``name="resume"``.
    """
    haystack = " ".join(
        part for part in (field.label, field.hint or "", field.selector) if part
    ).casefold()
    return any(hint in haystack for hint in hints)


def _existing(path: Path | None) -> Path | None:
    """Return *path* when it is a readable file, else ``None``.

    Args:
        path: A rendered document, or ``None`` when none was produced.

    Returns:
        The path, or ``None``. A path that no longer exists is treated exactly like a missing
        document: :meth:`~app.browser.autofill.AutoFiller.upload` would raise on it anyway,
        and finding out before the form is touched produces a clearer review item.
    """
    if path is None:
        return None
    candidate = Path(path)
    return candidate if candidate.is_file() else None


def plan_documents(fields: Sequence[FormField], ctx: ApplyContext) -> DocumentPlan:
    """Decide which rendered document belongs in which file input.

    Three passes, most specific first, so that a form with both slots fills both and a form
    with one unlabelled slot still gets the resume:

    1. inputs naming the cover letter take :attr:`~app.jobs.base.ApplyContext.cover_letter_path`;
    2. inputs naming a resume or CV take :attr:`~app.jobs.base.ApplyContext.resume_path`;
    3. any remaining input takes the resume **only if it has not already been placed** — a
       single unlabelled file input on a job application is the resume, but a second one is
       not, and attaching the resume twice tells the employer nothing while filling a slot
       they meant for something else.

    Args:
        fields: Every field discovery returned; non-file fields are ignored.
        ctx: The attempt's context, holding the rendered documents.

    Returns:
        The plan. A required input left unsatisfied lands in
        :attr:`DocumentPlan.missing` and is an escalation, never a reason to submit anyway.
    """
    resume = _existing(ctx.resume_path)
    cover = _existing(ctx.cover_letter_path)
    file_fields = [field for field in fields if field.kind is FieldKind.FILE]

    uploads: list[tuple[FormField, Path]] = []
    missing: list[FormField] = []
    skipped: list[FormField] = []
    unresolved: list[FormField] = []
    resume_placed = False

    for field in file_fields:
        if _mentions(field, COVER_LETTER_HINTS):
            if cover is not None:
                uploads.append((field, cover))
            elif field.required:
                missing.append(field)
            else:
                skipped.append(field)
            continue
        if _mentions(field, RESUME_HINTS):
            if resume is not None:
                uploads.append((field, resume))
                resume_placed = True
            elif field.required:
                missing.append(field)
            else:
                skipped.append(field)
            continue
        unresolved.append(field)

    for field in unresolved:
        if resume is not None and not resume_placed:
            uploads.append((field, resume))
            resume_placed = True
        elif field.required:
            missing.append(field)
        else:
            skipped.append(field)

    return DocumentPlan(uploads=uploads, missing=missing, skipped=skipped)


# ======================================================================================
# The driver
# ======================================================================================


class _Attempt:
    """One application attempt against one open browser session.

    Holds the mutable bookkeeping — the event log, the screenshots collected so far, the
    clock — so that :func:`run_apply` reads as the sequence of steps it is.

    Attributes:
        ctx: The attempt's context.
        session: The open browser session (``docs/CONTRACTS.md`` §12).
        pack: The provider's selector pack.
        events: The structured trace written to
            :attr:`~app.jobs.base.ApplyResult.browser_log`.
    """

    __slots__ = ("_screenshots", "_started", "ctx", "events", "pack", "session")

    def __init__(self, ctx: ApplyContext, session: Any, pack: SelectorPack) -> None:
        """Bind the attempt to a session and a pack.

        Args:
            ctx: Everything the attempt needs.
            session: The open browser session.
            pack: The provider's selector pack.
        """
        self.ctx = ctx
        self.session = session
        self.pack = pack
        self.events: list[dict[str, Any]] = []
        self._screenshots: list[Path] = []
        self._started = time.monotonic()

    # ----------------------------------------------------------------------------------
    # Bookkeeping
    # ----------------------------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        """Seconds since the attempt began."""
        return time.monotonic() - self._started

    def record(self, step: str, message: str, **detail: Any) -> None:
        """Append one entry to the browser log, mirroring it to the recorder.

        Args:
            step: One of the ``_STEP_*`` constants.
            message: One readable sentence.
            detail: Structured context, stored alongside the message.
        """
        entry: dict[str, Any] = {"step": step, "message": message, **detail}
        self.events.append(entry)
        recorder = self.ctx.recorder
        if recorder is not None and hasattr(recorder, "record_event"):
            recorder.record_event(step, message, dict(detail))

    async def capture(self, name: str) -> None:
        """Take a proof screenshot, and never let the capture end the attempt.

        Args:
            name: Artifact name.
        """
        take = getattr(self.session, "screenshot", None)
        if take is None:
            return
        try:
            path = await take(name)
        except Exception as exc:
            logger.warning(
                "apply.screenshot_failed",
                name=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        if isinstance(path, (str, Path)):
            self._screenshots.append(Path(path))

    def screenshots(self, filler: AutoFiller | None = None, *extra: Path | None) -> list[Path]:
        """Return every proof capture taken so far, in order and without duplicates.

        Args:
            filler: The autofiller, whose own captures (``before_submit`` / ``after_submit``)
                belong to the same attempt.
            extra: Further captures, such as the verifier's evidence screenshot.

        Returns:
            The paths. Order is chronological, which is the order a human reads them in.
        """
        collected: list[Path] = [*self._screenshots]
        if filler is not None:
            collected.extend(filler.screenshots)
        collected.extend(path for path in extra if path is not None)

        unique: list[Path] = []
        seen: set[str] = set()
        for path in collected:
            key = str(path)
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    # ----------------------------------------------------------------------------------
    # Result builders
    # ----------------------------------------------------------------------------------

    def review(
        self,
        reason: ReviewReason,
        error: str,
        *,
        filler: AutoFiller | None = None,
        fields: Sequence[FormField] | None = None,
        evidence: Path | None = None,
    ) -> ApplyResult:
        """Build a ``NEEDS_REVIEW`` result carrying everything a human needs.

        Args:
            reason: Why the attempt stopped.
            error: The sentence shown beside it.
            filler: The autofiller, for its screenshots.
            fields: The fields that could not be answered.
            evidence: A verification screenshot, when one was taken.

        Returns:
            The result.
        """
        logger.info(
            "apply.needs_review",
            reason=reason.value,
            error=error,
            pack=self.pack.name,
            **self.ctx.log_context(),
        )
        return ApplyResult.needs_review(
            reason,
            error=error,
            unanswered_fields=list(fields or ()),
            screenshot_paths=self.screenshots(filler, evidence),
            duration_seconds=self.elapsed,
            browser_log=self.events,
        )

    def failed(
        self,
        error: str,
        *,
        filler: AutoFiller | None = None,
        evidence: Path | None = None,
    ) -> ApplyResult:
        """Build a ``FAILED`` result.

        Args:
            error: What went wrong.
            filler: The autofiller, for its screenshots.
            evidence: A verification screenshot, when one was taken.

        Returns:
            The result.
        """
        logger.warning(
            "apply.failed", error=error, pack=self.pack.name, **self.ctx.log_context()
        )
        return ApplyResult.failed(
            error,
            screenshot_paths=self.screenshots(filler, evidence),
            duration_seconds=self.elapsed,
            browser_log=self.events,
        )

    def confirmed(self, verdict: VerificationResult, filler: AutoFiller) -> ApplyResult:
        """Build the one result that says an application was really submitted.

        Args:
            verdict: The verifier's proof.
            filler: The autofiller, for its screenshots.

        Returns:
            A result with ``ok=True`` and
            :attr:`~app.models.enums.ApplicationStatus.CONFIRMED`. Reached only from
            :attr:`~app.browser.verification.VerificationResult.confirmed`, never from the
            fact that a click happened.
        """
        logger.info(
            "apply.confirmed",
            method=verdict.method,
            has_confirmation_id=verdict.confirmation_id is not None,
            pack=self.pack.name,
            **self.ctx.log_context(),
        )
        return ApplyResult(
            ok=True,
            status=ApplicationStatus.CONFIRMED,
            confirmation_id=verdict.confirmation_id,
            confirmation_text=verdict.confirmation_text,
            screenshot_paths=self.screenshots(filler, verdict.evidence_screenshot),
            duration_seconds=self.elapsed,
            browser_log=self.events,
        )


def _blocked_message(ctx: ApplyContext, pack: SelectorPack) -> str:
    """Explain, for a human, why the submit control was never approached.

    Args:
        ctx: The attempt's context.
        pack: The provider's selector pack.

    Returns:
        A sentence naming the closed switch. Purely descriptive — the decision itself was
        made by :meth:`~app.browser.autofill.AutoFiller.submit`, which is the only place the
        kill switch is evaluated.
    """
    from app.config.settings import get_settings

    settings = get_settings()
    closed: list[str] = []
    if ctx.dry_run:
        closed.append("the caller asked for a dry run")
    if settings.dry_run:
        closed.append("settings.dry_run is on")
    if not settings.auto_apply_enabled:
        closed.append("settings.auto_apply_enabled is off")
    if not pack.supports_auto_apply:
        closed.append(f"{pack.name} may not be submitted automatically")
    detail = "; ".join(closed) or "automated submission is disabled"
    return (
        f"the form was filled and inspected but not submitted: {detail}. "
        "Nothing was sent to the employer."
    )


async def _dismiss_banner(session: Any, pack: SelectorPack) -> bool:
    """Dismiss the consent banner, if the session knows how.

    Args:
        session: The browser session.
        pack: The provider's selector pack, which names the banner control.

    Returns:
        Whether a banner was dismissed. A failure here is never fatal: a banner that will not
        close is a reason to carry on and find out whether the form is usable anyway.
    """
    dismiss = getattr(session, "dismiss_cookie_banner", None)
    if dismiss is None:
        return False
    try:
        return bool(await dismiss(pack))
    except Exception as exc:
        logger.debug(
            "apply.cookie_banner_failed", error=str(exc), error_type=type(exc).__name__
        )
        return False


async def _detect_blockers(session: Any, pack: SelectorPack) -> set[str]:
    """Ask the session what stands between us and the form.

    Args:
        session: The browser session.
        pack: The provider's selector pack, whose captcha markers sharpen the probe.

    Returns:
        The blocker names, or an empty set when the session cannot report them.
    """
    detector = getattr(session, "detect_blockers", None)
    if detector is None:
        return set()
    try:
        found = await detector(pack)
    except Exception as exc:
        logger.warning(
            "apply.blocker_detection_failed", error=str(exc), error_type=type(exc).__name__
        )
        return set()
    return {str(item) for item in (found or ()) if str(item).strip()}


def _build_resolver(ctx: ApplyContext) -> FieldResolver:
    """Build the field resolver for this applicant.

    Args:
        ctx: The attempt's context.

    Returns:
        A :class:`~app.browser.autofill.FieldResolver`. A model that cannot be resolved — no
        API key, no plugin loaded — yields ``llm=None``, which is a supported configuration:
        deterministic profile answers still fill most of the form and genuine free text goes
        to a human instead (golden rule #2).
    """
    llm: Any | None
    try:
        from app.ai.llm import get_llm

        llm = get_llm("fast")
    except Exception as exc:
        logger.info(
            "apply.model_unavailable",
            error=str(exc),
            error_type=type(exc).__name__,
            consequence="free-text questions will escalate to review",
        )
        llm = None
    return FieldResolver.for_user(ctx.user, ctx.answers, llm=llm)


async def _upload_documents(
    attempt: _Attempt, filler: AutoFiller, plan: DocumentPlan
) -> ApplyResult | None:
    """Attach every planned document, proving each one landed.

    Args:
        attempt: The running attempt.
        filler: The autofiller bound to the open form.
        plan: What :func:`plan_documents` decided.

    Returns:
        ``None`` when every required document attached, or a
        :attr:`~app.models.enums.ReviewReason.FILE_UPLOAD_FAILED` review result. The failure
        is never swallowed: a submitted application with no resume on it is worse than one
        that was never sent.
    """
    if plan.missing:
        labels = ", ".join(field.label or field.selector for field in plan.missing)
        attempt.record(
            _STEP_UPLOAD,
            "a required document is missing",
            fields=[field.selector for field in plan.missing],
        )
        return attempt.review(
            ReviewReason.FILE_UPLOAD_FAILED,
            f"the form requires a document this attempt does not have: {labels}",
            filler=filler,
            fields=plan.missing,
        )

    for field, document in plan.uploads:
        try:
            await filler.upload(field, document)
        except UploadFailedError as exc:
            attempt.record(
                _STEP_UPLOAD,
                "a document could not be attached",
                selector=field.selector,
                filename=document.name,
                error=str(exc),
            )
            return attempt.review(
                ReviewReason.FILE_UPLOAD_FAILED,
                f"{document.name} could not be attached to {field.label or field.selector}: {exc}",
                filler=filler,
                fields=[field],
            )
        attempt.record(
            _STEP_UPLOAD,
            f"attached {document.name}",
            selector=field.selector,
            label=field.label,
        )

    for field in plan.skipped:
        attempt.record(
            _STEP_UPLOAD,
            "no document for an optional upload; left empty",
            selector=field.selector,
            label=field.label,
        )
    return None


async def _drive(ctx: ApplyContext, session: Any, pack: SelectorPack) -> ApplyResult:
    """Run one attempt end to end against an already-open session.

    Args:
        ctx: The posting, the profile, the documents and the planned answers.
        session: An open browser session.
        pack: The provider's selector pack.

    Returns:
        The outcome. ``ok=True`` on exactly one path: the verifier proved the submission.
    """
    attempt = _Attempt(ctx, session, pack)
    url = ctx.posting.target_url

    await session.goto(url)
    form_url = page_url(session) or url
    attempt.record(_STEP_NAVIGATE, f"opened {form_url}", url=form_url, pack=pack.name)
    if await _dismiss_banner(session, pack):
        attempt.record(_STEP_NAVIGATE, "dismissed a consent banner")
    await attempt.capture(SCREENSHOT_FORM_LOADED)

    filler = AutoFiller(session, _build_resolver(ctx), pack=pack)
    fields = await filler.discover_fields()
    if not fields:
        blockers = await _detect_blockers(session, pack)
        reason = filler.review_reason_for((), blockers=blockers, essays=0)
        attempt.record(
            _STEP_DISCOVER, "no fields could be read", blockers=sorted(blockers) or None
        )
        return attempt.review(
            reason,
            "the application form could not be read; a human should open it",
            filler=filler,
        )

    essays = AutoFiller.count_essays(fields)
    attempt.record(
        _STEP_DISCOVER,
        f"discovered {len(fields)} fields",
        fields=len(fields),
        required=sum(1 for field in fields if field.required),
        essays=essays,
    )

    filled, unanswered = await filler.fill(fields)
    attempt.record(
        _STEP_FILL,
        f"filled {len(filled)} of {len(fields)} fields",
        filled=len(filled),
        needs_review=len(unanswered),
        blockers=sorted(filler.blockers) or None,
    )
    if unanswered:
        # §12 invariant 2: any field below the confidence line — required or not — is a
        # question for a human. An optional question answered wrongly is still an answer
        # submitted under the user's name.
        reason = filler.review_reason_for(unanswered, essays=essays)
        labels = ", ".join(field.label or field.selector for field in unanswered[:5])
        return attempt.review(
            reason,
            f"{len(unanswered)} field(s) could not be answered confidently: {labels}",
            filler=filler,
            fields=unanswered,
        )

    upload_failure = await _upload_documents(attempt, filler, plan_documents(fields, ctx))
    if upload_failure is not None:
        return upload_failure

    submitted = await filler.submit(dry_run=ctx.dry_run)
    attempt.record(
        _STEP_SUBMIT,
        "clicked the submit control" if filler.submit_clicked else "did not submit",
        attempted=filler.submit_attempted,
        clicked=filler.submit_clicked,
        reported=submitted,
    )

    if not filler.submit_clicked:
        # Two different silences. The gate never let the method touch the DOM (golden rule
        # #3), or the gate opened and no control this system is permitted to click was found
        # (§12 invariant 4). Neither may verify: nothing was sent.
        if not filler.submit_attempted:
            return attempt.review(
                ReviewReason.POLICY_BLOCK, _blocked_message(ctx, pack), filler=filler
            )
        return attempt.review(
            ReviewReason.SUBMIT_NOT_FOUND,
            "no submit control could be located through the provider's selector pack or an "
            "exact accessible name; the form was filled but not submitted",
            filler=filler,
        )

    # A click happened. From here the question is not "did we click" but "does the page
    # prove the employer has it", and only ApplicationVerifier answers that.
    verdict = await ApplicationVerifier().verify(session, pack, initial_url=form_url)
    attempt.record(
        _STEP_VERIFY,
        f"verification: {verdict.method}",
        confirmed=verdict.confirmed,
        method=verdict.method,
        marker=verdict.marker,
        confirmation_id=verdict.confirmation_id,
        reported_by_filler=submitted,
    )

    if verdict.confirmed:
        return attempt.confirmed(verdict, filler)

    if verdict.method == METHOD_ERROR_MARKER:
        # Name the marker, then quote the copy around it. A human opening this review item
        # should not have to guess which of the pack's error markers fired.
        detail = (verdict.confirmation_text or "").strip()[:MAX_ERROR_DETAIL_CHARS]
        return attempt.failed(
            f"the application form reported an error after submit ({verdict.marker})"
            + (f": {detail}" if detail else ""),
            filler=filler,
            evidence=verdict.evidence_screenshot,
        )

    return attempt.review(
        ReviewReason.VERIFICATION_FAILED,
        "the submit control was clicked but the page showed neither a confirmation nor an "
        "error; whether the employer received this application must be confirmed by a human",
        filler=filler,
        evidence=verdict.evidence_screenshot,
    )


async def run_apply(
    ctx: ApplyContext,
    *,
    selector_pack: str | SelectorPack | None = None,
    session: Any | None = None,
) -> ApplyResult:
    """Drive one application through a real browser and verify the outcome.

    Resolved by name from :func:`app.jobs._apply.run_browser_apply`, which has already
    applied the kill switch to *ctx* — so ``ctx.dry_run`` here is authoritative and is only
    ever narrowed further, never widened.

    Args:
        ctx: Everything the attempt needs: the posting, the profile, the rendered documents
            and the pre-planned answers.
        selector_pack: The provider's :class:`~app.browser.selectors.SelectorPack`, or the
            name of one. Falls back to :data:`~app.browser.selectors.GENERIC`, whose
            ``supports_auto_apply=False`` posture routes an unrecognised ATS to a human
            rather than submitting it blind (golden rule #10).
        session: An already-open browser session. Supplied by tests and by a caller that
            wants to reuse a session; when ``None`` a
            :class:`~app.browser.playwright_runner.BrowserSession` is opened and closed here.

    Returns:
        The outcome. ``ok=True`` and
        :attr:`~app.models.enums.ApplicationStatus.CONFIRMED` only when
        :class:`~app.browser.verification.ApplicationVerifier` proved the submission;
        ``FAILED`` when the ATS rejected it; ``NEEDS_REVIEW`` for everything else, including
        an unverifiable submit.

    Raises:
        UnsupportedFlowError: When Playwright is not installed in this deployment. The
            pipeline turns it into a review item naming the URL a human can apply at.
    """
    from app.browser.playwright_runner import BrowserAutomationUnavailable, BrowserSession

    pack = (
        selector_pack
        if isinstance(selector_pack, SelectorPack)
        else (pack_for(selector_pack) if selector_pack else GENERIC)
    )
    log = logger.bind(pack=pack.name, dry_run=bool(ctx.dry_run), **ctx.log_context())
    log.info("apply.started", url=ctx.posting.target_url)

    if session is not None:
        return await _drive(ctx, session, pack)

    from app.config.settings import get_settings

    recorder = ctx.recorder
    artifacts_dir = getattr(recorder, "directory", None) if recorder is not None else None
    try:
        async with BrowserSession(get_settings(), artifacts_dir=artifacts_dir) as opened:
            return await _drive(ctx, opened, pack)
    except BrowserAutomationUnavailable as exc:
        raise UnsupportedFlowError(
            f"browser automation is unavailable ({exc}); apply manually at "
            f"{ctx.posting.target_url}",
            provider=ctx.posting.provider,
            url=ctx.posting.target_url,
        ) from exc
