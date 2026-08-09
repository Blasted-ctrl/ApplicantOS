"""G2 — the browser layer, driven against real Greenhouse, Lever and Ashby forms.

Every other browser test in this repository runs against :class:`tests.fakes.FakePage`, a
recording double that answers whatever the test told it to. That proves the *logic*: the kill
switch returns before touching the DOM, low confidence never fills, a group's options are
resolved to the right elements. It cannot prove the one thing the packs in
:mod:`app.browser.selectors` exist to assert — that those selectors match markup real
employers are serving. And the failure mode of a wrong selector is silent: discovery finds
nothing, every application escalates to manual review, and the product's headline feature
quietly does nothing at all while the entire unit suite stays green.

So this module opens three real application forms in a real Chromium and reports what it
finds. It is marked ``integration`` and therefore excluded from the default run.

**What it found the first time it ran** (2026-08-09), all of it invisible to the unit suite:

1. **Every Lever and Ashby and Greenhouse application would have escalated to
   ``ReviewReason.CAPTCHA``.** All three load a captcha vendor in *invisible*, score-based
   mode on every posting — Greenhouse and Ashby a reCAPTCHA Enterprise badge, Lever an
   hCaptcha enclave — and the packs matched the vendor's bookkeeping rather than a challenge a
   human could solve. One hundred per cent of applications, blocked by a widget that asks
   nothing. Fixed in :data:`~app.browser.selectors._COMMON_CAPTCHA_MARKERS` (which now excludes
   the badge and the response textarea) and in
   :meth:`~app.browser.playwright_runner.BrowserSession._probe_captcha` (which now requires a
   marker to be *rendered*).
2. **Lever's form root was a panel, not the form.** ``.application-form`` is the class Lever
   puts on each of five page sections; the form is ``form#application-form``. Discovery took
   the section with the most controls — 7 of the form's 23 — and never saw the LinkedIn field,
   the employer's custom questions or the consent checkbox. Discovered fields went 6 → 9.
3. **Lever's submit selector pointed at a hidden button.**
   ``[data-qa='submit-application-button']`` matches nothing today; the real control is
   ``#btn-submit[data-qa='btn-submit']``, and the generic ``button[type='submit']`` fallback
   resolved instead to ``#hcaptchaSubmitBtn.hidden``, a zero-size helper that appears *earlier*
   in the document. A Playwright locator's ``.first`` is document order, not selector order, so
   that hidden button won.
4. **Greenhouse's field containers matched nothing.** ``.field, .application-question`` are not
   in the current board's markup, which uses ``.text-input-wrapper`` and ``.select__container``.
   Survivable only because Greenhouse also emits ``<label for>``.
5. **A redirecting Greenhouse board discovered a search box as its application form.** Several
   employers (Stripe, Databricks, Coinbase, Asana, Brex) redirect
   ``job-boards.greenhouse.io/<board>/jobs/<id>`` to their own careers site. No pack selector
   matched there, so discovery fell back to ``document.body`` and returned that site's "Search
   for a role" input as the form's only field. :meth:`~app.browser.autofill.AutoFiller.
   discover_fields` now reports an unmatched root as no fields — a question for a human, which
   is what golden rule #2 requires. The board used below is one that hosts its own form.

**The safety envelope, which is not negotiable.** ``DRY_RUN`` and ``AUTO_APPLY_ENABLED`` are
forced at import time, before any app module can read the environment, and a module-scoped
fixture asserts on the live settings object as well. Every page is instrumented with a
capture-phase click recorder before it navigates, and every test asserts **zero clicks of any
kind** — not "no submit click": nothing at all, for the whole session. No cookie banner is
dismissed, because dismissing one is a click. Nothing is ever submitted, and the only bytes
sent to an employer are one 700-byte placeholder PDF, to one board, to prove that
:meth:`~app.browser.autofill.AutoFiller.upload` can verify an attachment against a real
uploader. One page load per provider, a settle delay, a polite gap between them, and a
User-Agent that says what this traffic is.

**A network or browser failure skips; a selector failure fails.** A dead board, a timeout or a
missing Chromium says nothing about whether the packs are right, and must not redden a nightly
job. A form that loads and yields no résumé upload is exactly what this file exists to catch.
"""

from __future__ import annotations

import os

# Set before any app module can read the environment, so that even a fresh ``Settings()``
# constructed anywhere in this process is closed. This module drives a real browser at real
# employers' application forms; it must not be *able* to submit one (golden rule #3).
os.environ["DRY_RUN"] = "true"
os.environ["AUTO_APPLY_ENABLED"] = "false"

import asyncio
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from app.browser.autofill import AutoFiller, UploadFailedError
from app.browser.playwright_runner import (
    BLOCKER_CAPTCHA,
    BLOCKER_LOGIN_WALL,
    BLOCKERS,
    BrowserAutomationUnavailable,
    BrowserSession,
    BrowserSessionError,
)
from app.browser.selectors import SelectorPack, pack_for
from app.config.settings import Settings, get_settings
from app.jobs.base import FormField
from app.models.enums import ATSProviderName, FieldKind

pytestmark = pytest.mark.integration


# ======================================================================================
# What this module points at
# ======================================================================================

#: Board API roots and page templates, written out here rather than imported. Golden rule #5
#: forbids importing a concrete provider module from outside its own package, and
#: ``tests/integration/test_providers_live.py`` already owns the question of whether the
#: providers parse these feeds correctly. This module only needs one live apply URL each.
GREENHOUSE_JOBS_API: Final[str] = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
GREENHOUSE_APPLY_URL: Final[str] = "https://job-boards.greenhouse.io/{board}/jobs/{job_id}"
LEVER_JOBS_API: Final[str] = "https://api.lever.co/v0/postings/{board}?mode=json"
LEVER_APPLY_URL: Final[str] = "https://jobs.lever.co/{board}/{job_id}/apply"
ASHBY_JOBS_API: Final[str] = "https://api.ashbyhq.com/posting-api/job-board/{board}"
ASHBY_APPLY_URL: Final[str] = "https://jobs.ashbyhq.com/{board}/{job_id}/application"

#: The boards these tests open, one posting each.
#:
#: ``anthropic`` rather than a larger Greenhouse board because it *hosts* its form on
#: ``job-boards.greenhouse.io``. Stripe, Databricks, Coinbase, Asana and Brex all redirect that
#: URL to their own careers site, where the Greenhouse form is not present at all — finding 5
#: in the module docstring. ``veeva`` is a Lever board carrying a custom question card and a
#: consent checkbox, so it exercises radio-group and checkbox discovery, not just text boxes.
#: ``ramp`` is a large Ashby board.
BOARDS: Final[dict[str, str]] = {
    ATSProviderName.GREENHOUSE.value: "anthropic",
    ATSProviderName.LEVER.value: "veeva",
    ATSProviderName.ASHBY.value: "ramp",
}

#: The providers under test, in the order the fixture visits them.
PROVIDERS: Final[tuple[str, ...]] = tuple(BOARDS)

#: Appended to :data:`~app.browser.playwright_runner.DESKTOP_USER_AGENT` so an employer reading
#: their access log can tell what this is. The Chrome prefix is kept because several ATS forms
#: serve a degraded page to an unrecognised agent and then render none of their JavaScript
#: controls, which would make this suite test nothing.
USER_AGENT_SUFFIX: Final[str] = (
    "ApplicantOS-SelectorCheck/1.0 (+integration test; opens public application forms read-only)"
)

#: Sent with the two board-API requests, for the same reason.
API_USER_AGENT: Final[str] = f"ApplicantOS-SelectorCheck/1.0 ({USER_AGENT_SUFFIX})"

#: Seconds to let a form settle after ``load``. All three are single-page applications that
#: render their controls after the load event; reading the DOM too early finds an empty shell.
SETTLE_SECONDS: Final[float] = 6.0

#: Seconds between two providers. This is three page loads in total, so the delay is courtesy
#: rather than rate limiting — but a test suite that hammers somebody's careers site is not a
#: test suite this project ships.
POLITE_GAP_SECONDS: Final[float] = 4.0

#: Timeout for one board-API request.
API_TIMEOUT_SECONDS: Final[float] = 30.0

#: A minimal, structurally valid PDF. Generated rather than committed so that nothing that
#: could be mistaken for a real person's résumé is ever uploaded anywhere.
PLACEHOLDER_PDF: Final[bytes] = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)

#: The one board the placeholder PDF is offered to. Greenhouse's résumé control is a plain
#: ``<input type="file">`` inside a real ``<form>``, which is the case
#: :meth:`~app.browser.autofill.AutoFiller.upload` was written against and the one worth
#: proving. One upload, to one board, is enough: the point is that the verification path works
#: against a real uploader, not that it works three times.
UPLOAD_BOARD: Final[str] = ATSProviderName.GREENHOUSE.value

#: Field kinds that are *not* a plain text box. A form that yields only text inputs has not
#: exercised the parts of discovery that can silently go wrong — option extraction, radio and
#: checkbox grouping, essay detection. Broader than "a select or a textarea" on purpose: Lever
#: renders its custom questions as radio groups and its consent as a checkbox, and both are
#: strictly harder to discover correctly than a ``<select>`` is.
STRUCTURED_KINDS: Final[frozenset[FieldKind]] = frozenset(
    {
        FieldKind.SELECT,
        FieldKind.MULTISELECT,
        FieldKind.TEXTAREA,
        FieldKind.RADIO,
        FieldKind.CHECKBOX,
    }
)

#: Any element that proves a captcha vendor's script is loaded on the page — the *bookkeeping*,
#: not a challenge. Asserted to be present so that "no captcha blocker" is a real discrimination
#: rather than a page that simply has no captcha on it.
CAPTCHA_VENDOR_MARKERS: Final[str] = (
    ".grecaptcha-badge, #g-recaptcha-response, [class*='g-recaptcha'], "
    "#h-captcha, .h-captcha, iframe[src*='captcha']"
)

#: Recorded on the page before it navigates, in the capture phase, so that nothing — not a
#: framework handler, not ``stopPropagation`` — can hide a click from it.
CLICK_RECORDER_SCRIPT: Final[str] = """
() => {
  window.__applicantosClicks = [];
  const record = (event) => {
    const el = event.target;
    const tag = el && el.tagName ? el.tagName.toLowerCase() : "?";
    const id = el && el.id ? "#" + el.id : "";
    const cls = el && el.className && typeof el.className === "string"
      ? "." + el.className.trim().split(/\\s+/).join(".") : "";
    window.__applicantosClicks.push(tag + id + cls);
  };
  for (const name of ["click", "mousedown", "pointerdown"]) {
    document.addEventListener(name, record, true);
  }
}
"""

#: Reads the recorder back. Returns ``[]`` on a page that never ran the init script.
CLICK_READER_SCRIPT: Final[str] = "() => window.__applicantosClicks || []"


# ======================================================================================
# What one live form looked like
# ======================================================================================


@dataclass(frozen=True, slots=True)
class LiveForm:
    """Everything one page load told us, recorded once and asserted on many times.

    Frozen and collected up front so that the "one page load per provider" rule survives the
    module growing more tests: every test below reads this record rather than opening a page.

    Attributes:
        provider: The pack and provider name.
        pack: The selector pack under test.
        apply_url: The URL that was opened.
        final_url: Where the browser ended up, which differs when the board redirects.
        fields: Everything :meth:`~app.browser.autofill.AutoFiller.discover_fields` returned.
        form_root: The selector discovery resolved the application form to.
        blockers: What :meth:`~app.browser.playwright_runner.BrowserSession.detect_blockers`
            reported.
        captcha_vendor_present: Whether a captcha vendor's script is on the page at all.
        submit_matches: How many elements the pack's submit selector resolves to.
        submit_text: The visible text of the first of them.
        submit_visible: Whether that element is rendered.
        submit_returned: What ``AutoFiller.submit(dry_run=True)`` returned.
        submit_attempted: Whether ``submit`` got past the kill switch.
        submit_clicked: Whether ``submit`` clicked anything.
        submit_screenshots: How many proof captures ``submit`` took. Past the gate it takes
            one before the click, so zero is evidence the gate held.
        clicks: Every click, mousedown and pointerdown the page saw, in order.
        upload: ``"verified"``, ``"skipped"``, or the failure that stopped it.
    """

    provider: str
    pack: SelectorPack
    apply_url: str
    final_url: str
    fields: tuple[FormField, ...]
    form_root: str
    blockers: frozenset[str]
    captcha_vendor_present: bool
    submit_matches: int
    submit_text: str
    submit_visible: bool
    submit_returned: bool
    submit_attempted: bool
    submit_clicked: bool
    submit_screenshots: int
    clicks: tuple[str, ...]
    upload: str

    def labelled(self, needle: str, *, kinds: frozenset[FieldKind] | None = None) -> list[str]:
        """Return the labels of the discovered fields whose own label contains *needle*.

        Args:
            needle: A case-insensitive substring, such as ``"email"``.
            kinds: Restrict to these kinds, or ``None`` for any kind.

        Returns:
            The matching labels, which is what a failure message needs to be useful.
        """
        folded = needle.casefold()
        return [
            field.label
            for field in self.fields
            if folded in field.label.casefold() and (kinds is None or field.kind in kinds)
        ]

    def report(self) -> str:
        """Return the human-readable field list — the actual deliverable of this suite.

        The result is escaped to plain ASCII. Real forms mark a required field with ``✱`` and
        write employers' names with curly quotes, and a Windows console still defaults to
        cp1252, so an un-escaped report raises :class:`UnicodeEncodeError` on the way to the
        terminal and fails every test in this module for a reason that has nothing to do with
        any selector. The escaping is for display only; :attr:`fields` keeps the real labels.

        Returns:
            A block naming the URL, the resolved form root, the blockers and every discovered
            field with its kind, whether it is required and its first few options. Printed once
            per run, and embedded in every assertion message so that a failure carries the
            evidence with it.
        """
        lines = [
            f"[{self.provider}] {self.final_url}",
            f"  form root : {self.form_root or '(none)'}",
            f"  blockers  : {sorted(self.blockers) or '(none)'}"
            f"   captcha vendor loaded: {self.captcha_vendor_present}",
            f"  submit    : {self.submit_matches} match(es), visible={self.submit_visible}, "
            f"text={self.submit_text!r}",
            f"  upload    : {self.upload}",
            f"  clicks    : {list(self.clicks) or '(none)'}",
            f"  fields    : {len(self.fields)}",
        ]
        for field in self.fields:
            options = f"  options={list(field.options)[:4]}" if field.options else ""
            required = "required" if field.required else "optional"
            lines.append(
                f"    - {field.kind.value:<10} {required:<8} {field.label[:72]!r}{options}"
            )
        return "\n".join(lines).encode("ascii", errors="backslashreplace").decode("ascii")


# ======================================================================================
# Collection
# ======================================================================================


def _resolve_apply_url(provider: str, board: str) -> str:
    """Ask a provider's public board API for one posting and build its apply URL.

    Args:
        provider: The provider name.
        board: Its board token.

    Returns:
        The absolute apply URL of the board's first posting. Resolved live rather than
        hard-coded because any specific posting will be gone within weeks, and a suite that
        went red for that would be testing the employer's hiring plans rather than the code.

    Raises:
        Skipped: When the API is unreachable, answers non-200, or the board is empty. None of
            those says anything about whether the selector packs are right.
    """
    import httpx

    api, template = {
        ATSProviderName.GREENHOUSE.value: (GREENHOUSE_JOBS_API, GREENHOUSE_APPLY_URL),
        ATSProviderName.LEVER.value: (LEVER_JOBS_API, LEVER_APPLY_URL),
        ATSProviderName.ASHBY.value: (ASHBY_JOBS_API, ASHBY_APPLY_URL),
    }[provider]

    try:
        response = httpx.get(
            api.format(board=board),
            headers={"User-Agent": API_USER_AGENT},
            timeout=API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload: Any = response.json()
    except Exception as exc:
        pytest.skip(f"{provider} board API unreachable ({type(exc).__name__}: {exc})")

    jobs = payload if isinstance(payload, list) else (payload.get("jobs") or [])
    if not jobs:
        pytest.skip(f"{provider} board {board!r} is carrying no postings today")
    job_id = jobs[0].get("id")
    if not job_id:
        pytest.skip(f"{provider} board {board!r} returned a posting with no id")
    return template.format(board=board, job_id=job_id)


async def _observe(provider: str, apply_url: str, resume: Path) -> LiveForm:
    """Open one application form once and record everything the tests need.

    Nothing here decides whether the pack is correct — that is the tests' job. This function's
    single responsibility is to touch the network exactly once per provider and to make sure
    that, whatever happens, no submit control is ever clicked.

    Args:
        provider: The provider name, which is also the pack name.
        apply_url: The page to open.
        resume: The placeholder PDF, offered only to :data:`UPLOAD_BOARD`.

    Returns:
        The recorded observation.
    """
    settings: Settings = get_settings()
    pack = pack_for(provider)

    from app.browser.playwright_runner import DESKTOP_USER_AGENT

    async with BrowserSession(
        settings, user_agent=f"{DESKTOP_USER_AGENT} {USER_AGENT_SUFFIX}"
    ) as session:
        # Installed before navigation so it is running from the page's first instruction.
        await session.page.add_init_script(CLICK_RECORDER_SCRIPT)
        await session.goto(apply_url, wait="load")
        await asyncio.sleep(SETTLE_SECONDS)

        blockers = await session.detect_blockers(pack)
        filler = AutoFiller(session, resolver=None, pack=pack)
        fields = await filler.discover_fields()

        # The kill switch, against a real DOM. Narrowing only: settings already forbid it.
        submit_returned = await filler.submit(dry_run=True)

        submit = session.page.locator(pack.submit)
        submit_matches = await submit.count()
        submit_text = ""
        submit_visible = False
        if submit_matches:
            # Reading a control is not touching it: no click, no focus, no navigation.
            submit_visible = await submit.first.is_visible()
            submit_text = (await submit.first.inner_text()).strip()[:60]

        upload = "skipped"
        if provider == UPLOAD_BOARD:
            upload = await _try_upload(filler, fields, resume)

        vendor = await session.page.locator(CAPTCHA_VENDOR_MARKERS).count()
        clicks = await session.page.evaluate(CLICK_READER_SCRIPT)

        return LiveForm(
            provider=provider,
            pack=pack,
            apply_url=apply_url,
            final_url=session.url,
            fields=tuple(fields),
            form_root=filler.form_root,
            blockers=frozenset(blockers),
            captcha_vendor_present=vendor > 0,
            submit_matches=submit_matches,
            submit_text=submit_text,
            submit_visible=submit_visible,
            submit_returned=submit_returned,
            submit_attempted=filler.submit_attempted,
            submit_clicked=filler.submit_clicked,
            submit_screenshots=len(filler.screenshots),
            clicks=tuple(str(entry) for entry in clicks or ()),
            upload=upload,
        )


async def _try_upload(filler: AutoFiller, fields: list[FormField], resume: Path) -> str:
    """Attach the placeholder PDF to the first discovered file input and verify it stuck.

    Args:
        filler: The filler bound to the open page.
        fields: The discovered fields.
        resume: The placeholder PDF.

    Returns:
        ``"verified"``, ``"no file field discovered"``, or the typed failure. Returned rather
        than raised so that one board's uploader cannot abort the whole collection — the test
        that cares asserts on this string.
    """
    files = [field for field in fields if field.kind is FieldKind.FILE]
    if not files:
        return "no file field discovered"
    try:
        await filler.upload(files[0], resume)
    except UploadFailedError as exc:
        return f"UploadFailedError: {exc}"
    return "verified"


async def _observe_all(resume: Path) -> dict[str, LiveForm]:
    """Visit every provider once, in order, with a polite gap between them.

    Args:
        resume: The placeholder PDF.

    Returns:
        One :class:`LiveForm` per provider.
    """
    observed: dict[str, LiveForm] = {}
    for index, provider in enumerate(PROVIDERS):
        if index:
            await asyncio.sleep(POLITE_GAP_SECONDS)
        observed[provider] = await _observe(
            provider, _resolve_apply_url(provider, BOARDS[provider]), resume
        )
    return observed


@pytest.fixture(scope="module")
def live_forms() -> Iterator[dict[str, LiveForm]]:
    """Open each provider's real application form exactly once for the whole module.

    Deliberately synchronous, driving its own loop through :func:`asyncio.run`. A module-scoped
    *async* fixture would need its own event-loop scope and would then have to be kept in step
    with every test's loop scope; there is nothing to gain from that here, because the browser
    work is all done before the first assertion runs.

    Yields:
        Provider name → observation.

    Raises:
        Skipped: When Playwright or its Chromium is not installed, or when a page could not be
            opened at all. Neither says anything about whether the selector packs are right.
    """
    settings = get_settings()
    if settings.is_submission_allowed:
        raise AssertionError(
            "refusing to open a real application form with submission enabled; "
            "DRY_RUN and AUTO_APPLY_ENABLED are set at the top of this module"
        )

    with tempfile.TemporaryDirectory(prefix="applicantos-live-") as directory:
        resume = Path(directory) / "applicantos-placeholder.pdf"
        resume.write_bytes(PLACEHOLDER_PDF)
        try:
            observed = asyncio.run(_observe_all(resume))
        except BrowserAutomationUnavailable as exc:
            pytest.skip(f"no browser available: {exc}")
        except BrowserSessionError as exc:
            pytest.skip(f"a live application form could not be opened: {exc}")

        print("\n" + "\n".join(form.report() for form in observed.values()))
        yield observed


@pytest.fixture(scope="module", autouse=True)
def switches_are_closed() -> None:
    """Assert on the live settings object, not only on the environment.

    The environment is set at import time, but :func:`~app.config.settings.get_settings` is
    cached, so a singleton built earlier in the session would not have seen it. This fails the
    whole module rather than let a single test open a form under a configuration that could
    submit it.
    """
    settings = get_settings()
    assert settings.dry_run is True, "DRY_RUN must be on for this module"
    assert settings.auto_apply_enabled is False, "AUTO_APPLY_ENABLED must be off"
    assert settings.is_submission_allowed is False


@pytest.fixture(params=PROVIDERS)
def form(request: pytest.FixtureRequest, live_forms: dict[str, LiveForm]) -> LiveForm:
    """One provider's observation, parametrised across all three.

    Args:
        request: The parametrised request, carrying the provider name.
        live_forms: The module-wide observations.

    Returns:
        The observation for this provider.
    """
    return live_forms[str(request.param)]


# ======================================================================================
# The selector packs, against real markup
# ======================================================================================


def test_discovery_finds_the_fields_that_matter(form: LiveForm) -> None:
    """Name, email and a résumé upload are discovered on every real application form.

    These three are the floor. A form this system cannot find a name box, an email box and a
    file input on is a form it cannot apply to at all, so a red assertion here means the pack
    stopped matching that ATS's markup — which is the entire reason this module exists.
    """
    assert form.fields, (
        f"{form.provider}: discovery found no fields at all on a real application form.\n"
        f"{form.report()}"
    )
    assert form.form_root, f"{form.provider}: discovery resolved no form root\n{form.report()}"

    names = form.labelled("name")
    assert names, f"{form.provider}: no field whose label mentions a name\n{form.report()}"

    emails = form.labelled("email")
    assert emails, f"{form.provider}: no field whose label mentions an email\n{form.report()}"

    uploads = [field.label for field in form.fields if field.kind is FieldKind.FILE]
    assert uploads, (
        f"{form.provider}: no file input discovered, so no résumé could ever be attached\n"
        f"{form.report()}"
    )


def test_discovery_finds_a_structured_control(form: LiveForm) -> None:
    """Discovery reaches past the text boxes to a select, textarea, radio group or checkbox.

    Text inputs are the easy half. The parts of discovery that fail silently are the ones that
    read a ``<select>``'s options, collapse a radio group onto one field with the heading as its
    question, and tell a standalone consent checkbox from a group — and Lever's form root bug
    was invisible precisely because the seven controls it *did* find were all text boxes.
    """
    structured = [
        f"{field.kind.value}:{field.label[:40]}"
        for field in form.fields
        if field.kind in STRUCTURED_KINDS
    ]
    assert structured, (
        f"{form.provider}: discovery found only plain text inputs, which is what a form root "
        f"resolving to the wrong container looks like\n{form.report()}"
    )


def test_discovery_finds_a_required_field(form: LiveForm) -> None:
    """At least one discovered field is marked required.

    Every real application form has one, and ``required`` is what
    :meth:`~app.browser.autofill.AutoFiller.review_reason_for` uses to tell
    ``UNKNOWN_FIELD`` from ``LOW_CONFIDENCE``. A form where nothing looked required would send
    every review item to a human with the wrong explanation.
    """
    required = [field.label for field in form.fields if field.required]
    assert required, (
        f"{form.provider}: nothing was discovered as required, so required-ness is not being "
        f"read from this ATS's markup\n{form.report()}"
    )


def test_pack_locates_the_real_submit_control(form: LiveForm) -> None:
    """The pack's submit selector resolves to the visible control a person would press.

    This is the assertion that caught Lever: its selector matched only
    ``#hcaptchaSubmitBtn.hidden``, a zero-size helper button, while the real control sat
    unmatched. Nothing here clicks — the element is counted, its visibility read and its text
    read, all of which are pure reads.
    """
    assert form.submit_matches >= 1, (
        f"{form.provider}: the pack's submit selector ({form.pack.submit}) matches nothing on "
        f"the real form\n{form.report()}"
    )
    assert form.submit_visible, (
        f"{form.provider}: the pack's submit selector resolves first to something invisible "
        f"({form.submit_text!r}); a click on it would time out or bypass the ATS's own "
        f"validation\n{form.report()}"
    )
    assert "submit" in form.submit_text.casefold(), (
        f"{form.provider}: the control the pack would click reads {form.submit_text!r}, which "
        f"is not a submit control\n{form.report()}"
    )


# ======================================================================================
# Blockers
# ======================================================================================


def test_blockers_are_reported_from_the_known_vocabulary(form: LiveForm) -> None:
    """Whatever ``detect_blockers`` saw is one of the four names §12 defines."""
    assert form.blockers <= BLOCKERS, (
        f"{form.provider}: detect_blockers reported {sorted(form.blockers - BLOCKERS)}, which "
        f"is outside the documented vocabulary {sorted(BLOCKERS)}\n{form.report()}"
    )


def test_a_public_application_form_is_not_behind_a_login_wall(form: LiveForm) -> None:
    """No sign-in stands between this session and a form the ATS serves publicly.

    Greenhouse, Lever and Ashby all declare ``supports_auto_apply=True``, and that posture is
    only honest while their forms are reachable without an account. A login wall here would mean
    the ATS had joined Workday in the discovery-only column (golden rule #10).
    """
    assert BLOCKER_LOGIN_WALL not in form.blockers, (
        f"{form.provider}: a login wall was detected on a public application form; "
        f"supports_auto_apply={form.pack.supports_auto_apply} may no longer be honest\n"
        f"{form.report()}"
    )


def test_an_invisible_captcha_is_not_reported_as_a_blocker(form: LiveForm) -> None:
    """A captcha vendor being loaded is not a captcha a human has to solve.

    All three of these ATSs run reCAPTCHA or hCaptcha in invisible, score-based mode on every
    posting. Treating that as a blocker — which the packs did until 2026-08-09 — escalates one
    hundred per cent of applications to manual review and leaves the product with no automation
    at all, while every unit test stays green.

    The vendor's presence is asserted first, so this is a real discrimination rather than a page
    that happens to have no captcha on it. A failure means one of two things, and both are worth
    a person's attention: either the invisible-widget regression is back, or this ATS has begun
    showing first-load visitors a genuine challenge — in which case auto-apply for it must be
    reconsidered, not "fixed".
    """
    assert form.captcha_vendor_present, (
        f"{form.provider}: no captcha vendor found on the page at all, so this test is not "
        f"discriminating anything and its passing means nothing\n{form.report()}"
    )
    assert BLOCKER_CAPTCHA not in form.blockers, (
        f"{form.provider}: a captcha blocker was reported on first load. Either an invisible "
        f"score-based widget is being counted again, or this ATS now challenges every visitor\n"
        f"{form.report()}"
    )


# ======================================================================================
# Golden rule #3, against a real DOM
# ======================================================================================


def test_submit_returns_false_and_clicks_nothing(form: LiveForm) -> None:
    """The kill switch holds on a real employer's form, and nothing was clicked.

    ``tests/test_golden_kill_switch.py`` proves this against a fake page whose submit control is
    present and clickable. This proves it where it matters: a real form, a real submit button
    that really would send a real application, and a click recorder installed in the page's
    capture phase before it navigated.

    The click assertion is the strong one, and it is deliberately absolute — *no* clicks, not
    "no submit click". Nothing in this module dismisses a cookie banner or presses anything, so
    a single recorded interaction means some code path pressed something it was not asked to.
    """
    assert form.submit_returned is False, (
        f"{form.provider}: submit(dry_run=True) returned {form.submit_returned!r}\n"
        f"{form.report()}"
    )
    assert form.submit_clicked is False, f"{form.provider}: submit clicked a control"
    assert form.submit_attempted is False, (
        f"{form.provider}: submit got past the kill switch and began an attempt"
    )
    assert form.submit_screenshots == 0, (
        f"{form.provider}: submit captured {form.submit_screenshots} proof screenshot(s), which "
        f"it only does past the gate — the DOM was touched"
    )
    assert form.clicks == (), (
        f"{form.provider}: the page recorded {list(form.clicks)} during a session that must not "
        f"have clicked anything at all\n{form.report()}"
    )


# ======================================================================================
# Uploading
# ======================================================================================


def test_upload_verifies_an_attachment_against_a_real_uploader(
    live_forms: dict[str, LiveForm],
) -> None:
    """A placeholder PDF offered to a real file input is provably attached afterwards.

    ``set_input_files`` succeeding means the call was accepted, not that the page took the file,
    and an ATS that swaps the native input for a JavaScript uploader can swallow it silently —
    which is how an application arrives with no résumé on it. :meth:`~app.browser.autofill.
    AutoFiller.upload` reads the filename back to rule that out, and until now it had only ever
    read it back from a fake.

    One board, one 700-byte file, nothing submitted.
    """
    form = live_forms[UPLOAD_BOARD]
    assert form.upload == "verified", (
        f"{form.provider}: the placeholder résumé could not be shown to have attached "
        f"({form.upload})\n{form.report()}"
    )
    assert form.clicks == (), f"{form.provider}: uploading clicked something\n{form.report()}"
