"""Selector packs — the per-ATS knowledge the browser layer needs, and nothing else.

Greenhouse, Lever and Ashby all submit an application the same way: open the form, read the
labels, fill the controls, upload the documents, verify, submit. What differs between them
is *where things are on the page*. This module is that difference, extracted into data.

Keeping the selectors here rather than inside the automation code buys three things:

**One implementation of the risky part.** :func:`app.jobs._apply.run_browser_apply` drives
every provider through the same code path, so the kill switch, the dry-run guard and the
escalation rules (``docs/CONTRACTS.md`` §12, golden rules #2 and #3) are written once. A
provider contributes a pack name, not a copy of the submission logic.

**A verifiable safety invariant.** §12 rule 4 says only a submit control located through the
provider's pack — or by an exact accessible-name match — may ever be clicked. That rule is
checkable precisely because :attr:`SelectorPack.submit` is the *only* place a submit
selector is written down. A pack is also the only place a captcha or cookie banner is
described, so "did we detect the blocker?" has one answer per ATS.

**Import-time independence from Playwright.** This module is pure data and pure functions:
no third-party import, no browser, no network. ``app.browser`` can therefore be imported —
and a provider can therefore reference its pack — on a machine with no Playwright installed,
which is exactly the state of a discovery-only deployment. The Playwright runtime lands in a
later phase and slots in beside this file without changing it.

A marker string may be either a CSS selector or a piece of visible text; both forms occur in
the wild, because some ATSs mark success with a dedicated element and others merely change
the copy. :func:`is_css_selector` classifies them, and
:meth:`SelectorPack.css_success_markers` / :meth:`SelectorPack.text_success_markers` split a
pack's markers for a caller that treats the two differently.

Workday's pack is present for completeness and marked
``supports_auto_apply=False``: its application flow is account-gated and multi-step, so it is
described here for field discovery and blocker detection only, never for submission
(``docs/CONTRACTS.md`` §9, golden rule #10). The generic fallback carries the same posture —
an ATS this system does not recognise is escalated to a human rather than submitted blind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import structlog

from app.models.enums import ATSProviderName

__all__ = [
    "ASHBY",
    "GENERIC",
    "GREENHOUSE",
    "LEVER",
    "PACKS",
    "SelectorPack",
    "WORKDAY",
    "is_css_selector",
    "pack_for",
    "pack_names",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Marker classification
# ======================================================================================

#: Leading characters that make a marker unambiguously a CSS selector: a class, an id, an
#: attribute filter, a pseudo-class, or a descendant/child combinator.
_CSS_LEADING_CHARS: Final[tuple[str, ...]] = (".", "#", "[", ":", ">", "*")

#: A marker that opens with a bare element name followed by a selector construct — for
#: example ``form#application-form``, ``input[type=file]`` or ``div.confirmation``. Matched
#: against the whole marker so that ordinary prose ("This field is required") cannot be
#: mistaken for markup.
_CSS_ELEMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9-]*\s*[.#\[:]"
)

#: A comma-separated selector list where at least one alternative is itself selector-shaped.
_CSS_LIST_SEPARATOR: Final[str] = ","


def is_css_selector(marker: str) -> bool:
    """Return whether *marker* should be treated as a CSS selector rather than as text.

    Success and error markers are written in whichever form the ATS actually offers: Lever
    renders a dedicated ``.application-confirmation`` element, Greenhouse simply says
    "Thank you for applying". Both belong in the same tuple, so they are told apart here by
    shape rather than by being kept in two separate fields nobody would remember to fill in.

    The test is deliberately conservative: a marker qualifies only when it opens with a CSS
    punctuation character, or with an element name immediately followed by one. Prose is
    therefore never mis-read as markup, and the cost of an error in the other direction is
    merely that a selector is also searched for as literal text, which finds nothing.

    Args:
        marker: One entry from a pack's marker tuple.

    Returns:
        ``True`` when *marker* looks like a CSS selector.

    Example:
        >>> is_css_selector(".application-confirmation")
        True
        >>> is_css_selector("Thank you for applying")
        False
    """
    candidate = (marker or "").strip()
    if not candidate:
        return False
    if _CSS_LIST_SEPARATOR in candidate:
        return any(
            is_css_selector(alternative) for alternative in candidate.split(_CSS_LIST_SEPARATOR)
        )
    if candidate.startswith(_CSS_LEADING_CHARS):
        return True
    return bool(_CSS_ELEMENT_RE.match(candidate))


# ======================================================================================
# The pack
# ======================================================================================


@dataclass(frozen=True, slots=True)
class SelectorPack:
    """Everything the browser layer needs to know about one ATS's application form.

    Immutable on purpose. A pack is shared process-wide and read from concurrent apply
    attempts; a mutable one could be edited by a provider mid-run, which would quietly move
    the submit selector out from under §12 rule 4.

    Every field is a CSS selector string, or — for the ``*_markers`` tuples — a mixture of
    CSS selectors and literal visible text, told apart by :func:`is_css_selector`. A field
    that does not apply to an ATS is an empty string rather than ``None``, so a caller can
    always concatenate or truthiness-test without a type check.

    Attributes:
        name: Pack identifier, matching the provider name it serves.
        form_root: The application form itself. Every other selector is scoped inside it, so
            a marketing form elsewhere on the page can never be filled by mistake.
        field_container: The repeating wrapper around one label/control pair, used to
            associate a control with the text a human reads next to it.
        label: The visible label inside a field container.
        input: The fillable controls: text inputs, textareas and selects. Hidden controls
            and buttons are excluded, because filling a hidden field is never intended.
        file_input: The resume/cover-letter upload control.
        submit: The control that really submits. **The only selector §12 rule 4 permits a
            click on**, besides an exact accessible-name match.
        success_markers: Evidence that the application went through — a confirmation element
            or the text an employer shows afterwards. Verification requires one of these;
            absence of failure is not success.
        error_markers: Evidence that the form rejected the submission, so a failed attempt
            is reported as failed rather than silently retried.
        next_step: The "continue" control on a multi-step flow, empty for single-page forms.
        cookie_banner: The consent-banner dismiss control, which otherwise overlays the form
            and swallows the first click.
        captcha_markers: Evidence of a captcha or bot challenge. Any match is an immediate
            escalation to manual review — never an attempt to solve it (golden rule #2).
        supports_auto_apply: Whether a provider using this pack may submit automatically.
            ``False`` for Workday and for the generic fallback, whose flows route to a human
            by design (``docs/CONTRACTS.md`` §9, golden rule #10).
    """

    name: str
    form_root: str
    field_container: str
    label: str
    input: str
    file_input: str
    submit: str
    success_markers: tuple[str, ...] = ()
    error_markers: tuple[str, ...] = ()
    next_step: str = ""
    cookie_banner: str = ""
    captcha_markers: tuple[str, ...] = ()
    supports_auto_apply: bool = True

    @property
    def css_success_markers(self) -> tuple[str, ...]:
        """The success markers that are CSS selectors, to be queried on the page."""
        return tuple(marker for marker in self.success_markers if is_css_selector(marker))

    @property
    def text_success_markers(self) -> tuple[str, ...]:
        """The success markers that are visible text, to be searched for in the page copy."""
        return tuple(marker for marker in self.success_markers if not is_css_selector(marker))

    @property
    def css_error_markers(self) -> tuple[str, ...]:
        """The error markers that are CSS selectors."""
        return tuple(marker for marker in self.error_markers if is_css_selector(marker))

    @property
    def text_error_markers(self) -> tuple[str, ...]:
        """The error markers that are visible text."""
        return tuple(marker for marker in self.error_markers if not is_css_selector(marker))

    @property
    def is_multi_step(self) -> bool:
        """Whether this ATS spreads its application across several pages."""
        return bool(self.next_step)

    def scoped(self, selector: str) -> str:
        """Return *selector* scoped inside :attr:`form_root`.

        Scoping is what keeps automation inside the application form. A page that also
        carries a newsletter signup, a search box or a cookie preferences dialog would
        otherwise offer controls that match ``input`` perfectly well.

        Args:
            selector: A selector relative to the form, possibly a comma-separated list.

        Returns:
            The scoped selector; *selector* unchanged when the pack declares no form root.
            Each alternative in a comma-separated list is scoped individually, because CSS
            gives ``,`` lower precedence than descendant combination and a naive prefix
            would scope only the first.
        """
        root = self.form_root.strip()
        target = (selector or "").strip()
        if not root or not target:
            return target
        alternatives = [part.strip() for part in target.split(_CSS_LIST_SEPARATOR) if part.strip()]
        return ", ".join(f"{root} {alternative}" for alternative in alternatives)

    def matches_success_text(self, text: str) -> str | None:
        """Return the success marker found in *text*, if any.

        Args:
            text: Visible page text, as extracted after a submit attempt.

        Returns:
            The first matching text marker, or ``None``. Matching is case-insensitive
            because employers change the capitalisation of their confirmation copy and a
            missed confirmation would be reported as an unverified submission.
        """
        haystack = (text or "").casefold()
        if not haystack:
            return None
        for marker in self.text_success_markers:
            if marker.casefold() in haystack:
                return marker
        return None

    def matches_error_text(self, text: str) -> str | None:
        """Return the error marker found in *text*, if any.

        Args:
            text: Visible page text, as extracted after a submit attempt.

        Returns:
            The first matching text marker, or ``None``.
        """
        haystack = (text or "").casefold()
        if not haystack:
            return None
        for marker in self.text_error_markers:
            if marker.casefold() in haystack:
                return marker
        return None

    def __repr__(self) -> str:
        """Return ``<SelectorPack name auto_apply=… steps=…>`` for logs."""
        return (
            f"<SelectorPack {self.name} auto_apply={self.supports_auto_apply} "
            f"multi_step={self.is_multi_step}>"
        )


# ======================================================================================
# Shared fragments
# ======================================================================================

#: Controls a human can type into or choose from. Hidden inputs carry CSRF tokens and
#: bookkeeping the form fills itself; submit and button inputs are clicked, never filled.
_FILLABLE_CONTROLS: Final[str] = (
    "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='image'])"
    ", textarea"
    ", select"
)

#: Bot-challenge widgets, in the three flavours that appear in front of ATS forms: Google
#: reCAPTCHA, hCaptcha and Cloudflare's Turnstile / interstitial. Any of them ends the
#: attempt and sends the application to a human (golden rule #2).
_COMMON_CAPTCHA_MARKERS: Final[tuple[str, ...]] = (
    "iframe[src*='recaptcha']",
    "iframe[title*='reCAPTCHA']",
    ".g-recaptcha",
    "#g-recaptcha-response",
    "iframe[src*='hcaptcha']",
    ".h-captcha",
    "iframe[src*='challenges.cloudflare.com']",
    "#cf-challenge-running",
    "[data-sitekey]",
)

#: Consent banners, which render above the form and eat the first click. The two managed
#: platforms below (OneTrust and Osano) cover the overwhelming majority of ATS-hosted pages;
#: the generic attributes catch the rest.
_COMMON_COOKIE_BANNER: Final[str] = (
    "#onetrust-accept-btn-handler"
    ", .onetrust-close-btn-handler"
    ", .osano-cm-accept-all"
    ", [data-testid='cookie-accept']"
    ", button[aria-label*='Accept cookies']"
)


# ======================================================================================
# Concrete packs
# ======================================================================================

#: Greenhouse — ``boards.greenhouse.io/<token>`` and the newer
#: ``job-boards.greenhouse.io/<token>``, plus the same form embedded in an employer's own
#: careers page. A single-page form: one ``#application_form``, one submit button, one
#: confirmation. Submission is supported (``docs/CONTRACTS.md`` §9).
GREENHOUSE: Final[SelectorPack] = SelectorPack(
    name=ATSProviderName.GREENHOUSE.value,
    form_root="#application_form, form#application-form, form[action*='/applications']",
    field_container=".field, .application-question, [class*='field--']",
    label="label, .application-label",
    input=_FILLABLE_CONTROLS,
    file_input="input[type='file']",
    submit="#submit_app, input[type='submit'][value*='Submit'], button[type='submit']",
    success_markers=(
        "Thank you for applying",
        "Your application has been submitted",
        "#application_confirmation",
        ".application-confirmation",
    ),
    error_markers=(
        ".field_with_errors",
        ".error",
        "[aria-invalid='true']",
        "There was a problem with your application",
        "This field is required",
    ),
    next_step="",
    cookie_banner=_COMMON_COOKIE_BANNER,
    captcha_markers=_COMMON_CAPTCHA_MARKERS,
)

#: Lever — ``jobs.lever.co/<company>/<posting-id>/apply``. Lever annotates its own controls
#: with ``data-qa`` attributes, which are markedly more stable than its class names; the
#: submit selector leads with the annotated one for exactly that reason. Submission is
#: supported (``docs/CONTRACTS.md`` §9).
LEVER: Final[SelectorPack] = SelectorPack(
    name=ATSProviderName.LEVER.value,
    form_root=".application-form, form[action*='/apply']",
    field_container=".application-question, .application-field, li.application-question",
    label=".application-label, label",
    input=_FILLABLE_CONTROLS,
    file_input="input[type='file'][name='resume'], input[type='file']",
    submit="[data-qa='submit-application-button'], button[type='submit']",
    success_markers=(
        ".application-confirmation",
        "[data-qa='application-confirmation']",
        "Thank you for applying",
        "Application submitted",
    ),
    error_markers=(
        ".application-error",
        ".error-message",
        "[aria-invalid='true']",
        "There was an error",
        "is required",
    ),
    next_step="",
    cookie_banner=_COMMON_COOKIE_BANNER,
    captcha_markers=_COMMON_CAPTCHA_MARKERS,
)

#: Ashby — ``jobs.ashbyhq.com/<org>/<posting-uuid>/application``. The form is a React
#: application whose class names are hashed per build, so every selector here matches on a
#: substring of the stable ``ashby-application-form-*`` prefix rather than on an exact class.
#: Submission is supported (``docs/CONTRACTS.md`` §9).
ASHBY: Final[SelectorPack] = SelectorPack(
    name=ATSProviderName.ASHBY.value,
    form_root="form[class*='ashby'], [class*='ashby-application-form'], form",
    field_container="[class*='ashby-application-form-field-entry'], [class*='_fieldEntry']",
    label="label, [class*='ashby-application-form-field-label']",
    input=_FILLABLE_CONTROLS,
    file_input="input[type='file']",
    submit="[class*='ashby-application-form-submit-button'], button[type='submit']",
    success_markers=(
        "Application submitted",
        "Thanks for applying",
        "[class*='ashby-application-form-success']",
        "[class*='ashby-job-posting-success']",
    ),
    error_markers=(
        "[class*='ashby-application-form-error']",
        "[aria-invalid='true']",
        "Please complete this field",
        "is required",
    ),
    next_step="",
    cookie_banner=_COMMON_COOKIE_BANNER,
    captcha_markers=_COMMON_CAPTCHA_MARKERS,
)

#: Workday — ``<tenant>.wd<N>.myworkdayjobs.com/<site>``. **Discovery only.** The selectors
#: are real and current (Workday annotates everything with ``data-automation-id``, which is
#: the most stable selector surface of any ATS here), and they are useful for field
#: discovery and blocker detection. They are *not* a submission path: applying to a Workday
#: posting requires creating a tenant account, verifying an email address and walking a
#: five-to-eight step wizard whose shape each employer configures independently. This pack
#: therefore declares ``supports_auto_apply=False`` and
#: :meth:`app.jobs.workday.WorkdayProvider.apply` raises
#: :class:`~app.jobs.base.UnsupportedFlowError` (``docs/CONTRACTS.md`` §9, golden rule #10).
WORKDAY: Final[SelectorPack] = SelectorPack(
    name=ATSProviderName.WORKDAY.value,
    form_root="[data-automation-id='jobApplicationForm'], form[data-automation-id]",
    field_container="[data-automation-id='formField'], [data-automation-id*='formField-']",
    label="[data-automation-id='formLabel'], label",
    input=_FILLABLE_CONTROLS + ", [data-automation-id='dropdown'] button",
    file_input="input[type='file'][data-automation-id='file-upload-input-ref']"
    ", input[type='file']",
    submit="[data-automation-id='submitButton'], [data-automation-id='wd-CommandButton']",
    success_markers=(
        "Your application has been submitted",
        "[data-automation-id='applicationSubmitted']",
        "[data-automation-id='confirmationPage']",
    ),
    error_markers=(
        "[data-automation-id='errorMessage']",
        "[data-automation-id='validationError']",
        "[data-automation-id='alert']",
    ),
    next_step="[data-automation-id='bottom-navigation-next-button']",
    cookie_banner=_COMMON_COOKIE_BANNER + ", [data-automation-id='legalNoticeAcceptButton']",
    captcha_markers=_COMMON_CAPTCHA_MARKERS + ("[data-automation-id='captcha']",),
    supports_auto_apply=False,
)

#: The fallback for an ATS this system does not recognise — a self-hosted careers page, or a
#: provider contributed through an entry point that ships no pack of its own.
#:
#: Conservative by construction. Every selector is scoped to a ``form``, only genuinely
#: fillable controls are matched, and ``supports_auto_apply`` is ``False``: an unidentified
#: form is described and escalated to a human, never submitted blind. Guessing right most of
#: the time is worse than asking, because the failure mode of guessing wrong is a real
#: application sent to a real employer with wrong answers in it (golden rule #2).
GENERIC: Final[SelectorPack] = SelectorPack(
    name="generic",
    form_root="form",
    field_container=".form-group, .field, .form-field, fieldset",
    label="label",
    input=_FILLABLE_CONTROLS,
    file_input="input[type='file']",
    submit="button[type='submit'], input[type='submit']",
    success_markers=(
        "thank you for applying",
        "application submitted",
        "application received",
        "we have received your application",
    ),
    error_markers=(
        "[aria-invalid='true']",
        ".error",
        ".is-invalid",
        "is required",
    ),
    next_step="",
    cookie_banner=_COMMON_COOKIE_BANNER,
    captcha_markers=_COMMON_CAPTCHA_MARKERS,
    supports_auto_apply=False,
)


#: Every pack, keyed by the provider name it serves. LinkedIn is deliberately absent: this
#: system does not automate anything on LinkedIn, so there is no form for it to describe
#: (``docs/CONTRACTS.md`` §9, golden rule #10). ``pack_for("linkedin")`` therefore resolves
#: to :data:`GENERIC`, which cannot submit either.
PACKS: Final[dict[str, SelectorPack]] = {
    GREENHOUSE.name: GREENHOUSE,
    LEVER.name: LEVER,
    ASHBY.name: ASHBY,
    WORKDAY.name: WORKDAY,
    GENERIC.name: GENERIC,
}


def pack_names() -> tuple[str, ...]:
    """Return the names of every available pack, sorted.

    Returns:
        The pack names, including ``"generic"``.
    """
    return tuple(sorted(PACKS))


def pack_for(provider: ATSProviderName | str) -> SelectorPack:
    """Return the selector pack for *provider*, falling back to :data:`GENERIC`.

    Never raises and never returns ``None``: the browser layer always has *some* pack to
    work with, and an unknown ATS gets the conservative one that cannot submit rather than
    an exception in the middle of an apply attempt.

    Args:
        provider: An :class:`~app.models.enums.ATSProviderName`, its string value, or a pack
            name. Matching is case-insensitive and whitespace-tolerant.

    Returns:
        The matching pack, or :data:`GENERIC` when the name is not recognised.

    Example:
        >>> pack_for(ATSProviderName.LEVER).submit
        "[data-qa='submit-application-button'], button[type='submit']"
        >>> pack_for("workday").supports_auto_apply
        False
        >>> pack_for("some-new-ats").name
        'generic'
    """
    key = provider.value if isinstance(provider, ATSProviderName) else str(provider or "")
    normalized = key.strip().lower()
    pack = PACKS.get(normalized)
    if pack is not None:
        return pack
    logger.debug("selectors.pack_fallback", requested=normalized or None, using=GENERIC.name)
    return GENERIC
