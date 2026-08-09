"""Form discovery and form filling — the code that types on a real person's behalf.

This module and :mod:`app.browser.verification` are the only places in ApplicantOS that act
irreversibly. A submitted application cannot be unsent, and a guessed answer is attached to
the user's name at a company they wanted to work for. Every design decision here follows from
that: **the correct failure mode is to stop and ask, never to guess** (golden rule #2).

Four rules are load-bearing, and each is implemented in exactly one place so it can be
audited:

**The kill switch lives in :meth:`AutoFiller.submit`, at the top, before anything else.** When
``dry_run`` is set — by the caller, by ``settings.dry_run`` — or when
``settings.auto_apply_enabled`` is off, or when the provider's :class:`SelectorPack` declares
that its ATS may not be automated at all, the method logs and returns ``False`` **without
locating a control and without clicking**. Not "clicks a disabled button"; not "clicks and
then checks". The DOM is never queried for a submit control on that path, which is what makes
the guarantee testable: assert that the page recorded zero clicks and zero lookups
(``docs/CONTRACTS.md`` §12 invariant 1, golden rule #3).

**Only a control named by the pack, or matched exactly by accessible name, is ever clicked.**
:meth:`AutoFiller._locate_submit` tries :attr:`~app.browser.selectors.SelectorPack.submit`
scoped to the form, then unscoped, then a whole-string ``get_by_role("button", name=…,
exact=True)`` against :data:`SUBMIT_ACCESSIBLE_NAMES`. There is deliberately no "first button
that looks like a submit" fallback: the cost of that heuristic firing on the wrong button is
a real application sent to a real employer (§12 invariant 4).

**Confidence decides, and low confidence never fills.** :meth:`AutoFiller.fill` asks the
resolver for an :class:`~app.ai.field_answer.AnswerPlan` per field and compares it against
``settings.min_answer_confidence``. Below the line the field is *not filled* and is returned
in the needs-review list. A value that is not one of ``field.options`` is refused for every
choice-shaped control, because a browser silently ignores an option that does not exist and
the resulting validation failure looks like a selector bug rather than a wrong answer.

**Work that is going to a human anyway is not started.** Essay overflow is checked *before*
the first field is filled, then blockers, then per-field confidence — cheapest and most
decisive first. A form with five essay questions costs one comparison, not five model calls.

Discovery is split deliberately between JavaScript and Python: :data:`DISCOVERY_SCRIPT` does
DOM access only — it collects raw descriptors and the *candidates* for each label — and Python
applies every policy decision (which label wins, which :class:`~app.models.enums.FieldKind`
applies, what counts as required, what counts as an essay, which controls are skipped). The
risky logic is therefore unit-testable against canned descriptors with no browser, and the
part that must run in a page is small enough to read in one sitting.

Playwright is never imported here. The module talks to a duck-typed session exposing the
surface ``docs/CONTRACTS.md`` §12 gives :class:`app.browser.playwright_runner.BrowserSession`
— ``page``, ``screenshot``, ``detect_blockers``, ``html`` — so the whole file imports, and its
safety guarantees are testable, on a machine with no browser installed.
"""

from __future__ import annotations

import asyncio
import html as html_module
import inspect
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from app.browser.selectors import GENERIC, SelectorPack
from app.config.settings import get_settings
from app.jobs.base import FormField
from app.models.enums import FieldKind, ReviewReason

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.ai.field_answer import AnswerPlan, FieldAnswerer
    from app.jobs.base import UserProfileDTO

__all__ = [
    "BLOCKER_CAPTCHA",
    "BLOCKER_CLOUDFLARE",
    "BLOCKER_LOGIN_WALL",
    "BLOCKER_MFA",
    "BLOCKER_UNKNOWN",
    "CHOICE_KINDS",
    "DISCOVERY_MARKER",
    "DISCOVERY_SCRIPT",
    "ESSAY_MIN_MAX_LENGTH",
    "ISO_DATE_FORMAT",
    "SCREENSHOT_AFTER_SUBMIT",
    "SCREENSHOT_BEFORE_SUBMIT",
    "SUBMIT_ACCESSIBLE_NAMES",
    "UPLOAD_MARKER",
    "UPLOAD_SCRIPT",
    "AutoFillError",
    "AutoFiller",
    "FieldResolver",
    "UploadFailedError",
    "expected_date_format",
    "normalize_date",
    "page_html",
    "page_text",
    "page_url",
    "stated_day_first",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: A textarea that advertises at least this many characters — or advertises no limit at all —
#: is counted as an essay question. Below it the control is a short free-text box ("what is
#: your preferred name?", "GitHub handle"), which is answered from the profile rather than
#: written. 200 is roughly two sentences: the point past which a form is asking the applicant
#: to *compose* something.
ESSAY_MIN_MAX_LENGTH: Final[int] = 200

#: Blocker identifiers returned by ``BrowserSession.detect_blockers`` (``docs/CONTRACTS.md``
#: §12). Named constants rather than bare strings so the mapping to a
#: :class:`~app.models.enums.ReviewReason` is greppable.
BLOCKER_CAPTCHA: Final[str] = "captcha"
BLOCKER_MFA: Final[str] = "mfa"
BLOCKER_LOGIN_WALL: Final[str] = "login_wall"
BLOCKER_CLOUDFLARE: Final[str] = "cloudflare"

#: Reported by :meth:`AutoFiller._detect_blockers` when the check itself failed. "We could not
#: look" is not "all clear", so it is a blocker like any other.
BLOCKER_UNKNOWN: Final[str] = "unknown"

#: Raised by :meth:`AutoFiller.fill` when the resolver refused a field because the form's own
#: label, helper text or options are a prompt injection (``docs/CONTRACTS.md`` §10b). A page
#: that carries an injection is not a page this system finishes on its own, so it joins the
#: blocker vocabulary rather than being counted as one more unanswered field.
BLOCKER_UNSAFE_CONTENT: Final[str] = "unsafe_content"

#: Blocker → review reason, in priority order. A Cloudflare interstitial is a bot challenge by
#: another name, so it maps to the same reason a reCAPTCHA does: a human opens the page and
#: finishes the application by hand.
_BLOCKER_REASONS: Final[tuple[tuple[str, ReviewReason], ...]] = (
    (BLOCKER_UNSAFE_CONTENT, ReviewReason.POLICY_BLOCK),
    (BLOCKER_CAPTCHA, ReviewReason.CAPTCHA),
    (BLOCKER_CLOUDFLARE, ReviewReason.CAPTCHA),
    (BLOCKER_MFA, ReviewReason.MFA),
    (BLOCKER_LOGIN_WALL, ReviewReason.LOGIN_REQUIRED),
    (BLOCKER_UNKNOWN, ReviewReason.AMBIGUOUS_ANSWER),
)

#: Accessible names a submit control may carry, matched **whole-string and case-sensitively**
#: through ``get_by_role("button", name=…, exact=True)``. This list is the entire fallback
#: available to :meth:`AutoFiller._locate_submit` when the pack's selector finds nothing;
#: substring matching is not used, because "Submit" is a substring of "Submit your details to
#: our newsletter" and clicking that is not a recoverable mistake (§12 invariant 4).
SUBMIT_ACCESSIBLE_NAMES: Final[tuple[str, ...]] = (
    "Submit Application",
    "Submit application",
    "Submit Application ",
    "Submit my application",
    "Submit My Application",
    "Submit",
    "SUBMIT",
    "Send Application",
    "Send application",
    "Submit and Apply",
)

#: Names given to the two proof-of-submission captures §12 invariant 3 requires around a real
#: click. The fill-time captures belong to the driver that owns the session.
SCREENSHOT_BEFORE_SUBMIT: Final[str] = "before_submit"
SCREENSHOT_AFTER_SUBMIT: Final[str] = "after_submit"

#: Poll interval while waiting for a success or error marker to appear after a click.
SUBMIT_POLL_INTERVAL_MS: Final[int] = 250

#: Ceiling on that wait, regardless of what ``settings.playwright_timeout_ms`` says. A
#: confirmation page that has not rendered in half a minute is not going to.
SUBMIT_MAX_WAIT_MS: Final[int] = 30_000

#: Floor on the same wait, so a mis-set timeout cannot turn "did it work?" into "we did not
#: look".
SUBMIT_MIN_WAIT_MS: Final[int] = 2_000

#: How long :meth:`AutoFiller.upload` keeps looking for evidence that the file attached. React
#: uploaders re-render asynchronously, so the first read after ``set_input_files`` legitimately
#: finds nothing.
UPLOAD_VERIFY_TIMEOUT_MS: Final[int] = 5_000

#: Poll interval for that check.
UPLOAD_VERIFY_INTERVAL_MS: Final[int] = 200

#: Field kinds whose submitted value must be one of ``field.options``, byte for byte.
CHOICE_KINDS: Final[frozenset[FieldKind]] = frozenset(
    {FieldKind.SELECT, FieldKind.MULTISELECT, FieldKind.RADIO, FieldKind.CHECKBOX}
)

#: Field kinds filled by typing into them.
_TEXTUAL_KINDS: Final[frozenset[FieldKind]] = frozenset(
    {
        FieldKind.TEXT,
        FieldKind.TEXTAREA,
        FieldKind.EMAIL,
        FieldKind.PHONE,
        FieldKind.URL,
        FieldKind.NUMBER,
    }
)

#: ``<input type=…>`` values that are never filled: hidden bookkeeping, and controls that are
#: clicked rather than typed into. A ``submit`` input reaching the filler would be a click on
#: the submit button by another name.
SKIPPED_INPUT_TYPES: Final[frozenset[str]] = frozenset(
    {"hidden", "submit", "button", "image", "reset"}
)

#: ``<input type=…>`` → :class:`~app.models.enums.FieldKind`. ``password`` maps to ``unknown``
#: on purpose: this system never types a password into a form, and an application flow that
#: asks for one is an account-creation flow that belongs to a human (golden rule #2).
_INPUT_KINDS: Final[dict[str, FieldKind]] = {
    "text": FieldKind.TEXT,
    "search": FieldKind.TEXT,
    "email": FieldKind.EMAIL,
    "tel": FieldKind.PHONE,
    "url": FieldKind.URL,
    "number": FieldKind.NUMBER,
    "date": FieldKind.DATE,
    "month": FieldKind.DATE,
    "week": FieldKind.DATE,
    "datetime-local": FieldKind.DATE,
    "file": FieldKind.FILE,
    "checkbox": FieldKind.CHECKBOX,
    "radio": FieldKind.RADIO,
    "password": FieldKind.UNKNOWN,
    "color": FieldKind.UNKNOWN,
    "range": FieldKind.UNKNOWN,
}

#: Values that mean "tick this box" and "leave it clear", for a standalone checkbox. Compared
#: against the case-folded resolved value.
_AFFIRMATIVE_VALUES: Final[frozenset[str]] = frozenset(
    {"yes", "y", "true", "1", "on", "agree", "i agree", "accept", "checked"}
)
_NEGATIVE_VALUES: Final[frozenset[str]] = frozenset(
    {"no", "n", "false", "0", "off", "disagree", "i do not agree", "decline", "unchecked"}
)

#: The two options offered for a standalone checkbox, so that the answerer — which requires a
#: choice field's value to be one of its options — has something to coerce to.
CHECKBOX_OPTIONS: Final[tuple[str, str]] = ("Yes", "No")

#: A ``<select>`` option that is a prompt rather than an answer. Dropped from ``options`` only
#: when its ``value`` is also empty, which is what distinguishes "— Select —" from a genuine
#: choice that happens to be worded like one. Leading dashes and asterisks are consumed first
#: because that is how the prompt is actually written: ``"-- Select --"``, ``"— Choose one —"``.
_PLACEHOLDER_OPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^[\s\-–—*]*(?:(?:please\s+)?(?:select|choose|pick)\b.*|n/?a|none|)$", re.IGNORECASE
)

#: The format an ``<input type="date">`` accepts, and the fallback target everywhere else.
ISO_DATE_FORMAT: Final[str] = "%Y-%m-%d"

#: Placeholder spellings → the ``strftime`` format they describe. Matched against the hint with
#: whitespace removed, longest key first, so ``"MM/DD/YYYY"`` wins over ``"M/D/YYYY"``.
_DATE_HINT_FORMATS: Final[dict[str, str]] = {
    "yyyy-mm-dd": "%Y-%m-%d",
    "yyyy/mm/dd": "%Y/%m/%d",
    "mm/dd/yyyy": "%m/%d/%Y",
    "mm-dd-yyyy": "%m-%d-%Y",
    "dd/mm/yyyy": "%d/%m/%Y",
    "dd-mm-yyyy": "%d-%m-%Y",
    "m/d/yyyy": "%m/%d/%Y",
    "d/m/yyyy": "%d/%m/%Y",
    "mm/yyyy": "%m/%Y",
    "mm/dd/yy": "%m/%d/%y",
    "dd/mm/yy": "%d/%m/%y",
}

#: Input spellings that cannot be misread. Numeric ``dd/mm`` vs ``mm/dd`` is deliberately
#: absent: it is handled separately, and refused outright when neither the value nor the
#: field states which convention it uses.
_UNAMBIGUOUS_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
)

#: A purely numeric date with two small leading components, the shape whose meaning depends
#: entirely on convention.
_NUMERIC_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4})$"
)

#: Highest month number, used to tell an unambiguous ``13/07/2024`` from an ambiguous
#: ``07/07/2024``.
_MAX_MONTH: Final[int] = 12

#: ``<script>`` / ``<style>`` bodies, removed before tags so their contents never become
#: "visible text" that a success marker could match.
_SCRIPT_STYLE_RE: Final[re.Pattern[str]] = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)

#: Any remaining markup tag.
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")

#: Runs of whitespace.
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Marker comment identifying :data:`DISCOVERY_SCRIPT` to a test double that dispatches on the
#: script it was handed. Harmless in a real page and much more robust than matching on a
#: fragment of the implementation.
DISCOVERY_MARKER: Final[str] = "applicantos:discover-fields"

#: The same, for :data:`UPLOAD_SCRIPT`.
UPLOAD_MARKER: Final[str] = "applicantos:uploaded-files"

#: **DOM access only.** Collects one raw descriptor per control inside the application form,
#: including every *candidate* for the control's label. It decides nothing: which candidate
#: wins, what kind the control is, whether it is required and whether it is skipped are all
#: resolved in Python, where they can be tested without a browser.
#:
#: The root is chosen as the match of ``args.root`` containing the most fillable controls
#: (ties resolved in document order), which is what keeps a newsletter signup or a site search
#: box from being mistaken for the application form on a page that has several ``<form>``
#: elements.
DISCOVERY_SCRIPT: Final[str] = (
    """
(args) => {
  /* """
    + DISCOVERY_MARKER
    + """ */
  const CONTROLS = "input, textarea, select";
  const MAX_DEPTH = 12;
  const rootSelector = (args && args.root) || "form";
  const containerSelector = (args && args.container) || "";
  const labelSelector = (args && args.label) || "label";

  const squash = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const query = (node, selector) => {
    if (!selector) return [];
    try { return Array.from(node.querySelectorAll(selector)); } catch (e) { return []; }
  };

  /* Text of a label, excluding any control nested inside it: the option texts of a wrapped
     <select> are not part of the question. */
  const ownText = (element) => {
    let out = "";
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === 3) { out += child.nodeValue; continue; }
        if (child.nodeType !== 1) continue;
        const tag = child.tagName.toLowerCase();
        if (tag === "input" || tag === "select" || tag === "textarea"
            || tag === "button" || tag === "option") continue;
        walk(child);
      }
    };
    walk(element);
    return squash(out);
  };

  const hidden = (el) => {
    if (el.type === "hidden" || el.hidden) return true;
    if (typeof el.checkVisibility === "function") {
      /* checkOpacity stays off: forms routinely hide the native control behind a styled
         one, and those fields are real and must still be discovered. */
      return !el.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true });
    }
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (!style) return false;
    return style.display === "none" || style.visibility === "hidden";
  };

  const cssPath = (el) => {
    const unique = (selector) => {
      try { return document.querySelectorAll(selector).length === 1; } catch (e) { return false; }
    };
    const escape = (value) =>
      window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/[^\\w-]/g, "\\\\$&");
    if (el.id && unique("#" + escape(el.id))) return "#" + escape(el.id);
    const tag = el.tagName.toLowerCase();
    if (el.name) {
      const byName = tag + '[name="' + String(el.name).replace(/["\\\\]/g, "\\\\$&") + '"]';
      if (unique(byName)) return byName;
    }
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < MAX_DEPTH; depth++) {
      if (node.id && unique("#" + escape(node.id))) { parts.unshift("#" + escape(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const twins = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (twins.length > 1) part += ":nth-of-type(" + (twins.indexOf(node) + 1) + ")";
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  };

  const labelledBy = (el) => {
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean);
    return squash(ids.map((id) => {
      const node = document.getElementById(id);
      return node ? ownText(node) : "";
    }).filter(Boolean).join(" "));
  };

  const heading = (el) => {
    const fieldset = el.closest ? el.closest("fieldset") : null;
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend) { const t = ownText(legend); if (t) return t; }
    }
    if (containerSelector) {
      let container = null;
      try { container = el.closest(containerSelector); } catch (e) { container = null; }
      if (container) {
        for (const candidate of query(container, labelSelector)) {
          const t = ownText(candidate);
          if (t) return t;
        }
      }
    }
    let node = el;
    for (let depth = 0; node && depth < MAX_DEPTH; depth++) {
      let sibling = node.previousElementSibling;
      while (sibling) {
        const tag = sibling.tagName.toLowerCase();
        if (/^h[1-6]$/.test(tag) || tag === "legend") {
          const t = ownText(sibling); if (t) return t;
        }
        const nested = sibling.querySelector
          ? sibling.querySelector("h1, h2, h3, h4, h5, h6, legend") : null;
        if (nested) { const t = ownText(nested); if (t) return t; }
        sibling = sibling.previousElementSibling;
      }
      node = node.parentElement;
    }
    return "";
  };

  const describe = (el) => {
    const tag = el.tagName.toLowerCase();
    const labels = el.labels ? Array.from(el.labels) : [];
    let labelFor = "";
    let labelWrap = "";
    for (const label of labels) {
      const text = ownText(label);
      if (!text) continue;
      if (label.getAttribute("for")) { if (!labelFor) labelFor = text; }
      else if (!labelWrap) labelWrap = text;
    }
    const options = tag === "select"
      ? Array.from(el.options || []).map((o) => ({
          value: o.value == null ? "" : String(o.value),
          label: squash(o.label || o.textContent || ""),
        }))
      : [];
    return {
      selector: cssPath(el),
      tag: tag,
      type: String(el.type || tag).toLowerCase(),
      name: el.name || "",
      id: el.id || "",
      value: el.value == null ? "" : String(el.value),
      disabled: !!el.disabled,
      readOnly: !!el.readOnly,
      hidden: hidden(el),
      required: !!el.required,
      ariaRequired: el.getAttribute("aria-required") === "true",
      maxLength: typeof el.maxLength === "number" && el.maxLength > 0 ? el.maxLength : null,
      multiple: !!el.multiple,
      labelFor: labelFor,
      labelWrap: labelWrap,
      ariaLabel: squash(el.getAttribute("aria-label") || ""),
      ariaLabelledBy: labelledBy(el),
      placeholder: squash(el.getAttribute("placeholder") || ""),
      title: squash(el.getAttribute("title") || ""),
      heading: heading(el),
      options: options,
    };
  };

  /* `matched` counts what the *pack's* root selector found, before any fallback, so Python
     can tell "the application form is here" from "we are looking at some other page". */
  const matches = query(document, rootSelector);
  let roots = matches;
  if (!roots.length) roots = [document.body || document.documentElement].filter(Boolean);
  let root = null;
  let best = -1;
  for (const candidate of roots) {
    const count = query(candidate, CONTROLS).length;
    if (count > best) { best = count; root = candidate; }
  }
  if (!root) return { root: "", matched: 0, controls: [] };
  return {
    root: cssPath(root),
    matched: matches.length,
    controls: query(root, CONTROLS).map(describe),
  };
}
"""
)

#: Reads back the filenames a file input is actually holding. Used to prove an upload
#: attached, because a silently failed upload submits an application with no resume on it —
#: strictly worse than not applying at all.
UPLOAD_SCRIPT: Final[str] = (
    """
(args) => {
  /* """
    + UPLOAD_MARKER
    + """ */
  let el = null;
  try { el = document.querySelector(args.selector); } catch (e) { el = null; }
  if (!el) return null;
  const files = el.files ? Array.from(el.files).map((f) => f.name) : [];
  const value = el.value ? String(el.value) : "";
  return { files: files, value: value };
}
"""
)


# ======================================================================================
# Errors
# ======================================================================================


class AutoFillError(Exception):
    """Base class for failures raised by this module."""


class UploadFailedError(AutoFillError):
    """A document was handed to a file input but could not be shown to have attached.

    Raised by :meth:`AutoFiller.upload` when neither the input's own ``files`` list nor the
    page's markup mentions the filename after the upload. The caller must route the
    application to review with
    :attr:`~app.models.enums.ReviewReason.FILE_UPLOAD_FAILED` rather than submit: an
    application that arrives with no resume attached is worse than one that never arrives,
    because the employer sees it and forms an impression.

    Attributes:
        selector: The file input that was targeted.
        path: The document that was offered to it.
    """

    def __init__(self, message: str, *, selector: str = "", path: Path | None = None) -> None:
        """Record what failed to attach and where.

        Args:
            message: Human-readable explanation, safe to log.
            selector: The file input's selector.
            path: The document that was offered.
        """
        super().__init__(message)
        self.selector = selector
        self.path = path


# ======================================================================================
# Page access helpers
# ======================================================================================


async def _resolve(candidate: Any) -> Any:
    """Return the value of a session member that may be an attribute, method or coroutine.

    ``BrowserSession`` is built in a separate phase against ``docs/CONTRACTS.md`` §12, which
    names ``html`` without fixing whether it is a property or an ``async`` method. Both
    spellings are legitimate, and reading the wrong one would fail in the middle of an apply
    attempt — the single worst moment for an ``AttributeError``. Ten lines here buy immunity
    to that.

    Args:
        candidate: The attribute value already read from the session.

    Returns:
        The resolved value: called if callable, awaited if awaitable.
    """
    value = candidate() if callable(candidate) else candidate
    if inspect.isawaitable(value):
        value = await value
    return value


def _strip_tags(markup: str) -> str:
    """Reduce markup to the text a human would read on the page.

    Args:
        markup: Serialised HTML.

    Returns:
        Whitespace-collapsed text with script and style bodies removed and entities decoded.
        Used only as a fallback for :func:`page_text`; matching a success marker against raw
        markup would let a string inside an attribute or an inline script masquerade as a
        confirmation message.
    """
    without_code = _SCRIPT_STYLE_RE.sub(" ", markup or "")
    without_tags = _TAG_RE.sub(" ", without_code)
    return _WHITESPACE_RE.sub(" ", html_module.unescape(without_tags)).strip()


async def page_html(session: Any) -> str:
    """Return the current page markup.

    Args:
        session: The browser session.

    Returns:
        The serialised DOM, or ``""`` when neither ``session.html`` nor ``page.content()``
        could produce one. An empty string is a safe answer here: it matches no success
        marker, so an unreadable page can never be mistaken for a confirmed submission.
    """
    for source in (
        getattr(session, "html", None),
        getattr(getattr(session, "page", None), "content", None),
    ):
        if source is None:
            continue
        try:
            value = await _resolve(source)
        except Exception as exc:
            logger.debug("autofill.html_unavailable", error=str(exc), error_type=type(exc).__name__)
            continue
        if isinstance(value, str) and value:
            return value
    return ""


async def page_text(session: Any) -> str:
    """Return the page's visible text.

    Args:
        session: The browser session.

    Returns:
        ``page.inner_text("body")`` when the page offers it, otherwise the markup with tags
        stripped, otherwise ``""``.
    """
    page = getattr(session, "page", None)
    inner_text = getattr(page, "inner_text", None)
    if inner_text is not None:
        try:
            value = await _resolve(lambda: inner_text("body"))
        except Exception as exc:
            logger.debug("autofill.text_unavailable", error=str(exc), error_type=type(exc).__name__)
        else:
            if isinstance(value, str) and value.strip():
                return _WHITESPACE_RE.sub(" ", value).strip()
    return _strip_tags(await page_html(session))


def page_url(session: Any) -> str:
    """Return the page's current URL.

    Args:
        session: The browser session.

    Returns:
        The URL, or ``""`` when the session does not expose one. Synchronous because
        Playwright's ``Page.url`` is a plain property.
    """
    for source in (
        getattr(getattr(session, "page", None), "url", None),
        getattr(session, "url", None),
    ):
        if isinstance(source, str) and source:
            return source
    return ""


# ======================================================================================
# Dates
# ======================================================================================


def expected_date_format(field: FormField, *, declared: str | None = None) -> str:
    """Determine the date format a control expects.

    Args:
        field: The discovered input.
        declared: The format recorded at discovery time, when there is one — an
            ``<input type="date">`` always wants ISO, whatever its placeholder says.

    Returns:
        A ``strftime`` format string. :data:`ISO_DATE_FORMAT` when the field advertises
        nothing, because that is what a native date control accepts and what every ATS in
        ``docs/CONTRACTS.md`` §9 stores internally.
    """
    if declared:
        return declared
    hint = (field.hint or "").casefold().replace(" ", "")
    for spelling in sorted(_DATE_HINT_FORMATS, key=len, reverse=True):
        if spelling in hint:
            return _DATE_HINT_FORMATS[spelling]
    return ISO_DATE_FORMAT


def stated_day_first(field: FormField) -> bool | None:
    """Return whether the control itself says the day comes before the month.

    Args:
        field: The discovered input.

    Returns:
        ``True`` for a ``DD/MM/YYYY`` placeholder, ``False`` for ``MM/DD/YYYY``, and ``None``
        when the control says nothing — which is the common case, and the one in which an
        ambiguous numeric date is refused rather than interpreted. Note that a native
        ``<input type="date">`` yields ``None`` too: ISO is the format it *accepts*, not a
        statement about how the applicant wrote the value being converted.
    """
    hint = (field.hint or "").casefold().replace(" ", "")
    for spelling in sorted(_DATE_HINT_FORMATS, key=len, reverse=True):
        if spelling in hint:
            fmt = _DATE_HINT_FORMATS[spelling]
            day, month = fmt.find("%d"), fmt.find("%m")
            return None if day < 0 or month < 0 else day < month
    return None


def normalize_date(value: str, target_format: str, *, day_first: bool | None = None) -> str | None:
    """Render *value* in *target_format*, or refuse.

    A wrong date on an application is a false statement about when the applicant can start,
    and ``05/06/2024`` means two different days on two sides of an ocean. So a numeric date
    whose components are both small enough to be either a day or a month is only accepted when
    the caller can say which convention applies — normally because the field's own placeholder
    said so (:func:`stated_day_first`). Otherwise this returns ``None`` and the field goes to a
    human (golden rule #2).

    A value already written in *target_format* is passed through unchanged rather than
    reinterpreted: it is in the shape the control asked for, and re-deriving its meaning could
    only introduce an error.

    Args:
        value: The resolved answer, in whatever shape the profile stored it.
        target_format: The ``strftime`` format the control expects.
        day_first: Whether an ambiguous ``a/b/YYYY`` puts the day first. ``None`` — the
            default — means the convention is unknown and such a value is refused.

    Returns:
        The reformatted date, or ``None`` when the value is not a date at all ("Immediately",
        "ASAP") or is ambiguous. Both are correct outcomes and route to review.
    """
    cleaned = _WHITESPACE_RE.sub(" ", (value or "").replace(",", " ")).strip()
    if not cleaned:
        return None

    for candidate_format in (target_format, *_UNAMBIGUOUS_DATE_FORMATS):
        try:
            # A calendar date carries no timezone — "3 March" is the same answer in every
            # zone — so the naive parse is the correct one and is reduced to a `date`
            # immediately, before anything can mistake it for an instant.
            parsed = datetime.strptime(cleaned, candidate_format).date()
        except ValueError:
            continue
        return parsed.strftime(target_format)

    numeric = _NUMERIC_DATE_RE.match(cleaned)
    if numeric is None:
        return None
    first, second, year = (int(part) for part in numeric.groups())

    if first > _MAX_MONTH and second <= _MAX_MONTH:
        day, month = first, second
    elif second > _MAX_MONTH and first <= _MAX_MONTH:
        month, day = first, second
    elif first <= _MAX_MONTH and second <= _MAX_MONTH:
        if day_first is None:
            return None
        day, month = (first, second) if day_first else (second, first)
    else:
        return None

    try:
        return date(year, month, day).strftime(target_format)
    except ValueError:
        return None


# ======================================================================================
# Field resolution
# ======================================================================================


class FieldResolver:
    """Adapter between :class:`~app.ai.field_answer.FieldAnswerer` and :class:`AutoFiller`.

    Deliberately thin (``docs/CONTRACTS.md`` §12): the answering policy — the explicit answers
    dictionary, the profile map, the EEO rules, the model — belongs to
    :mod:`app.ai.field_answer`, and the browser layer's only job is to ask and to obey the
    confidence it gets back. The one behaviour added here is that **a failure to answer is an
    answer**: an exception inside the answerer becomes a plan at confidence ``0.0``, so a model
    outage sends applications to the review queue rather than aborting a run.

    Usage::

        resolver = FieldResolver.for_user(profile, application.answers, llm=get_llm("fast"))
        plan = await resolver.resolve(field)
    """

    def __init__(self, answerer: FieldAnswerer) -> None:
        """Bind the resolver to an answerer.

        Args:
            answerer: The configured :class:`~app.ai.field_answer.FieldAnswerer`.
        """
        self.answerer = answerer

    @classmethod
    def for_user(
        cls,
        user: UserProfileDTO,
        answers: Mapping[str, Any] | None = None,
        *,
        llm: Any | None = None,
        knowledge: Any | None = None,
    ) -> FieldResolver:
        """Build a resolver for one applicant without importing the AI package by hand.

        Args:
            user: The applicant's answer sheet.
            answers: Values the user or a previous manual review already settled.
            llm: Model client for genuine free-text questions; ``None`` disables that path
                entirely, which is a supported configuration — essays simply go to a human.
            knowledge: Retriever grounding free-text answers in the applicant's own facts.

        Returns:
            The resolver.
        """
        from app.ai.field_answer import FieldAnswerer

        return cls(FieldAnswerer(user, dict(answers or {}), llm=llm, knowledge=knowledge))

    async def resolve(self, field: FormField) -> AnswerPlan:
        """Resolve one field into an :class:`~app.ai.field_answer.AnswerPlan`.

        Args:
            field: The discovered input.

        Returns:
            The plan. Confidence ``0.0`` — whether because the answerer declined or because it
            raised — means "ask a human", which is a correct outcome and not an error.
        """
        try:
            return await self.answerer.answer(field)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "autofill.resolver_failed",
                label=field.label,
                selector=field.selector,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            from app.ai.field_answer import (
                NO_ANSWER,
                SOURCE_NONE,
                AnswerPlan,
            )

            return AnswerPlan(
                field=field,
                value="",
                confidence=NO_ANSWER,
                source=SOURCE_NONE,
                reasoning=f"the answerer raised {type(exc).__name__}",
            )


# ======================================================================================
# The filler
# ======================================================================================


class AutoFiller:
    """Discovers, fills and (only under both switches) submits one application form.

    One instance per application attempt: it caches what discovery learned about the page —
    which element backs each radio option, which format each date control wants — and that
    knowledge is only valid for the DOM it was read from.

    The safety posture in one paragraph. :meth:`fill` refuses to start on a form with more
    essay questions than the user allows, refuses to continue when the session reports a
    captcha, an MFA prompt or a login wall, refuses to type any value the resolver is not
    confident about, and refuses any value that is not literally one of a choice control's
    options. :meth:`submit` refuses to look at the page at all unless both switches are open
    and the provider's ATS permits automation. Every refusal returns evidence rather than an
    exception, so the caller can build a review item that tells a human exactly what to
    finish.

    Attributes:
        session: The browser session, per ``docs/CONTRACTS.md`` §12.
        resolver: The field resolver, normally a :class:`FieldResolver`.
        pack: The provider's selector pack. The only source of a clickable submit selector.
        blockers: Whatever the last :meth:`fill` found via ``detect_blockers``.
        screenshots: Proof captures taken by this filler, in order.
        submit_attempted: Whether :meth:`submit` got past the kill switch.
        submit_clicked: Whether a submit control was actually clicked. Never ``True`` unless
            ``settings.auto_apply_enabled`` was on and no dry run was in effect.
    """

    def __init__(
        self,
        session: Any,
        resolver: Any,
        *,
        pack: SelectorPack,
        min_confidence: float | None = None,
        max_essays: int | None = None,
    ) -> None:
        """Bind the filler to a session, a resolver and one ATS's selectors.

        Args:
            session: A ``BrowserSession`` exposing ``page``, ``screenshot``,
                ``detect_blockers`` and ``html``.
            resolver: Anything with ``async resolve(field) -> AnswerPlan``.
            pack: The provider's :class:`~app.browser.selectors.SelectorPack`.
            min_confidence: Confidence a plan must reach before it is typed into the form.
                Defaults to ``settings.min_answer_confidence``.
            max_essays: Essay questions tolerated before the whole form goes to a human.
                Defaults to ``settings.max_essay_questions_before_review``.
        """
        settings = get_settings()
        self.session = session
        self.resolver = resolver
        self.pack = pack or GENERIC
        self._min_confidence = (
            float(settings.min_answer_confidence)
            if min_confidence is None
            else float(min_confidence)
        )
        self._max_essays = (
            int(settings.max_essay_questions_before_review)
            if max_essays is None
            else int(max_essays)
        )
        self.blockers: set[str] = set()
        self.screenshots: list[Path] = []
        self.submit_attempted: bool = False
        self.submit_clicked: bool = False
        self.form_root: str = ""
        #: Choice control selector → {option label: the selector of the element to click}.
        #: Populated for radio and checkbox groups only; its *absence* for a field is what
        #: identifies a real ``<select>``, which is chosen through ``select_option`` instead.
        self._choice_targets: dict[str, dict[str, str]] = {}
        #: Selectors of standalone checkboxes, which are ticked or cleared rather than chosen.
        self._toggles: set[str] = set()
        #: Selector → the ``strftime`` format that control expects.
        self._date_formats: dict[str, str] = {}

    # ----------------------------------------------------------------------------------
    # Configuration
    # ----------------------------------------------------------------------------------

    @property
    def min_confidence(self) -> float:
        """Confidence a plan must reach before this filler will type it into the form."""
        return self._min_confidence

    @property
    def max_essays(self) -> int:
        """Essay questions tolerated before the whole form is escalated to a human."""
        return self._max_essays

    @property
    def page(self) -> Any:
        """The Playwright page behind the session."""
        return getattr(self.session, "page", None)

    # ----------------------------------------------------------------------------------
    # Discovery
    # ----------------------------------------------------------------------------------

    async def discover_fields(self) -> list[FormField]:
        """Walk the application form and describe every control a human would fill in.

        Runs :data:`DISCOVERY_SCRIPT` once — one round trip, not one per control — and turns
        its raw descriptors into :class:`~app.jobs.base.FormField` instances. All of the
        policy is applied here rather than in the page:

        * **Label**, in the priority ``docs/CONTRACTS.md`` §12 fixes: ``<label for>``, a
          wrapping ``<label>``, ``aria-label``, ``aria-labelledby``, ``placeholder``, then the
          nearest preceding heading, legend or container label. A radio or checkbox *group*
          takes the heading instead, because the individual labels are its answers ("Yes",
          "No") and the heading is its question.
        * **Kind**, from the tag and the ``type`` attribute. ``password`` and anything
          unrecognised become :attr:`~app.models.enums.FieldKind.UNKNOWN`, which is never
          filled speculatively.
        * **Required**, from the ``required`` attribute, ``aria-required="true"``, or an
          asterisk in the label — the third being how most forms actually mark it.
        * **Options**, for selects and for radio/checkbox groups, with prompt options
          ("— Select —") dropped only when their value is empty too.

        Hidden, disabled, read-only and ``type=submit``/``button``/``image``/``reset``
        controls are skipped, and radios and checkboxes sharing a ``name`` are collapsed into
        one field.

        **A page whose pack root matched nothing is not an application form**, and is reported
        as no fields rather than described anyway. The script falls back to ``document.body``
        so that a form the root selector merely mis-describes is still readable, but that
        fallback is only safe when the page really is the form. It is routinely not: a
        Greenhouse posting whose employer embeds the board redirects to the employer's own
        careers site, where — measured on 2026-08-09 against a real posting — the fallback
        happily returned that site's "Search for a role" box as the application's only field.
        Answering a question nobody asked is the failure golden rule #2 exists to prevent, so
        an unmatched root escalates to a human instead (``docs/CONTRACTS.md`` §12).

        Returns:
            The fields in document order. Empty when the form could not be read or was not
            found, which the caller treats as a review item rather than as an empty form.
        """
        self._choice_targets.clear()
        self._toggles.clear()
        self._date_formats.clear()

        payload = await self._evaluate(
            DISCOVERY_SCRIPT,
            {
                "root": self.pack.form_root,
                "container": self.pack.field_container,
                "label": self.pack.label,
            },
        )
        if isinstance(payload, Mapping) and not _int(payload.get("matched")):
            logger.warning(
                "autofill.form_root_unmatched",
                pack=self.pack.name,
                form_root=self.pack.form_root,
                url=page_url(self.session) or None,
            )
            return []
        controls = self._controls_from(payload)
        if not controls:
            logger.warning(
                "autofill.no_controls_discovered",
                pack=self.pack.name,
                form_root=self.pack.form_root,
            )
            return []

        described = [(raw, _kind_for(raw)) for raw in controls if not self._is_skipped(raw)]
        # A checkbox is only part of a group when it shares its name with another checkbox;
        # a lone named checkbox is the "I agree to the terms" control, which is ticked or
        # cleared rather than chosen from. Radios are always a group, even a group of one.
        shared: dict[tuple[str, str], int] = {}
        for raw, kind in described:
            if kind in (FieldKind.RADIO, FieldKind.CHECKBOX) and _str(raw.get("name")):
                key = (_str(raw.get("type")), _str(raw.get("name")))
                shared[key] = shared.get(key, 0) + 1

        fields: list[FormField] = []
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        group_slot: dict[tuple[str, str], int] = {}

        for raw, kind in described:
            key = (_str(raw.get("type")), _str(raw.get("name")))
            groupable = kind is FieldKind.RADIO or (
                kind is FieldKind.CHECKBOX and shared.get(key, 0) > 1
            )
            if groupable and key[1]:
                if key not in grouped:
                    group_slot[key] = len(fields)
                    fields.append(_PLACEHOLDER_FIELD)
                    grouped[key] = []
                grouped[key].append(raw)
                continue
            field = self._single_field(raw, kind)
            if field is not None:
                fields.append(field)

        for key, members in grouped.items():
            group = self._group_field(members)
            if group is not None:
                fields[group_slot[key]] = group

        resolved = [field for field in fields if field is not _PLACEHOLDER_FIELD]
        self.form_root = _str(payload.get("root")) if isinstance(payload, Mapping) else ""
        logger.info(
            "autofill.fields_discovered",
            pack=self.pack.name,
            fields=len(resolved),
            required=sum(1 for field in resolved if field.required),
            essays=self.count_essays(resolved),
            form_root=self.form_root or None,
        )
        return resolved

    @staticmethod
    def count_essays(fields: Sequence[FormField]) -> int:
        """Count the free-writing questions on a form.

        Args:
            fields: The discovered fields.

        Returns:
            How many are textareas that either advertise no character limit or advertise at
            least :data:`ESSAY_MIN_MAX_LENGTH`. Compared against
            ``settings.max_essay_questions_before_review``: past that many the application is a
            writing assignment, and a human decides whether to take it on.
        """
        return sum(
            1
            for field in fields
            if field.kind is FieldKind.TEXTAREA
            and (field.max_length is None or field.max_length >= ESSAY_MIN_MAX_LENGTH)
        )

    # ----------------------------------------------------------------------------------
    # Filling
    # ----------------------------------------------------------------------------------

    async def fill(self, fields: Sequence[FormField]) -> tuple[list[AnswerPlan], list[FormField]]:
        """Fill everything that can be filled confidently, and report everything that cannot.

        The order of the checks is the order of their cost and their decisiveness:

        1. **Essay overflow, before anything is filled.** A form with more essays than the
           user tolerates is going to a human whatever else is true of it, so no field is
           resolved, no model is called and no key is pressed.
        2. **Blockers.** A captcha, an MFA prompt, a login wall or a Cloudflare interstitial
           means the page is not the application form, and every field goes to review.
        3. **Per-field confidence.** Below ``min_confidence`` the field is *not filled* and is
           returned for a human — required or not, because an optional question answered
           wrongly is still an answer submitted under the user's name. A field the resolver
           refused under ``docs/CONTRACTS.md`` §10b — its label, helper text or options are a
           prompt injection — additionally raises :data:`BLOCKER_UNSAFE_CONTENT`, so the whole
           form is reported with :attr:`~app.models.enums.ReviewReason.POLICY_BLOCK`.

        File inputs are skipped: they are handled by :meth:`upload`, which can verify that the
        document actually attached.

        Args:
            fields: The discovered fields.

        Returns:
            ``(filled, needs_review)``. ``filled`` holds the plans that were really written to
            the page — a plan whose interaction failed is reported as needing review, not as
            filled, because the page state and the plan would otherwise disagree.
        """
        pending = list(fields)
        essays = self.count_essays(pending)
        if essays > self._max_essays:
            logger.info(
                "autofill.essay_overflow",
                essays=essays,
                allowed=self._max_essays,
                fields=len(pending),
            )
            return [], pending

        blockers = await self._detect_blockers()
        self.blockers = blockers
        if blockers:
            logger.warning("autofill.blocked", blockers=sorted(blockers), fields=len(pending))
            return [], pending

        # Imported here rather than at module scope for the same reason `FieldResolver` does
        # it: the browser layer must import without the AI package having been touched.
        from app.ai.field_answer import SOURCE_BLOCKED as _SOURCE_BLOCKED

        filled: list[AnswerPlan] = []
        review: list[FormField] = []

        for field in pending:
            if field.kind is FieldKind.FILE:
                logger.debug("autofill.file_field_deferred", label=field.label)
                continue

            plan = await self.resolver.resolve(field)
            if plan.source == _SOURCE_BLOCKED:
                # §10b: the field's own text was refused, so the page is compromised rather
                # than merely hard. Recorded as a blocker so `review_reason_for` reports
                # POLICY_BLOCK instead of "unknown field", which would send the human looking
                # for a missing profile value that does not exist.
                logger.warning(
                    "autofill.unsafe_field",
                    label=field.label,
                    selector=field.selector,
                    reasoning=plan.reasoning,
                )
                self.blockers.add(BLOCKER_UNSAFE_CONTENT)
                review.append(field)
                continue

            if not plan.is_confident(self._min_confidence):
                logger.info(
                    "autofill.low_confidence",
                    label=field.label,
                    kind=field.kind.value,
                    required=field.required,
                    confidence=round(float(plan.confidence), 3),
                    threshold=self._min_confidence,
                    source=plan.source,
                )
                review.append(field)
                continue

            if await self._write(plan):
                filled.append(plan)
            else:
                review.append(field)

        logger.info(
            "autofill.filled",
            filled=len(filled),
            needs_review=len(review),
            unanswered_required=sum(1 for field in review if field.required),
        )
        return filled, review

    async def upload(self, field: FormField, path: Path) -> None:
        """Attach a document to a file input and **prove that it attached**.

        ``set_input_files`` succeeding means the call was accepted, not that the page took the
        file: an ATS that replaces the native input with a JavaScript uploader can swallow it
        silently, and the application then arrives with no resume on it. So the filename is
        read back — from the input's own ``files`` list first, then from the page's markup,
        which is where a custom uploader renders its chip — for up to
        :data:`UPLOAD_VERIFY_TIMEOUT_MS`.

        Args:
            field: The discovered file input.
            path: The document to attach.

        Raises:
            UploadFailedError: If the file does not exist, if the input rejected it, or if no
                evidence of the attachment appears. The caller routes the application to
                review with :attr:`~app.models.enums.ReviewReason.FILE_UPLOAD_FAILED`.
        """
        document = Path(path)
        if not document.is_file():
            raise UploadFailedError(
                f"document {document} does not exist", selector=field.selector, path=document
            )

        try:
            await self.page.locator(field.selector).set_input_files(str(document))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Re-raised as a typed failure so the caller routes to review with
            # FILE_UPLOAD_FAILED rather than seeing a Playwright error it cannot classify.
            raise UploadFailedError(
                f"set_input_files failed for {field.selector}: {type(exc).__name__}: {exc}",
                selector=field.selector,
                path=document,
            ) from exc

        if await self._upload_visible(field.selector, document.name):
            logger.info(
                "autofill.uploaded",
                label=field.label,
                selector=field.selector,
                filename=document.name,
            )
            return

        raise UploadFailedError(
            f"{document.name} was offered to {field.selector} but does not appear in the page "
            "afterwards; the application would be submitted without it",
            selector=field.selector,
            path=document,
        )

    # ----------------------------------------------------------------------------------
    # Submission — the kill switch
    # ----------------------------------------------------------------------------------

    async def submit(self, *, dry_run: bool) -> bool:
        """Submit the application, or refuse to.

        **This is the kill switch** (``docs/CONTRACTS.md`` §12 invariant 1, golden rule #3).
        The gate is the first thing in the method and it returns before the DOM is touched:
        no screenshot, no ``locator`` call, no ``get_by_role`` call, no click. That is what
        makes the guarantee provable — a test asserts that the page recorded no interactions
        at all, not merely that the return value was ``False``.

        Four independent conditions block a submission, and each defaults to the safe
        position:

        * the caller asked for a dry run;
        * ``settings.dry_run`` is on;
        * ``settings.auto_apply_enabled`` is off;
        * the provider's :class:`~app.browser.selectors.SelectorPack` declares that its ATS
          may not be automated at all — Workday's account-gated wizard, and every ATS this
          system does not recognise (golden rule #10).

        Past the gate: capture proof, locate the control **through the pack or by an exact
        accessible name only**, click it, wait for a success or error marker to appear, and
        capture proof again.

        Args:
            dry_run: Whether this attempt is a rehearsal. Narrowing only — a caller that asks
                for a dry run always gets one, whatever settings say.

        Returns:
            ``True`` only when a control was clicked and a success marker followed. Everything
            else — blocked, no control found, an error marker, nothing at all — is ``False``.
            :class:`app.browser.verification.ApplicationVerifier` remains the authority on
            whether the application really went through.
        """
        blocked = self._blocked_reason(dry_run=dry_run)
        if blocked is not None:
            logger.warning(
                "autofill.submit_blocked",
                reason=blocked,
                pack=self.pack.name,
                dry_run=bool(dry_run),
                clicked=False,
            )
            return False

        self.submit_attempted = True
        await self._screenshot(SCREENSHOT_BEFORE_SUBMIT)

        control = await self._locate_submit()
        if control is None:
            logger.warning(
                "autofill.submit_not_found",
                pack=self.pack.name,
                selector=self.pack.submit,
            )
            return False

        baseline = await self._present_error_marker()
        try:
            await control.click()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "autofill.submit_click_failed",
                pack=self.pack.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            await self._screenshot(SCREENSHOT_AFTER_SUBMIT)
            return False

        self.submit_clicked = True
        logger.info("autofill.submit_clicked", pack=self.pack.name)

        outcome = await self._await_outcome(baseline)
        await self._screenshot(SCREENSHOT_AFTER_SUBMIT)
        logger.info("autofill.submit_finished", pack=self.pack.name, succeeded=outcome)
        return outcome

    def review_reason_for(
        self,
        fields: Sequence[FormField],
        blockers: Iterable[str] | None = None,
        essays: int = 0,
    ) -> ReviewReason:
        """Explain, in one enum value, why this application needs a human.

        The order mirrors :meth:`fill`'s own: essay overflow is decided before the page is
        even inspected, blockers before any field is resolved, and the fields themselves last.
        A review item with the wrong reason sends a person looking in the wrong place, so the
        most decisive cause wins.

        Args:
            fields: The fields that could not be filled.
            blockers: Whatever ``detect_blockers`` reported; defaults to the last :meth:`fill`.
            essays: The essay count for the form.

        Returns:
            The reason: ``TOO_MANY_ESSAYS``, then ``CAPTCHA`` / ``MFA`` / ``LOGIN_REQUIRED``,
            then ``UNKNOWN_FIELD`` when a *required* field is unanswered, then
            ``LOW_CONFIDENCE`` when only optional ones are, and ``AMBIGUOUS_ANSWER`` when
            there is no evidence at all — which is still a question for a human, never a
            submission.
        """
        if essays > self._max_essays:
            return ReviewReason.TOO_MANY_ESSAYS

        reported = self.blockers if blockers is None else blockers
        detected = {str(blocker).strip().casefold() for blocker in reported}
        for name, reason in _BLOCKER_REASONS:
            if name in detected:
                return reason

        if any(field.required for field in fields):
            return ReviewReason.UNKNOWN_FIELD
        if fields:
            return ReviewReason.LOW_CONFIDENCE
        return ReviewReason.AMBIGUOUS_ANSWER

    # ----------------------------------------------------------------------------------
    # Internals — submission
    # ----------------------------------------------------------------------------------

    def _blocked_reason(self, *, dry_run: bool) -> str | None:
        """Return why this attempt may not submit, or ``None`` when it may.

        Args:
            dry_run: Whether the caller asked for a rehearsal.

        Returns:
            A short machine-readable reason for the log line, or ``None``. Evaluated with no
            page access whatsoever, which is what lets :meth:`submit` return before touching
            the DOM.
        """
        settings = get_settings()
        if dry_run:
            return "dry_run"
        if settings.dry_run:
            return "settings.dry_run"
        if not settings.auto_apply_enabled:
            return "auto_apply_disabled"
        if not self.pack.supports_auto_apply:
            return "provider_forbids_automation"
        return None

    async def _locate_submit(self) -> Any | None:
        """Find the one control this attempt is permitted to click.

        Three sources, in order, and no others: the pack's submit selector scoped inside the
        form, the same selector unscoped (for an ATS that renders its button outside the
        ``<form>``), and a whole-string accessible-name match from
        :data:`SUBMIT_ACCESSIBLE_NAMES`. §12 invariant 4 permits exactly these.

        Returns:
            A locator for the first match, or ``None``. ``None`` is a
            :attr:`~app.models.enums.ReviewReason.SUBMIT_NOT_FOUND` review item — never a
            reason to widen the search.
        """
        for selector in (_scoped_submit(self.pack), self.pack.submit):
            if not selector:
                continue
            locator = await self._first_match(lambda sel=selector: self.page.locator(sel))
            if locator is not None:
                logger.debug("autofill.submit_located", via="pack", selector=selector)
                return locator

        by_role = getattr(self.page, "get_by_role", None)
        if by_role is None:
            return None
        for name in SUBMIT_ACCESSIBLE_NAMES:
            locator = await self._first_match(
                lambda label=name: by_role("button", name=label, exact=True)
            )
            if locator is not None:
                logger.debug("autofill.submit_located", via="accessible_name", name=name)
                return locator
        return None

    async def _first_match(self, build: Any) -> Any | None:
        """Return the first element a locator factory matches, or ``None``.

        Args:
            build: A zero-argument callable producing a locator.

        Returns:
            ``locator.first`` when at least one element matched, else ``None``. Any failure —
            an invalid selector, a page that navigated away — counts as no match, because the
            alternative is an exception on the submission path.
        """
        try:
            locator = build()
            count = await _resolve(getattr(locator, "count", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("autofill.locator_failed", error=str(exc), error_type=type(exc).__name__)
            return None
        if not isinstance(count, int) or count < 1:
            return None
        return getattr(locator, "first", locator)

    async def _await_outcome(self, baseline: str | None) -> bool:
        """Wait for the page to say whether the submission worked.

        Args:
            baseline: The error marker that was already on the page *before* the click, if
                any. Error markers are routinely present on an untouched form —
                ``[aria-invalid='true']`` on a pristine required field, an empty ``.error``
                container — so only a marker that was not there before is evidence of a
                rejection.

        Returns:
            ``True`` on a success marker, ``False`` on a newly appeared error marker and
            ``False`` on timeout. Silence is not consent: an unconfirmed submission is a
            review item.
        """
        settings = get_settings()
        budget_ms = min(
            max(int(settings.playwright_timeout_ms), SUBMIT_MIN_WAIT_MS), SUBMIT_MAX_WAIT_MS
        )
        deadline = time.monotonic() + budget_ms / 1000.0

        while True:
            success = await self._present_success_marker()
            if success is not None:
                logger.info("autofill.submit_success_marker", marker=success)
                return True
            error = await self._present_error_marker()
            if error is not None and error != baseline:
                logger.warning("autofill.submit_error_marker", marker=error)
                return False
            if time.monotonic() >= deadline:
                logger.warning("autofill.submit_outcome_unknown", waited_ms=budget_ms)
                return False
            await asyncio.sleep(SUBMIT_POLL_INTERVAL_MS / 1000.0)

    async def _present_success_marker(self) -> str | None:
        """Return the success marker currently on the page, if any."""
        css = await self._first_present(self.pack.css_success_markers)
        if css is not None:
            return css
        return self.pack.matches_success_text(await page_text(self.session))

    async def _present_error_marker(self) -> str | None:
        """Return the error marker currently on the page, if any."""
        css = await self._first_present(self.pack.css_error_markers)
        if css is not None:
            return css
        return self.pack.matches_error_text(await page_text(self.session))

    async def _first_present(self, markers: Sequence[str]) -> str | None:
        """Return the first CSS marker that matches something on the page.

        Args:
            markers: CSS selectors from the pack.

        Returns:
            The matching marker, or ``None``. Markers are queried unscoped: a confirmation
            page has usually replaced the form entirely, so scoping to it would find nothing.
        """
        for marker in markers:
            if await self._first_match(lambda sel=marker: self.page.locator(sel)) is not None:
                return marker
        return None

    # ----------------------------------------------------------------------------------
    # Internals — writing values
    # ----------------------------------------------------------------------------------

    async def _write(self, plan: AnswerPlan) -> bool:
        """Write one confident plan into the page.

        Args:
            plan: A plan that has already cleared the confidence threshold.

        Returns:
            Whether the value really reached the control. ``False`` for an unsupported kind,
            a value that is not among a choice control's options, an unparseable date, or a
            Playwright failure — all of which send the field to a human instead.
        """
        field = plan.field
        value = plan.value

        if field.kind in CHOICE_KINDS and value not in field.options:
            logger.warning(
                "autofill.value_not_offered",
                label=field.label,
                kind=field.kind.value,
                value=value,
                options=list(field.options)[:12],
            )
            return False

        try:
            if field.kind in _TEXTUAL_KINDS:
                return await self._write_text(field, value)
            if field.kind is FieldKind.DATE:
                return await self._write_date(field, value)
            if field.kind in (FieldKind.SELECT, FieldKind.MULTISELECT):
                return await self._write_choice(field, value, select=True)
            if field.kind in (FieldKind.RADIO, FieldKind.CHECKBOX):
                return await self._write_choice(field, value, select=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "autofill.write_failed",
                label=field.label,
                kind=field.kind.value,
                selector=field.selector,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

        logger.info(
            "autofill.kind_not_fillable",
            label=field.label,
            kind=field.kind.value,
            selector=field.selector,
        )
        return False

    async def _write_text(self, field: FormField, value: str) -> bool:
        """Type a value into a text, textarea, email, phone, url or number control."""
        text = value[: field.max_length] if field.max_length else value
        if len(text) < len(value):
            logger.info(
                "autofill.value_truncated",
                label=field.label,
                characters=len(value),
                limit=field.max_length,
            )
        await self.page.locator(field.selector).fill(text)
        logger.debug("autofill.text_filled", label=field.label, characters=len(text))
        return True

    async def _write_date(self, field: FormField, value: str) -> bool:
        """Type a date, in the format the control expects, or refuse."""
        target = expected_date_format(field, declared=self._date_formats.get(field.selector))
        normalized = normalize_date(value, target, day_first=stated_day_first(field))
        if normalized is None:
            logger.warning(
                "autofill.date_unparseable",
                label=field.label,
                value=value,
                expected_format=target,
            )
            return False
        await self.page.locator(field.selector).fill(normalized)
        logger.debug("autofill.date_filled", label=field.label, value=normalized)
        return True

    async def _write_choice(self, field: FormField, value: str, *, select: bool) -> bool:
        """Choose an option on a select, a radio group or a checkbox.

        Args:
            field: The discovered control.
            value: The option to choose, already verified to be one the control offers.
            select: Whether the caller reached here through a ``<select>``-shaped kind.

        Returns:
            Whether the option was chosen. A radio or checkbox group whose per-option elements
            were not recorded at discovery time is refused rather than guessed at: clicking the
            wrong radio is exactly the failure this module exists to prevent.
        """
        if field.selector in self._toggles:
            folded = value.strip().casefold()
            locator = self.page.locator(field.selector)
            if folded in _AFFIRMATIVE_VALUES:
                await locator.check()
            elif folded in _NEGATIVE_VALUES:
                await locator.uncheck()
            else:
                logger.warning("autofill.checkbox_value_unclear", label=field.label, value=value)
                return False
            logger.debug("autofill.checkbox_set", label=field.label, value=value)
            return True

        targets = self._choice_targets.get(field.selector)
        if targets is not None:
            option_selector = targets.get(value)
            if option_selector is None:
                logger.warning(
                    "autofill.option_element_missing",
                    label=field.label,
                    value=value,
                    known=list(targets)[:12],
                )
                return False
            await self.page.locator(option_selector).check()
            logger.debug("autofill.option_checked", label=field.label, value=value)
            return True

        if not select:
            logger.warning(
                "autofill.choice_targets_unknown",
                label=field.label,
                selector=field.selector,
                kind=field.kind.value,
            )
            return False

        await self.page.locator(field.selector).select_option(label=value)
        logger.debug("autofill.option_selected", label=field.label, value=value)
        return True

    # ----------------------------------------------------------------------------------
    # Internals — page access
    # ----------------------------------------------------------------------------------

    async def _evaluate(self, script: str, argument: Any) -> Any:
        """Run one script in the page.

        Args:
            script: The JavaScript arrow function.
            argument: Its single argument.

        Returns:
            Whatever the script returned, or ``None`` when the page could not run it —
            which discovery reports as "no fields", and therefore as a review item.
        """
        page = self.page
        if page is None or not hasattr(page, "evaluate"):
            logger.warning("autofill.page_unavailable")
            return None
        try:
            return await page.evaluate(script, argument)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "autofill.evaluate_failed", error=str(exc), error_type=type(exc).__name__
            )
            return None

    async def _detect_blockers(self) -> set[str]:
        """Ask the session what is standing between us and the form.

        Returns:
            The blocker names. A detection failure reports :data:`BLOCKER_UNKNOWN` rather than
            an empty set, so that an unreadable page routes to a human instead of being filled
            blind — "we could not look" is not "all clear".
        """
        detector = getattr(self.session, "detect_blockers", None)
        if detector is None:
            return set()
        try:
            found = await _resolve(detector)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "autofill.blocker_detection_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return {BLOCKER_UNKNOWN}
        return {str(item) for item in (found or ()) if str(item).strip()}

    async def _screenshot(self, name: str) -> None:
        """Capture proof, and never let the capture itself end an attempt.

        Args:
            name: Artifact name, per ``BrowserSession.screenshot``.
        """
        capture = getattr(self.session, "screenshot", None)
        if capture is None:
            return
        try:
            path = await _resolve(lambda: capture(name))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "autofill.screenshot_failed",
                name=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        if isinstance(path, (str, Path)):
            self.screenshots.append(Path(path))

    async def _upload_visible(self, selector: str, filename: str) -> bool:
        """Poll for evidence that *filename* attached to *selector*.

        Args:
            selector: The file input.
            filename: The document's name.

        Returns:
            Whether the input reports the file, or the page's markup mentions it, before
            :data:`UPLOAD_VERIFY_TIMEOUT_MS` elapses.
        """
        needle = filename.casefold()
        stem = Path(filename).stem.casefold()
        deadline = time.monotonic() + UPLOAD_VERIFY_TIMEOUT_MS / 1000.0

        while True:
            payload = await self._evaluate(UPLOAD_SCRIPT, {"selector": selector})
            if isinstance(payload, Mapping):
                names = [str(item).casefold() for item in (payload.get("files") or ())]
                if any(needle in item or item in needle for item in names):
                    return True
                if needle in str(payload.get("value") or "").casefold():
                    return True

            markup = (await page_html(self.session)).casefold()
            if needle in markup or (stem and stem in markup):
                return True

            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(UPLOAD_VERIFY_INTERVAL_MS / 1000.0)

    # ----------------------------------------------------------------------------------
    # Internals — descriptor to FormField
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _controls_from(payload: Any) -> list[dict[str, Any]]:
        """Normalise whatever :data:`DISCOVERY_SCRIPT` returned into a list of descriptors."""
        controls = payload.get("controls") if isinstance(payload, Mapping) else payload
        if not isinstance(controls, (list, tuple)):
            return []
        return [dict(item) for item in controls if isinstance(item, Mapping)]

    @staticmethod
    def _is_skipped(raw: Mapping[str, Any]) -> bool:
        """Return whether a control must not be filled at all.

        Args:
            raw: One descriptor.

        Returns:
            ``True`` for hidden, disabled, read-only controls and for the input types that are
            clicked or that carry the form's own bookkeeping.
        """
        if raw.get("hidden") or raw.get("disabled") or raw.get("readOnly"):
            return True
        return _str(raw.get("type")) in SKIPPED_INPUT_TYPES

    def _single_field(self, raw: Mapping[str, Any], kind: FieldKind) -> FormField | None:
        """Build one :class:`~app.jobs.base.FormField` from one descriptor.

        Args:
            raw: The descriptor.
            kind: Its resolved kind.

        Returns:
            The field, or ``None`` when the descriptor carries no usable selector.
        """
        selector = _str(raw.get("selector"))
        if not selector:
            return None

        label = _first_label(raw) or _str(raw.get("name")) or selector
        options: list[str] = []
        if kind in (FieldKind.SELECT, FieldKind.MULTISELECT):
            options = _select_options(raw)
        elif kind is FieldKind.CHECKBOX:
            options = list(CHECKBOX_OPTIONS)
            self._toggles.add(selector)

        if kind is FieldKind.DATE and _str(raw.get("type")) == "date":
            self._date_formats[selector] = ISO_DATE_FORMAT

        return FormField(
            selector=selector,
            label=label,
            kind=kind,
            required=_required(raw, label),
            options=options,
            max_length=_int(raw.get("maxLength")),
            hint=_hint(raw, label),
        )

    def _group_field(self, members: Sequence[Mapping[str, Any]]) -> FormField | None:
        """Collapse radios or checkboxes sharing a ``name`` into a single field.

        The group's *question* is its legend, container label or preceding heading; the
        individual labels are its *answers*. Each option's own element selector is recorded in
        :attr:`_choice_targets` so that filling can click precisely the right one — and a
        group whose options could not be recorded is refused at fill time rather than guessed.

        Args:
            members: The descriptors sharing a name.

        Returns:
            The grouped field, or ``None`` when nothing usable remains.
        """
        first = members[0]
        name = _str(first.get("name"))
        kind = FieldKind.RADIO if _str(first.get("type")) == "radio" else FieldKind.MULTISELECT
        tag = _str(first.get("tag")) or "input"
        control_type = _str(first.get("type")) or "radio"
        group_selector = f'{tag}[type="{control_type}"][name="{_css_escape(name)}"]'

        options: list[str] = []
        targets: dict[str, str] = {}
        for member in members:
            selector = _str(member.get("selector"))
            option = (
                _str(member.get("labelFor"))
                or _str(member.get("labelWrap"))
                or _str(member.get("ariaLabel"))
                or _str(member.get("value"))
            )
            if not selector or not option or option in targets:
                continue
            options.append(option)
            targets[option] = selector

        if not options:
            logger.debug("autofill.group_without_options", name=name)
            return None

        label = _str(first.get("heading")) or _str(first.get("ariaLabelledBy")) or name
        self._choice_targets[group_selector] = targets
        return FormField(
            selector=group_selector,
            label=label,
            kind=kind,
            required=any(_required(member, label) for member in members),
            options=options,
            max_length=None,
            hint=_str(first.get("title")) or None,
        )


# ======================================================================================
# Descriptor helpers
# ======================================================================================

#: Slot marker used while radio and checkbox groups are being assembled in document order.
#: Never returned to a caller.
_PLACEHOLDER_FIELD: Final[FormField] = FormField(selector="", label="", kind=FieldKind.UNKNOWN)


def _str(value: Any) -> str:
    """Return *value* as a trimmed string, or ``""``."""
    if value is None:
        return ""
    return (value if isinstance(value, str) else str(value)).strip()


def _int(value: Any) -> int | None:
    """Return *value* as a positive ``int``, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _css_escape(value: str) -> str:
    """Escape a string for use inside a double-quoted CSS attribute selector."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _scoped_submit(pack: SelectorPack) -> str:
    """Return the pack's submit selector, correctly scoped inside its form.

    :meth:`~app.browser.selectors.SelectorPack.scoped` prefixes each alternative of the
    *selector* with the whole ``form_root`` string. When the root is itself a comma-separated
    list — as it is for Greenhouse, Lever and Ashby — the result is a selector list whose
    leading alternatives are the *form roots themselves*, so ``.first`` can resolve to the
    ``<form>`` element instead of to the submit button. On any other selector that is a
    curiosity; on this one it is the difference between pressing submit and pressing nothing,
    so the cross product is built explicitly here.

    Args:
        pack: The provider's selector pack.

    Returns:
        ``"<root> <submit>"`` for every combination of root and submit alternative, joined
        into one selector list; the bare submit selector when the pack declares no root; and
        ``""`` when it declares no submit control.
    """
    targets = [part.strip() for part in pack.submit.split(",") if part.strip()]
    if not targets:
        return ""
    roots = [part.strip() for part in pack.form_root.split(",") if part.strip()]
    if not roots:
        return ", ".join(targets)
    return ", ".join(f"{root} {target}" for root in roots for target in targets)


def _kind_for(raw: Mapping[str, Any]) -> FieldKind:
    """Map a descriptor's tag and type onto a :class:`~app.models.enums.FieldKind`.

    Args:
        raw: One descriptor.

    Returns:
        The kind. Anything unrecognised — including ``password`` — becomes
        :attr:`~app.models.enums.FieldKind.UNKNOWN`, which is escalated rather than filled.
    """
    tag = _str(raw.get("tag")).lower()
    if tag == "textarea":
        return FieldKind.TEXTAREA
    if tag == "select":
        return FieldKind.MULTISELECT if raw.get("multiple") else FieldKind.SELECT
    return _INPUT_KINDS.get(_str(raw.get("type")).lower(), FieldKind.UNKNOWN)


def _first_label(raw: Mapping[str, Any]) -> str:
    """Pick a control's label from its candidates, in the priority §12 fixes.

    Args:
        raw: One descriptor.

    Returns:
        The first non-empty of: ``<label for>``, a wrapping ``<label>``, ``aria-label``,
        ``aria-labelledby``, ``placeholder``, the nearest preceding heading, then ``title`` as
        a last resort. An empty result is possible and is left for the caller to replace with
        the control's name, because a field with no label at all is one a human must look at.
    """
    for key in (
        "labelFor",
        "labelWrap",
        "ariaLabel",
        "ariaLabelledBy",
        "placeholder",
        "heading",
        "title",
    ):
        candidate = _str(raw.get(key))
        if candidate:
            return candidate
    return ""


def _required(raw: Mapping[str, Any], label: str) -> bool:
    """Decide whether a control must be answered.

    Args:
        raw: One descriptor.
        label: Its resolved label.

    Returns:
        ``True`` for the ``required`` attribute, for ``aria-required="true"``, or for an
        asterisk in the label — the last being how most ATS forms actually mark obligation,
        and the one a purely attribute-based check would miss.
    """
    return bool(raw.get("required") or raw.get("ariaRequired") or "*" in label)


def _hint(raw: Mapping[str, Any], label: str) -> str | None:
    """Return the helper text shown beside a control, when it differs from its label."""
    for key in ("placeholder", "title"):
        candidate = _str(raw.get(key))
        if candidate and candidate != label:
            return candidate
    return None


def _select_options(raw: Mapping[str, Any]) -> list[str]:
    """Return the answers a ``<select>`` offers.

    Args:
        raw: One descriptor.

    Returns:
        The option labels, with prompts ("— Select —", "Please choose…") dropped **only** when
        their value is empty as well. That pairing is what tells a prompt apart from a genuine
        answer that happens to be worded like one, and dropping a real answer would make it
        unreachable to the answerer, which refuses any value not among the options.
    """
    options: list[str] = []
    for entry in raw.get("options") or ():
        if not isinstance(entry, Mapping):
            continue
        label = _str(entry.get("label"))
        if not label:
            continue
        if not _str(entry.get("value")) and _PLACEHOLDER_OPTION_RE.match(label):
            continue
        if label not in options:
            options.append(label)
    return options
