"""Proving that an application was really submitted — or admitting that we cannot tell.

Clicking a submit button is not the same thing as submitting an application. The click may
land on a control that validates and re-renders the form, the request may fail after the
button's spinner starts, the ATS may return an error page that looks like a confirmation to a
program and nothing like one to a person. This module is what stands between "we clicked" and
"the employer has it".

It exists because both wrong answers are expensive, in different directions:

* **A false positive** writes :attr:`~app.models.enums.ApplicationStatus.SUBMITTED` into the
  database, and golden rule #1 then refuses to ever apply to that posting again. A job the
  user wanted is silently lost, and nothing in the product will ever notice.
* **A false negative** puts a submitted application back in the review queue, and a human
  spends ten seconds confirming it. That is the cheap failure.

So the verdict is asymmetric on purpose. Evidence is required for success; absence of evidence
is never taken as success. When nothing confirms and nothing errors, :meth:`verify` returns
``confirmed=False`` with ``method="inconclusive"``, and the pipeline treats that as a review
item rather than as a submission (golden rule #2).

Four independent signals are consulted, in this order:

1. **The pack's error markers.** They are checked *first*, because an error marker means the
   submission did **not** go through, and a page can carry both a lingering confirmation
   fragment and a fresh validation error. Deciding for the error is the conservative
   direction: the worst it costs is a human glance.
2. **The pack's success markers** — a dedicated confirmation element, or the copy an employer
   shows afterwards (:class:`~app.browser.selectors.SelectorPack` keeps both in one tuple and
   tells them apart by shape).
3. **A confirmation or reference number**, matched by :data:`CONFIRMATION_ID_PATTERNS`. This
   is the most valuable signal of the four, because it is also the thing
   :mod:`app.tracking` later matches an employer's email against.
4. **A URL that has become a confirmation URL** — ``/confirmation``, ``/thanks``,
   ``?submitted=true``. Greenhouse and Lever both redirect, and the redirect is often the only
   machine-readable evidence there is.

A proof screenshot is captured whatever the verdict, because the artifact is worth as much on
a failure as on a success: it is what a person looks at when deciding what really happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import structlog

from app.browser.autofill import page_text, page_url
from app.browser.selectors import GENERIC, SelectorPack, pack_for

__all__ = [
    "ApplicationVerifier",
    "CONFIRMATION_ID_PATTERNS",
    "CONFIRMATION_URL_TOKENS",
    "DEFAULT_EVIDENCE_NAME",
    "MAX_CONFIRMATION_TEXT_CHARS",
    "METHOD_CONFIRMATION_ID",
    "METHOD_ERROR_MARKER",
    "METHOD_INCONCLUSIVE",
    "METHOD_SUCCESS_MARKER",
    "METHOD_SUCCESS_TEXT",
    "METHOD_URL_CHANGE",
    "VerificationResult",
    "find_confirmation_id",
    "is_confirmation_url",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: How the verdict was reached, recorded on :attr:`VerificationResult.method` and surfaced in
#: the desktop app so a person can see what the system actually saw.
METHOD_SUCCESS_MARKER: Final[str] = "success_marker"
METHOD_SUCCESS_TEXT: Final[str] = "success_text"
METHOD_CONFIRMATION_ID: Final[str] = "confirmation_id"
METHOD_URL_CHANGE: Final[str] = "url_change"
METHOD_ERROR_MARKER: Final[str] = "error_marker"

#: The verdict when nothing confirmed and nothing errored. **Not a success.** The pipeline
#: routes it to review with
#: :attr:`~app.models.enums.ReviewReason.VERIFICATION_FAILED` (golden rule #2).
METHOD_INCONCLUSIVE: Final[str] = "inconclusive"

#: Artifact name for the proof capture.
DEFAULT_EVIDENCE_NAME: Final[str] = "verification"

#: Ceiling on the confirmation text kept for the database. The column exists to show a person
#: what the employer said, not to archive the page.
MAX_CONFIRMATION_TEXT_CHARS: Final[int] = 400

#: Characters of context kept on each side when no sentence boundary can be found.
_SNIPPET_PADDING: Final[int] = 160

#: The nouns an ATS puts in front of the number it wants the applicant to quote back.
_ID_SUBJECTS: Final[str] = (
    r"(?:confirmation|reference|application|candidate|requisition|tracking|ticket|case)"
)

#: Patterns for a confirmation or reference number, tried in order. Each captures a token that
#: is validated afterwards by :func:`find_confirmation_id` — the regex finds candidates, the
#: validator decides, because "Application number is pending" matches the shape perfectly and
#: contains no identifier at all.
CONFIRMATION_ID_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        _ID_SUBJECTS + r"\s*(?:id|number|no\.?|code|#)\s*(?:is\s*)?[:#\-]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9._-]{3,63})",
        re.IGNORECASE,
    ),
    re.compile(_ID_SUBJECTS + r"\s*#\s*([A-Za-z0-9][A-Za-z0-9._-]{3,63})", re.IGNORECASE),
    re.compile(r"\b(?:id|ref)\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9._-]{5,63})", re.IGNORECASE),
)

#: Words that match the shape of an identifier but are prose. A candidate reducing to one of
#: these is discarded rather than written to ``applications.confirmation_id``, where it would
#: later be matched against an employer's email by :mod:`app.tracking`.
_ID_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "pending",
        "unavailable",
        "unknown",
        "none",
        "null",
        "below",
        "above",
        "shown",
        "listed",
        "provided",
        "generated",
        "assigned",
    }
)

#: Shortest acceptable identifier.
_MIN_ID_LENGTH: Final[int] = 4

#: Trailing punctuation a sentence leaves attached to a captured identifier.
_ID_TRAILING: Final[str] = ".,;:)]}\"'!?-"

#: Path and query tokens that mark a URL as a confirmation URL. Deliberately narrow:
#: ``application`` is absent because every apply URL contains it, and a token that fires on the
#: form itself would report a submission that never happened.
CONFIRMATION_URL_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "confirmation",
        "confirmations",
        "confirm",
        "confirmed",
        "thanks",
        "thankyou",
        "thank",
        "success",
        "submitted",
        "received",
        "postapply",
    }
)

#: Splits a URL path or query into comparable tokens.
_URL_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Sentence boundary, used to quote the employer's confirmation copy rather than a fragment.
_SENTENCE_END_RE: Final[re.Pattern[str]] = re.compile(r"[.!?]\s")


# ======================================================================================
# The result
# ======================================================================================


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What the page said about whether the application went through.

    Frozen: a verdict is evidence, and evidence that a later stage could edit is not evidence.

    Attributes:
        confirmed: Whether the submission is *proven*. ``False`` covers three different
            situations — the page reported an error, the page said nothing either way, or the
            page could not be read — and :attr:`method` distinguishes them. None of the three
            may be treated as a success.
        confirmation_id: The reference number the employer displayed, when they displayed one.
            Written to ``applications.confirmation_id`` and later matched against the
            employer's own emails by :mod:`app.tracking`.
        confirmation_text: The sentence the employer showed, capped at
            :data:`MAX_CONFIRMATION_TEXT_CHARS`. Shown to the user as proof.
        evidence_screenshot: The proof capture, if one could be taken.
        method: How the verdict was reached — one of the ``METHOD_*`` constants.
    """

    confirmed: bool
    confirmation_id: str | None = None
    confirmation_text: str | None = None
    evidence_screenshot: Path | None = None
    method: str = METHOD_INCONCLUSIVE

    @property
    def is_inconclusive(self) -> bool:
        """Whether the page gave no evidence in either direction.

        Distinct from an outright failure: nothing said the submission was rejected, so a
        human deciding what happened should look at the screenshot rather than assume the
        worst — or the best.
        """
        return not self.confirmed and self.method == METHOD_INCONCLUSIVE

    def __repr__(self) -> str:
        """Return ``<VerificationResult confirmed=… method=… id=…>`` for logs."""
        return (
            f"<VerificationResult confirmed={self.confirmed} method={self.method!r} "
            f"id={self.confirmation_id!r}>"
        )


# ======================================================================================
# Evidence helpers
# ======================================================================================


def find_confirmation_id(text: str) -> str | None:
    """Extract the reference number an employer displayed, if there is one.

    Args:
        text: The page's visible text.

    Returns:
        The identifier, or ``None``. A candidate is rejected unless it contains a digit and is
        not an ordinary word: ``"Your application number is pending"`` matches the pattern
        perfectly and contains no identifier, and a wrong value here would later be matched
        against the employer's status emails and bind an outcome to the wrong application.
    """
    haystack = text or ""
    for pattern in CONFIRMATION_ID_PATTERNS:
        for match in pattern.finditer(haystack):
            candidate = match.group(1).strip().strip(_ID_TRAILING)
            if len(candidate) < _MIN_ID_LENGTH:
                continue
            if candidate.casefold() in _ID_STOPWORDS:
                continue
            if not any(character.isdigit() for character in candidate):
                continue
            return candidate
    return None


def is_confirmation_url(url: str, *, initial_url: str | None = None) -> bool:
    """Return whether *url* is a confirmation URL rather than an application form.

    Args:
        url: The page's current URL.
        initial_url: The URL the form was opened at, when the caller recorded one. Supplying
            it turns "this looks like a confirmation page" into "this *became* a confirmation
            page", which is the stronger claim.

    Returns:
        ``True`` when a path or query token marks the page as a confirmation and — when
        *initial_url* was supplied — the URL actually changed.
    """
    current = (url or "").strip()
    if not current:
        return False
    if initial_url and current.rstrip("/") == initial_url.strip().rstrip("/"):
        return False
    parts = urlsplit(current.casefold())
    tokens = {
        token
        for chunk in (parts.path, parts.query, parts.fragment)
        for token in _URL_TOKEN_RE.split(chunk)
        if token
    }
    return bool(tokens & CONFIRMATION_URL_TOKENS)


def _snippet(text: str, marker: str | None) -> str | None:
    """Quote the employer's confirmation copy.

    Args:
        text: The page's visible text.
        marker: The success marker that matched, when it was a text marker.

    Returns:
        The sentence containing *marker*, or the opening of the page text when there is no
        marker to anchor on — capped at :data:`MAX_CONFIRMATION_TEXT_CHARS` either way, and
        ``None`` when the page had nothing to say.
    """
    body = (text or "").strip()
    if not body:
        return None
    if not marker:
        return body[:MAX_CONFIRMATION_TEXT_CHARS].strip() or None

    position = body.casefold().find(marker.casefold())
    if position < 0:
        return body[:MAX_CONFIRMATION_TEXT_CHARS].strip() or None

    starts = [match.end() for match in _SENTENCE_END_RE.finditer(body, 0, position)]
    start = starts[-1] if starts else max(0, position - _SNIPPET_PADDING)
    ending = _SENTENCE_END_RE.search(body, position)
    end = ending.end() if ending else min(len(body), position + len(marker) + _SNIPPET_PADDING)
    return body[start:end].strip()[:MAX_CONFIRMATION_TEXT_CHARS] or None


# ======================================================================================
# The verifier
# ======================================================================================


class ApplicationVerifier:
    """Decides whether a submission really happened (``docs/CONTRACTS.md`` §12).

    Stateless apart from the artifact name, so one instance can verify every application in a
    run.

    Usage::

        verifier = ApplicationVerifier()
        result = await verifier.verify(session, pack, initial_url=form_url)
        if not result.confirmed:
            return ApplyResult.needs_review(ReviewReason.VERIFICATION_FAILED, ...)
    """

    def __init__(self, *, screenshot_name: str = DEFAULT_EVIDENCE_NAME) -> None:
        """Configure the verifier.

        Args:
            screenshot_name: Artifact name for the proof capture.
        """
        self.screenshot_name = screenshot_name

    async def verify(
        self,
        session: Any,
        pack: SelectorPack | str | None = None,
        *,
        initial_url: str | None = None,
    ) -> VerificationResult:
        """Establish whether the application went through, and capture proof either way.

        Args:
            session: The browser session, exposing ``page``, ``screenshot`` and ``html``.
            pack: The provider's :class:`~app.browser.selectors.SelectorPack`, or the provider
                name to resolve one from — the pipeline holds the name and the browser layer
                holds the pack, and both call this method.
            initial_url: The URL the form was opened at, when the caller recorded one. It
                turns the URL signal from "looks like a confirmation" into "changed into one".

        Returns:
            The verdict. ``confirmed=True`` requires positive evidence; silence returns
            ``confirmed=False`` with :data:`METHOD_INCONCLUSIVE`, which the pipeline treats as
            needing a human rather than as a submission.
        """
        selectors = pack if isinstance(pack, SelectorPack) else (pack_for(pack) if pack else GENERIC)
        evidence = await self._capture(session)
        text = await page_text(session)
        url = page_url(session)

        error = await self._error_marker(session, selectors, text)
        if error is not None:
            logger.warning(
                "verification.rejected", pack=selectors.name, marker=error, url=url or None
            )
            return VerificationResult(
                confirmed=False,
                confirmation_id=None,
                confirmation_text=_snippet(text, error),
                evidence_screenshot=evidence,
                method=METHOD_ERROR_MARKER,
            )

        method: str | None = None
        matched: str | None = None

        css_marker = await self._css_marker(session, selectors.css_success_markers)
        if css_marker is not None:
            method, matched = METHOD_SUCCESS_MARKER, css_marker
        else:
            text_marker = selectors.matches_success_text(text)
            if text_marker is not None:
                method, matched = METHOD_SUCCESS_TEXT, text_marker

        confirmation_id = find_confirmation_id(text)
        if method is None and confirmation_id is not None:
            method = METHOD_CONFIRMATION_ID
        if method is None and is_confirmation_url(url, initial_url=initial_url):
            method = METHOD_URL_CHANGE

        if method is None:
            logger.warning(
                "verification.inconclusive",
                pack=selectors.name,
                url=url or None,
                text_length=len(text),
            )
            return VerificationResult(
                confirmed=False,
                confirmation_id=None,
                confirmation_text=None,
                evidence_screenshot=evidence,
                method=METHOD_INCONCLUSIVE,
            )

        logger.info(
            "verification.confirmed",
            pack=selectors.name,
            method=method,
            marker=matched,
            has_confirmation_id=confirmation_id is not None,
            url=url or None,
        )
        return VerificationResult(
            confirmed=True,
            confirmation_id=confirmation_id,
            confirmation_text=_snippet(text, matched),
            evidence_screenshot=evidence,
            method=method,
        )

    # ----------------------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------------------

    async def _capture(self, session: Any) -> Path | None:
        """Take the proof screenshot, and never let it change the verdict.

        Args:
            session: The browser session.

        Returns:
            The artifact path, or ``None`` when the capture failed — which is logged and
            otherwise ignored, because a missing screenshot is a missing convenience and a
            wrong verdict is a lost job.
        """
        capture = getattr(session, "screenshot", None)
        if capture is None:
            return None
        try:
            path = capture(self.screenshot_name)
            if hasattr(path, "__await__"):
                path = await path
        except Exception as exc:  # noqa: BLE001 - evidence is best-effort
            logger.warning(
                "verification.screenshot_failed",
                name=self.screenshot_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        return Path(path) if isinstance(path, (str, Path)) else None

    async def _error_marker(
        self, session: Any, pack: SelectorPack, text: str
    ) -> str | None:
        """Return the evidence that the submission was rejected, if there is any."""
        css = await self._css_marker(session, pack.css_error_markers)
        if css is not None:
            return css
        return pack.matches_error_text(text)

    @staticmethod
    async def _css_marker(session: Any, markers: tuple[str, ...]) -> str | None:
        """Return the first CSS marker present on the page.

        Args:
            session: The browser session.
            markers: CSS selectors from the pack.

        Returns:
            The matching marker, or ``None``. Markers are queried unscoped, because a
            confirmation page has usually replaced the application form entirely.
        """
        page = getattr(session, "page", None)
        locate = getattr(page, "locator", None)
        if locate is None:
            return None
        for marker in markers:
            try:
                count = await locate(marker).count()
            except Exception as exc:  # noqa: BLE001 - an unusable selector is simply no match
                logger.debug(
                    "verification.locator_failed",
                    marker=marker,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            if isinstance(count, int) and count > 0:
                return marker
        return None
