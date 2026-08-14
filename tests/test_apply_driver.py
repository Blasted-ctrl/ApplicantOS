"""The apply driver — proof that submission verification actually runs.

:class:`~app.browser.verification.ApplicationVerifier` was fully built, documented and
unreachable: no caller outside its own module, which meant ``docs/SAFETY.md``'s promise of
*proof of submission* was not being kept. A click was being read as a submission, and
``docs/RESEARCH_EVOLVEAGENT.md`` Gap 3 says the gap is wiring, not design.

These tests drive :func:`app.browser.apply.run_apply` end to end against
:class:`tests.fakes.FakeSession` — no Playwright, no browser binary, no network — and assert
the four outcomes the verdict may take, plus the two that must never reach it:

============================  ===================================================
Page after the submit click   Outcome
============================  ===================================================
confirmation copy + a number  ``CONFIRMED`` with the id and a proof screenshot
an error marker               ``FAILED``, quoting what the ATS said
nothing either way            ``NEEDS_REVIEW`` / ``VERIFICATION_FAILED``
============================  ===================================================

and, before any of that can happen: a required resume upload that cannot be satisfied is
``FILE_UPLOAD_FAILED`` (``docs/OPEN_QUESTIONS.md`` section J — ``AutoFiller.fill`` skips
``FieldKind.FILE``, so a driver iterating only the needs-review list would submit an
application with no resume attached), and a dry run clicks nothing and **never verifies**.

The assertions are against recorded state — ``page.clicks``, ``session.screenshots`` — for
the same reason the kill-switch file is: a driver could return the right enum while having
clicked, or claim a confirmation it never looked for.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.browser.apply import (
    SCREENSHOT_FORM_FILLED,
    SCREENSHOT_FORM_LOADED,
    plan_documents,
    run_apply,
)
from app.browser.selectors import PACKS, SelectorPack
from app.browser.verification import DEFAULT_EVIDENCE_NAME
from app.jobs.base import ApplyContext, ApplyResult, FormField, JobPostingDTO, UserProfileDTO
from app.models.enums import (
    ApplicationStatus,
    ATSProviderName,
    FieldKind,
    ReviewReason,
)
from tests.fakes import FakePage, FakeSession, PageTransition, discovery_payload, form_control

#: The submit control this form offers. Present on every page here, so a "nothing was
#: clicked" assertion can never be an artefact of there being nothing to click.
SUBMIT_SELECTOR = "#submit_app"

#: The same control as the Greenhouse pack reaches it — scoped inside the form root. A click
#: recorded against *this* string is proof the control was found through the provider's pack
#: rather than through the accessible-name fallback (§12 invariant 4).
SCOPED_SUBMIT = f"#application_form {SUBMIT_SELECTOR}"

#: What Greenhouse actually shows afterwards: the pack's success copy plus a reference number
#: in the shape :data:`~app.browser.verification.CONFIRMATION_ID_PATTERNS` looks for.
CONFIRMATION_TEXT = (
    "Thank you for applying to Acme Robotics. Your confirmation number is GH-4417-22. "
    "We will be in touch within two weeks."
)


# ======================================================================================
# Fixtures
# ======================================================================================


@pytest.fixture
def resume_file(tmp_path: Path) -> Path:
    """A rendered resume on disk, which is what the form's file input is offered."""
    document = tmp_path / "ada-lovelace-backend-engineer.pdf"
    document.write_bytes(b"%PDF-1.7 rendered resume")
    return document


@pytest.fixture
def profile() -> UserProfileDTO:
    """An applicant whose contact details answer the whole form deterministically.

    Every field below resolves through ``KNOWN_FIELDS`` at ``KNOWN_CONFIDENCE`` (0.95), above
    the 0.75 default threshold — so the fill step is exercised for real rather than stubbed,
    and no model is involved.
    """
    return UserProfileDTO(
        user_id=uuid.uuid4(),
        full_name="Ada Lovelace",
        email="ada@example.com",
        phone="+44 7700 900123",
        location="Manchester, UK",
        links={"github": "https://github.com/ada"},
    )


@pytest.fixture
def posting() -> JobPostingDTO:
    """The posting being applied to."""
    return JobPostingDTO(
        id=uuid.uuid4(),
        provider=ATSProviderName.GREENHOUSE,
        external_id="4417",
        title="Senior Backend Engineer",
        company_name="Acme Robotics",
        url="https://boards.greenhouse.io/acme/jobs/4417",
        apply_url="https://boards.greenhouse.io/acme/jobs/4417#app",
    )


def _context(
    posting: JobPostingDTO,
    profile: UserProfileDTO,
    *,
    resume: Path | None = None,
    dry_run: bool = False,
) -> ApplyContext:
    """One attempt's context."""
    return ApplyContext(
        application_id=uuid.uuid4(),
        posting=posting,
        user=profile,
        resume_path=resume,
        dry_run=dry_run,
    )


def _form(*, with_file: bool = True) -> list[dict]:
    """The controls a small Greenhouse application form exposes."""
    controls = [
        form_control("#first_name", label="First Name", name="first_name", required=True),
        form_control("#last_name", label="Last Name", name="last_name", required=True),
        form_control(
            "#email", label="Email", control_type="email", name="email", required=True
        ),
        form_control("#phone", label="Phone", control_type="tel", name="phone"),
    ]
    if with_file:
        controls.append(
            form_control(
                "#resume",
                label="Resume/CV",
                control_type="file",
                name="resume",
                required=True,
            )
        )
    return controls


def _page(
    after_submit: PageTransition | None,
    *,
    with_file: bool = True,
    extra: list[dict] | None = None,
) -> FakePage:
    """A loaded application form whose controls and submit button all really exist.

    Every discovered selector is in ``present``, so a field that does not get filled failed
    for a reason this system decided on rather than because the double had nothing to type
    into.
    """
    controls = [*_form(with_file=with_file), *(extra or [])]
    return FakePage(
        present={
            SUBMIT_SELECTOR,
            SCOPED_SUBMIT,
            *(control["selector"] for control in controls),
        },
        roles={("button", "Submit Application")},
        discovery=discovery_payload(*controls),
        text="Apply for Senior Backend Engineer",
        url="https://boards.greenhouse.io/acme/jobs/4417#app",
        after_submit=after_submit,
    )


@pytest.fixture
def fast_submit_wait(settings, monkeypatch: pytest.MonkeyPatch):
    """Shrink the post-click wait to its floor.

    ``AutoFiller._await_outcome`` waits ``playwright_timeout_ms`` (30 s by default, clamped
    to a 2 s floor) for a marker to appear before reporting that it could not tell. Only the
    inconclusive case ever reaches that ceiling, and 2 s is the shortest the floor allows —
    which is itself the point: "we did not look" must never be reachable by mis-setting a
    timeout.
    """
    monkeypatch.setattr(settings, "playwright_timeout_ms", 100)
    return settings


# ======================================================================================
# The verdict — the wiring this file exists to prove
# ======================================================================================


async def test_a_confirmed_submit_reaches_confirmed_with_an_id_and_proof(
    submission_allowed, posting, profile, resume_file
) -> None:
    """The happy path: the page proves it, so the result says ``CONFIRMED``."""
    page = _page(
        PageTransition(
            present={".application-confirmation"},
            text=CONFIRMATION_TEXT,
            url="https://boards.greenhouse.io/acme/jobs/4417/confirmation",
        )
    )
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.ok is True
    assert result.status is ApplicationStatus.CONFIRMED
    assert result.review_reason is None
    # The confirmation id is what app.tracking later matches the employer's email against.
    assert result.confirmation_id == "GH-4417-22"
    assert "Thank you for applying" in (result.confirmation_text or "")
    # Proof of submission: the two captures §12 invariant 3 requires, plus the filled form
    # (captured before the unanswered-required check can divert, so a review item always
    # shows what the form actually looked like) and the verifier's own evidence shot.
    assert session.screenshots == [
        SCREENSHOT_FORM_LOADED,
        SCREENSHOT_FORM_FILLED,
        "before_submit",
        "after_submit",
        DEFAULT_EVIDENCE_NAME,
    ]
    assert any(path.name.startswith(DEFAULT_EVIDENCE_NAME) for path in result.screenshot_paths)
    assert page.clicks == [SCOPED_SUBMIT]
    assert page.uploads == [("#resume", str(resume_file))]


async def test_an_inconclusive_page_reaches_needs_review_not_success(
    submission_allowed, fast_submit_wait, posting, profile, resume_file
) -> None:
    """Silence is never read as success — the single most important mapping here.

    A false positive writes ``CONFIRMED``, golden rule #1 then refuses to ever apply to that
    posting again, and the job is lost with nothing in the product noticing. A false negative
    costs a human ten seconds.
    """
    page = _page(PageTransition(present=set(), text="Processing your request."))
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.ok is False
    assert result.status is ApplicationStatus.NEEDS_REVIEW
    assert result.review_reason is ReviewReason.VERIFICATION_FAILED
    assert result.confirmation_id is None
    assert page.clicks == [SCOPED_SUBMIT], "the click still happened; only the verdict is open"
    # The verifier ran and captured proof even though it could not decide — that screenshot
    # is exactly what the human in the review queue looks at.
    assert DEFAULT_EVIDENCE_NAME in session.screenshots


async def test_an_error_marker_reaches_failed_and_quotes_the_page(
    submission_allowed, posting, profile, resume_file
) -> None:
    """The ATS said no, so the result says ``FAILED`` rather than "needs a human"."""
    page = _page(
        PageTransition(
            present=set(),
            text="There was a problem with your application. Please try again.",
        )
    )
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.ok is False
    assert result.status is ApplicationStatus.FAILED
    assert "There was a problem with your application" in (result.error or "")
    assert DEFAULT_EVIDENCE_NAME in session.screenshots


async def test_a_css_error_marker_is_named_in_the_error(
    submission_allowed, posting, profile, resume_file
) -> None:
    """Some ATSs signal rejection with an element, not with copy.

    The marker is then the only thing that identifies what fired, so it is carried out of the
    verdict and into the error rather than being left in a log line.
    """
    page = _page(
        PageTransition(present={".field_with_errors"}, text="Please check the highlighted fields.")
    )
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.status is ApplicationStatus.FAILED
    assert ".field_with_errors" in (result.error or "")


async def test_a_confirmation_url_alone_is_enough_to_confirm(
    submission_allowed, fast_submit_wait, posting, profile, resume_file
) -> None:
    """Greenhouse and Lever both redirect, and the redirect is often the only evidence.

    The filler cannot see it — it watches for markers, not URLs — so this case would be
    reported as an unverified submission by anything except the verifier.
    """
    page = _page(
        PageTransition(
            present=set(),
            text="",
            url="https://boards.greenhouse.io/acme/jobs/4417/thanks",
        )
    )
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.ok is True
    assert result.status is ApplicationStatus.CONFIRMED
    assert result.confirmation_id is None


# ======================================================================================
# The kill switch — verification must not run at all
# ======================================================================================


async def test_a_dry_run_clicks_nothing_and_never_verifies(
    submission_allowed, posting, profile, resume_file
) -> None:
    """Both settings switches are open; the caller asked for a rehearsal anyway.

    The form is filled and the documents attached — that is the point of a rehearsal — but
    the submit control is never located, never clicked, and the verifier never runs. An
    evidence screenshot after a dry run would be a page that proves nothing about a
    submission that did not happen.
    """
    page = _page(PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT))
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file, dry_run=True),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.ok is False
    assert result.status is ApplicationStatus.NEEDS_REVIEW
    assert result.review_reason is ReviewReason.POLICY_BLOCK
    assert page.clicks == [], "a dry run clicked something"
    assert DEFAULT_EVIDENCE_NAME not in session.screenshots, "a dry run verified a submission"
    assert page.transitioned is False
    # The rehearsal still did its job.
    assert page.fills, "a dry run should still fill the form"
    assert page.uploads == [("#resume", str(resume_file))]


async def test_both_switches_closed_blocks_before_the_submit_control(
    settings, monkeypatch, posting, profile, resume_file
) -> None:
    """The packaged defaults: nothing is submitted and nothing is verified."""
    monkeypatch.setattr(settings, "auto_apply_enabled", False)
    monkeypatch.setattr(settings, "dry_run", True)

    page = _page(PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT))
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.review_reason is ReviewReason.POLICY_BLOCK
    assert page.clicks == []
    assert DEFAULT_EVIDENCE_NAME not in session.screenshots


async def test_the_generic_pack_never_submits(
    submission_allowed, posting, profile, resume_file
) -> None:
    """An ATS this system does not recognise routes to a human (golden rule #10)."""
    page = _page(PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT))
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        session=session,
    )

    assert result.review_reason is ReviewReason.POLICY_BLOCK
    assert page.clicks == []
    assert DEFAULT_EVIDENCE_NAME not in session.screenshots


# ======================================================================================
# File uploads — the FieldKind.FILE gap
# ======================================================================================


async def test_a_required_resume_with_no_document_reaches_file_upload_failed(
    submission_allowed, posting, profile
) -> None:
    """The form wants a resume and this attempt has none, so nothing is submitted.

    ``AutoFiller.fill`` skips ``FieldKind.FILE`` entirely, so the file input appears in
    neither list it returns. A driver that trusted those lists would find "nothing needs
    review" and submit an application with no resume on it — worse than not applying.
    """
    page = _page(PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT))
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=None),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.ok is False
    assert result.review_reason is ReviewReason.FILE_UPLOAD_FAILED
    assert [field.selector for field in result.unanswered_fields] == ["#resume"]
    assert page.clicks == [], "submitted a form whose required resume was never attached"
    assert page.uploads == []


async def test_an_upload_the_page_swallows_reaches_file_upload_failed(
    submission_allowed, posting, profile, resume_file
) -> None:
    """``set_input_files`` failing is a review item, not an exception and not a submission."""
    page = _page(PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT))
    page.reject_uploads.add("#resume")
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.review_reason is ReviewReason.FILE_UPLOAD_FAILED
    assert resume_file.name in (result.error or "")
    assert page.clicks == []


async def test_a_form_with_no_file_input_still_submits(
    submission_allowed, posting, profile, resume_file
) -> None:
    """Not every application asks for a document; the reconciliation must not invent one."""
    page = _page(
        PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT),
        with_file=False,
    )
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.status is ApplicationStatus.CONFIRMED
    assert page.uploads == []


# ======================================================================================
# Document planning
# ======================================================================================


def _file_field(label: str, *, selector: str = "#file", required: bool = True) -> FormField:
    """One discovered file input."""
    return FormField(selector=selector, label=label, kind=FieldKind.FILE, required=required)


def test_the_cover_letter_slot_gets_the_cover_letter(tmp_path: Path, posting, profile) -> None:
    """Both documents exist and both slots are labelled, so both are placed correctly."""
    resume = tmp_path / "resume.pdf"
    cover = tmp_path / "cover.pdf"
    resume.write_bytes(b"resume")
    cover.write_bytes(b"cover")
    ctx = ApplyContext(
        application_id=uuid.uuid4(),
        posting=posting,
        user=profile,
        resume_path=resume,
        cover_letter_path=cover,
    )

    plan = plan_documents(
        [
            _file_field("Resume/CV", selector="#resume"),
            _file_field("Cover Letter", selector="#cover"),
        ],
        ctx,
    )

    assert [(field.selector, path.name) for field, path in plan.uploads] == [
        ("#resume", "resume.pdf"),
        ("#cover", "cover.pdf"),
    ]
    assert plan.missing == []


def test_a_single_unlabelled_upload_gets_the_resume(tmp_path: Path, posting, profile) -> None:
    """One anonymous file input on a job application is the resume."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume")
    ctx = ApplyContext(
        application_id=uuid.uuid4(), posting=posting, user=profile, resume_path=resume
    )

    plan = plan_documents([_file_field("Attachment", selector="#file")], ctx)

    assert [field.selector for field, _ in plan.uploads] == ["#file"]


def test_a_second_unlabelled_upload_is_not_given_the_resume_twice(
    tmp_path: Path, posting, profile
) -> None:
    """Attaching the resume again tells the employer nothing and fills the wrong slot."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume")
    ctx = ApplyContext(
        application_id=uuid.uuid4(), posting=posting, user=profile, resume_path=resume
    )

    plan = plan_documents(
        [
            _file_field("Attachment", selector="#one"),
            _file_field("Transcript", selector="#two"),
            _file_field("Portfolio", selector="#three", required=False),
        ],
        ctx,
    )

    assert [field.selector for field, _ in plan.uploads] == ["#one"]
    assert [field.selector for field in plan.missing] == ["#two"]
    assert [field.selector for field in plan.skipped] == ["#three"]


def test_a_document_that_no_longer_exists_counts_as_missing(
    tmp_path: Path, posting, profile
) -> None:
    """A stale path is caught before the form is touched, not by a Playwright error."""
    ctx = ApplyContext(
        application_id=uuid.uuid4(),
        posting=posting,
        user=profile,
        resume_path=tmp_path / "deleted.pdf",
    )

    plan = plan_documents([_file_field("Resume", selector="#resume")], ctx)

    assert plan.uploads == []
    assert [field.selector for field in plan.missing] == ["#resume"]


# ======================================================================================
# Everything that stops the attempt before a click
# ======================================================================================


async def test_an_unanswerable_required_field_stops_before_submitting(
    submission_allowed, posting, profile, resume_file
) -> None:
    """A field nothing can answer is a question for a human, not a blank submission."""
    page = _page(
        PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT),
        with_file=False,
        extra=[
            form_control(
                "#referral",
                label="Which of our engineers referred you?",
                name="referral",
                required=True,
            )
        ],
    )
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.review_reason is ReviewReason.UNKNOWN_FIELD
    assert [field.selector for field in result.unanswered_fields] == ["#referral"]
    assert page.clicks == []
    assert DEFAULT_EVIDENCE_NAME not in session.screenshots


async def test_a_captcha_stops_before_anything_is_typed(
    submission_allowed, posting, profile, resume_file
) -> None:
    """A bot challenge is escalated, never solved (golden rule #2)."""
    page = _page(PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT))
    session = FakeSession(page=page, blockers={"captcha"})

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.review_reason is ReviewReason.CAPTCHA
    assert page.fills == []
    assert page.clicks == []


async def test_an_unreadable_form_is_a_review_item_not_an_empty_form(
    submission_allowed, posting, profile, resume_file
) -> None:
    """Discovering nothing means the page was not the form, not that the form was empty."""
    page = _page(None)
    page.discovery = discovery_payload()
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.status is ApplicationStatus.NEEDS_REVIEW
    assert result.review_reason is ReviewReason.AMBIGUOUS_ANSWER
    assert page.clicks == []


async def test_no_submit_control_is_submit_not_found(
    submission_allowed, posting, profile, resume_file
) -> None:
    """The gate opened, the DOM was searched, and nothing this system may click was there."""
    page = _page(None, with_file=False)
    # The form is fillable; only the submit control is missing.
    page.present.discard(SUBMIT_SELECTOR)
    page.present.discard(SCOPED_SUBMIT)
    page.roles.clear()
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    assert result.review_reason is ReviewReason.SUBMIT_NOT_FOUND
    assert page.clicks == []
    assert page.lookups != [], "the gate opened, so the DOM should have been searched"
    assert DEFAULT_EVIDENCE_NAME not in session.screenshots


# ======================================================================================
# The seam into app/jobs
# ======================================================================================


async def test_the_jobs_seam_now_resolves_a_real_entry_point() -> None:
    """``app.jobs._apply`` finds the driver, so providers no longer raise unsupported flow.

    Before this wiring, ``browser_available()`` was ``False`` and every Greenhouse, Lever and
    Ashby submission became an ``UnsupportedFlowError`` review item.
    """
    from app.jobs._apply import browser_available

    assert browser_available() is True


async def test_the_seam_hands_the_driver_its_pack_and_the_guarded_context(
    settings, monkeypatch, posting, profile
) -> None:
    """``run_browser_apply`` calls this driver, with the pack name and a narrowed context.

    The seam owns the kill switch and this module owns the attempt, so the contract between
    them is worth pinning: the entry point is found by name, receives ``selector_pack``, and
    receives a context whose ``dry_run`` has been forced on by the closed switch — never
    widened.
    """
    import app.browser as browser_package
    from app.jobs._apply import run_browser_apply

    monkeypatch.setattr(settings, "auto_apply_enabled", False)
    monkeypatch.setattr(settings, "dry_run", True)
    seen: dict[str, object] = {}

    async def _record(ctx, *, selector_pack=None, session=None):
        seen["pack"] = selector_pack
        seen["dry_run"] = ctx.dry_run
        return ApplyResult.needs_review(ReviewReason.POLICY_BLOCK, error="stub")

    monkeypatch.setattr(browser_package, "run_apply", _record)

    result = await run_browser_apply(_context(posting, profile, dry_run=False), "greenhouse")

    assert seen == {"pack": "greenhouse", "dry_run": True}
    assert result.review_reason is ReviewReason.POLICY_BLOCK


async def test_the_browser_log_records_the_verification_step(
    submission_allowed, posting, profile, resume_file
) -> None:
    """The audit trail names the verdict, so "why is this confirmed?" has an answer on file."""
    page = _page(
        PageTransition(present={".application-confirmation"}, text=CONFIRMATION_TEXT)
    )
    session = FakeSession(page=page)

    result = await run_apply(
        _context(posting, profile, resume=resume_file),
        selector_pack="greenhouse",
        session=session,
    )

    steps = [entry["step"] for entry in result.browser_log]
    assert steps == ["navigate", "discover", "fill", "upload", "submit", "verify"]
    verify = result.browser_log[-1]
    assert verify["confirmed"] is True
    assert verify["confirmation_id"] == "GH-4417-22"


# ======================================================================================
# Scoping — the property that keeps a click on the control and off the form
# ======================================================================================


def test_scoping_crosses_every_root_with_every_target() -> None:
    """Both sides are selector lists, so scoping is a cross product, not a prefix."""
    pack = SelectorPack(
        name="fake",
        form_root="#a, form.b",
        field_container="",
        label="",
        input="",
        file_input="input[type='file'], .dropzone input",
        submit="",
        success_markers=(),
        error_markers=(),
        next_step="",
        cookie_banner="",
        captcha_markers=(),
    )

    assert pack.scoped(pack.file_input).split(", ") == [
        "#a input[type='file']",
        "#a .dropzone input",
        "form.b input[type='file']",
        "form.b .dropzone input",
    ]


def test_no_scoped_alternative_is_ever_a_bare_form_root() -> None:
    """The regression this file exists to prevent, checked across every shipped pack.

    A prefix-only scoping of ``"#a, form.b"`` + ``"input[type='file']"`` produces
    ``"#a, form.b input[type='file']"``, whose *first* alternative is the form itself.
    ``locator(...).first`` then resolves to the ``<form>``, and Playwright uploads to — or
    clicks — the wrong element. Every alternative must therefore name something inside a
    root, never a root.
    """
    for pack in PACKS.values():
        roots = {part.strip() for part in pack.form_root.split(",") if part.strip()}
        if not roots:
            continue
        for selector in (pack.input, pack.file_input, pack.submit, pack.field_container):
            if not selector:
                continue
            for alternative in pack.scoped(selector).split(", "):
                assert alternative not in roots, f"{pack.name}: {alternative!r} is a bare root"
                assert " " in alternative, f"{pack.name}: {alternative!r} is not scoped"
