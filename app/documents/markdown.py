"""The ``markdown`` template plugin — plain-text resumes, and the helpers every renderer shares.

Markdown is the least glamorous renderer in :mod:`app.documents` and the one that gets used
most often. Three jobs:

**Web forms.** A depressing share of application forms still contain a "paste your resume"
textarea. Pasting a PDF's text layer into one produces ligature damage and stray hyphens;
pasting this produces something a human wrote.

**Diffing.** ``ResumeVersion.content_json`` is kept forever (golden rule #6) and the desktop
app lets the user compare two tailored versions of the same resume. Diffing JSON is unusable
and diffing PDFs is impossible; diffing this is a two-line change highlighted in green. That
is why the output is line-stable — one bullet per line, blank lines only between blocks, no
reflowing — so a diff shows the bullets that actually changed and nothing else.

**A guaranteed floor.** Nothing here imports anything outside the standard library, so the
``markdown`` template renders on a machine with no LaTeX, no LibreOffice, no WeasyPrint and
no reportlab. There is always *some* way to get a resume out of ApplicantOS.

Because this module has no third-party imports at all, it is also the safe home for the
pieces the other three renderers share and would otherwise each re-implement:

* :func:`contact_items` — *which* contact details a header shows and *in what order*. The
  LaTeX, HTML and DOCX renderers consume it and then format the result in their own idiom,
  so the same resume always shows the same details in the same sequence.
* :func:`resolve_render_settings` — validation and clamping of the ``font_size`` /
  ``margin_in`` options that ``render_resume``'s shrink loop turns.
* :func:`estimated_page_count` and :func:`write_text` — the two things every renderer does
  on the way out.

Depending on this module can never drag an optional package into another renderer's import
path, which is the property that makes the arrangement safe.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.documents.models import (
    Contact,
    CoverLetterDocument,
    ResumeDocument,
    ResumeEntry,
)
from app.documents.renderer import DocumentRenderError, RenderResult, TemplatePlugin
from app.models.enums import PluginKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "CONTACT_SEPARATOR",
    "DEFAULT_CLOSING",
    "DEFAULT_FONT_SIZE_PT",
    "DEFAULT_MARGIN_IN",
    "DEFAULT_SKILLS_HEADING",
    "DEFAULT_SUMMARY_HEADING",
    "LETTER_HEIGHT_IN",
    "LETTER_WIDTH_IN",
    "LINK_DISPLAY_NAMES",
    "MARKDOWN_ENGINE",
    "MARKDOWN_FORMAT",
    "PREFERRED_LINK_ORDER",
    "SKILLS_HEADING_META_KEY",
    "SUMMARY_HEADING_META_KEY",
    "ContactItem",
    "MarkdownTemplate",
    "RenderSettings",
    "contact_items",
    "cover_letter_markdown",
    "escape_markdown",
    "estimated_page_count",
    "generated_heading",
    "pretty_url",
    "recipient_lines",
    "require_format",
    "resolve_render_settings",
    "resume_markdown",
    "write_text",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Format identifier this plugin produces.
MARKDOWN_FORMAT: Final[str] = "md"

#: Reported as ``RenderResult.engine`` — there is no external engine involved.
MARKDOWN_ENGINE: Final[str] = "markdown"

#: Separator between contact details on the header line. A middle dot reads as a separator
#: rather than as punctuation, and survives copy-paste into a plain textarea.
CONTACT_SEPARATOR: Final[str] = " · "

#: Characters that change Markdown's meaning wherever they appear in body text.
INLINE_SPECIALS: Final[str] = "\\`*_[]<>"

#: Line-leading sequences that would turn a bullet's text into a new block. Deliberately
#: narrow: ``-``, ``+`` and ``.`` are only structural at the start of a line, and escaping
#: every one of them would render "end-to-end latency, 12.4ms" unreadable for the sake of a
#: syntax that was never going to trigger. ``>`` and ``*`` are absent because
#: :data:`INLINE_SPECIALS` has already escaped them by the time this pattern runs.
_LEADING_BLOCK: Final[re.Pattern[str]] = re.compile(r"^(\s*)(#|[-+]\s|\d+[.)]\s)")

#: URL schemes printed verbatim rather than being prefixed with ``https://``.
_KNOWN_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://", "mailto:", "tel:")

#: Link labels hoisted to the front of the contact line, in this order.
PREFERRED_LINK_ORDER: Final[tuple[str, ...]] = ("github", "linkedin", "portfolio", "website")

#: Human-readable names for the conventional link labels. Anything not listed is title-cased.
LINK_DISPLAY_NAMES: Final[dict[str, str]] = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "linkedin": "LinkedIn",
    "portfolio": "Portfolio",
    "website": "Website",
    "blog": "Blog",
    "twitter": "Twitter",
    "x": "X",
    "stackoverflow": "Stack Overflow",
    "scholar": "Google Scholar",
    "orcid": "ORCID",
    "youtube": "YouTube",
}

#: Heading printed above :attr:`~app.documents.models.ResumeDocument.summary`.
DEFAULT_SUMMARY_HEADING: Final[str] = "Summary"

#: Heading printed above :attr:`~app.documents.models.ResumeDocument.skills_line`.
DEFAULT_SKILLS_HEADING: Final[str] = "Skills"

#: ``ResumeDocument.meta`` keys a caller may set to override the two generated headings.
SUMMARY_HEADING_META_KEY: Final[str] = "summary_heading"
SKILLS_HEADING_META_KEY: Final[str] = "skills_heading"

#: Sign-off used when the caller supplies none.
DEFAULT_CLOSING: Final[str] = "Sincerely,"

#: Defaults matching ``docs/CONTRACTS.md`` §11's shrink ladder (10.5 → 10 → 9.5pt,
#: 0.5 → 0.45 → 0.4in).
DEFAULT_FONT_SIZE_PT: Final[float] = 10.5
DEFAULT_MARGIN_IN: Final[float] = 0.5

#: Bounds on ``options["font_size"]``. Below the floor the text stops being readable on
#: paper; above the ceiling nothing fits and the shrink loop has no room to work.
MIN_FONT_SIZE_PT: Final[float] = 7.0
MAX_FONT_SIZE_PT: Final[float] = 14.0

#: Bounds on ``options["margin_in"]``. Under a quarter inch, consumer printers and some ATS
#: PDF-to-image converters clip the content.
MIN_MARGIN_IN: Final[float] = 0.25
MAX_MARGIN_IN: Final[float] = 1.5


# ======================================================================================
# Render settings — the two knobs the shrink loop turns
# ======================================================================================


@dataclass(frozen=True, slots=True)
class RenderSettings:
    """Resolved, clamped typography settings for one render attempt.

    Attributes:
        font_size: Body font size in points.
        margin_in: Page margin in inches, applied to all four sides.
    """

    font_size: float
    margin_in: float

    @property
    def content_width_in(self) -> float:
        """Printable width of a US Letter page at this margin, in inches."""
        return LETTER_WIDTH_IN - 2 * self.margin_in


#: US Letter, in inches. Every template in this package is letter-only; A4 would be a
#: separate template rather than an option, because the line lengths differ enough that the
#: shrink loop's estimates would be wrong.
LETTER_WIDTH_IN: Final[float] = 8.5
LETTER_HEIGHT_IN: Final[float] = 11.0


def resolve_render_settings(options: dict[str, Any] | None) -> RenderSettings:
    """Read ``font_size`` and ``margin_in`` out of a renderer's *options*.

    Args:
        options: The mapping passed to ``TemplatePlugin.render``. ``None`` and missing keys
            both mean "use the default".

    Returns:
        The resolved settings, clamped to the printable bounds.

    Raises:
        DocumentRenderError: If a supplied value is not a real number.
    """
    values = options or {}
    return RenderSettings(
        font_size=_clamp_option(
            values.get("font_size"),
            key="font_size",
            default=DEFAULT_FONT_SIZE_PT,
            low=MIN_FONT_SIZE_PT,
            high=MAX_FONT_SIZE_PT,
        ),
        margin_in=_clamp_option(
            values.get("margin_in"),
            key="margin_in",
            default=DEFAULT_MARGIN_IN,
            low=MIN_MARGIN_IN,
            high=MAX_MARGIN_IN,
        ),
    )


def _clamp_option(value: Any, *, key: str, default: float, low: float, high: float) -> float:
    """Validate and clamp one numeric render option.

    Args:
        value: The raw value from ``options``, possibly ``None``.
        key: Option name, used in the error message and the clamp log.
        default: Value used when *value* is ``None``.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        The clamped float.

    Raises:
        DocumentRenderError: If *value* is present but not a real number. Falling back
            silently would let a typo in the shrink loop produce a resume at the wrong size
            with nothing in the logs explaining why.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DocumentRenderError(
            f"render option {key!r} must be a number, got {type(value).__name__}"
        )
    clamped = min(high, max(low, float(value)))
    if clamped != float(value):
        logger.warning(
            "documents.option_clamped", option=key, requested=float(value), applied=clamped
        )
    return clamped


def estimated_page_count(doc: ResumeDocument, settings: RenderSettings) -> int:
    """Return the page count renderers report for unpaginated output.

    Markdown, LaTeX source and (unconverted) DOCX have no pages to count, but reporting zero
    would tell ``render_resume``'s shrink loop that a five-page resume fits on one. The
    estimate from the document model is the honest answer.

    Args:
        doc: The document being rendered.
        settings: The resolved font size and margin.

    Returns:
        :meth:`~app.documents.models.ResumeDocument.estimated_pages` rounded up, never below
        1 — an empty resume still occupies a sheet of paper.
    """
    return max(1, math.ceil(doc.estimated_pages(settings.font_size, settings.margin_in)))


def write_text(out: Path, text: str) -> int:
    """Write *text* to *out* as UTF-8, creating parent directories.

    Args:
        out: Destination path.
        text: Content to write.

    Returns:
        The number of bytes written.

    Raises:
        DocumentRenderError: If the file cannot be created or written.
    """
    payload = text.encode("utf-8")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
    except OSError as exc:
        raise DocumentRenderError(f"could not write {out}: {exc}") from exc
    return len(payload)


# ======================================================================================
# Contact rendering — shared with the LaTeX, HTML and DOCX templates
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ContactItem:
    """One printable element of a resume's contact line.

    Format-agnostic on purpose: :mod:`app.documents.latex` turns it into ``\\href``,
    :mod:`app.documents.html` into an ``<a>``, :mod:`app.documents.docx` into a plain run,
    and this module into ``[text](url)``. Only the *selection and ordering* is shared,
    because that is the part that should look the same on every rendering of one resume.

    Attributes:
        label: Machine-readable slot — ``"email"``, ``"phone"``, ``"location"``, or the key
            the user stored the link under (``"github"``, ``"linkedin"``, …).
        text: What the reader sees. For links this is the URL stripped of its scheme, which
            is both shorter and more recognisable than the full URL.
        url: The href to attach, already carrying a scheme. Empty for items that are not
            links — notably ``location``, which must never become a map link.
    """

    label: str
    text: str
    url: str = ""

    @property
    def is_link(self) -> bool:
        """Whether this item should be rendered as a hyperlink."""
        return bool(self.url)

    @property
    def display_name(self) -> str:
        """Human-readable name for the slot, e.g. ``"GitHub"`` for ``"github"``."""
        return LINK_DISPLAY_NAMES.get(self.label, self.label.replace("_", " ").title())


def pretty_url(url: str) -> str:
    """Return the display form of *url* — no scheme, no ``www.``, no trailing slash.

    ``https://www.github.com/jane/`` becomes ``github.com/jane``. Recruiters read the
    handle, not the protocol, and the shorter string keeps the contact line on one row at
    small font sizes.

    Args:
        url: A URL, with or without a scheme.

    Returns:
        The shortened display string, or the trimmed input when it carries no recognisable
        scheme.
    """
    text = url.strip()
    for scheme in ("https://", "http://"):
        if text.lower().startswith(scheme):
            text = text[len(scheme) :]
            break
    if text.lower().startswith("www."):
        text = text[4:]
    return text.rstrip("/")


def _absolute_url(url: str) -> str:
    """Return *url* with a scheme, so it is clickable from a PDF or a browser.

    Args:
        url: A URL that may be scheme-relative (``github.com/jane``).

    Returns:
        The URL prefixed with ``https://`` when it carries no known scheme; the empty string
        when *url* is blank.
    """
    text = url.strip()
    if not text:
        return ""
    if text.lower().startswith(_KNOWN_SCHEMES):
        return text
    return f"https://{text.lstrip('/')}"


def _tel_target(phone: str) -> str:
    """Return the ``tel:`` target for a printed phone number.

    Args:
        phone: The number as the user wants it printed, e.g. ``"(415) 555-0134"``.

    Returns:
        The number reduced to an optional leading ``+`` and digits, which is what a dialer
        expects. Returns the trimmed input when it contains no digits at all.
    """
    trimmed = phone.strip()
    digits = re.sub(r"\D", "", trimmed)
    if not digits:
        return trimmed
    return f"+{digits}" if trimmed.startswith("+") else digits


def contact_items(
    contact: Contact,
    *,
    include_links: bool = True,
    preferred: Sequence[str] = PREFERRED_LINK_ORDER,
) -> list[ContactItem]:
    """Return the contact line's elements, in the order every renderer prints them.

    The order — email, phone, location, then links — is the one recruiters and resume parsers
    both expect: the two fields an ATS actually indexes come first, so a header that wraps or
    gets truncated still carries the way to reach the candidate.

    Args:
        contact: The document's contact block.
        include_links: Set ``False`` to print only email, phone and location.
        preferred: Link labels to hoist to the front, passed through to
            :meth:`~app.documents.models.Contact.ordered_links`.

    Returns:
        Non-empty items only, so a contact with no phone number simply has one fewer
        separator rather than a dangling one.
    """
    items: list[ContactItem] = []

    email = contact.email.strip()
    if email:
        items.append(ContactItem(label="email", text=email, url=f"mailto:{email}"))

    phone = contact.phone.strip()
    if phone:
        items.append(ContactItem(label="phone", text=phone, url=f"tel:{_tel_target(phone)}"))

    location = contact.location.strip()
    if location:
        items.append(ContactItem(label="location", text=location))

    if include_links:
        for label, url in contact.ordered_links(preferred):
            absolute = _absolute_url(url)
            if absolute:
                items.append(
                    ContactItem(label=label.strip().lower(), text=pretty_url(url), url=absolute)
                )

    return items


def generated_heading(doc: ResumeDocument, meta_key: str, default: str) -> str:
    """Return a heading this package generates, honouring a ``meta`` override.

    The summary and skills blocks have no :class:`~app.documents.models.ResumeSection` of
    their own, so their headings are supplied by the renderer. A caller who wants "Profile"
    instead of "Summary" sets it in ``ResumeDocument.meta`` rather than post-processing the
    output.

    Args:
        doc: The document whose ``meta`` may carry an override.
        meta_key: The key to look for.
        default: Heading used when the key is absent, blank, or not a string.

    Returns:
        The heading text, unescaped — each renderer escapes for its own format.
    """
    override = doc.meta.get(meta_key)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return default


def recipient_lines(letter: CoverLetterDocument) -> list[str]:
    """Return the addressee block of a letter, one string per printed line.

    Args:
        letter: The letter being rendered.

    Returns:
        Up to three lines — recipient, the role applied for, company — omitting the empty
        ones. The role is included because a letter that lands in a shared inbox has to say
        which opening it is about before anyone reads the first paragraph.
    """
    lines: list[str] = []
    if letter.recipient.strip():
        lines.append(letter.recipient.strip())
    if letter.role.strip():
        lines.append(f"Re: {letter.role.strip()}")
    if letter.company.strip():
        lines.append(letter.company.strip())
    return lines


# ======================================================================================
# Escaping
# ======================================================================================


def escape_markdown(text: str) -> str:
    """Escape *text* so it prints literally instead of being read as Markdown.

    Model-produced bullets contain things like ``C_str``, ``*args``, ``a[i]`` and
    ``<threshold>``; unescaped, those silently turn into emphasis, links and dropped HTML
    tags. Two passes:

    * every character in :data:`INLINE_SPECIALS` is backslash-escaped wherever it occurs;
    * a leading ``#``, ``- ``, ``+ `` or ``1. `` is escaped only at the start of its line,
      where it would otherwise open a new block.

    Args:
        text: Raw text from the knowledge graph or an LLM.

    Returns:
        The escaped text. Newlines are preserved and each line is considered independently
        for the leading-token rule.
    """
    if not text:
        return ""

    escaped_lines: list[str] = []
    for line in text.split("\n"):
        buffer: list[str] = []
        for char in line:
            if char in INLINE_SPECIALS:
                buffer.append("\\")
            buffer.append(char)
        candidate = _LEADING_BLOCK.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", "".join(buffer))
        escaped_lines.append(candidate)
    return "\n".join(escaped_lines)


def _one_line(text: str) -> str:
    """Collapse *text* to a single escaped line.

    Bullets and headers must occupy exactly one line for the diff view to stay readable, so
    any internal wrapping an LLM introduced is removed.

    Args:
        text: Raw text, possibly hard-wrapped.

    Returns:
        The text with every whitespace run collapsed to one space, then escaped.
    """
    return escape_markdown(" ".join(text.split()))


# ======================================================================================
# Document rendering
# ======================================================================================


def _contact_markdown(contact: Contact) -> str:
    """Return the contact line as Markdown, or the empty string when there is nothing.

    Args:
        contact: The document's contact block.

    Returns:
        Items joined by :data:`CONTACT_SEPARATOR`, links rendered as ``[text](url)``.
    """
    parts: list[str] = []
    for item in contact_items(contact):
        text = _one_line(item.text)
        parts.append(f"[{text}]({item.url})" if item.is_link else text)
    return CONTACT_SEPARATOR.join(parts)


def _entry_markdown(entry: ResumeEntry) -> list[str]:
    """Return the Markdown block for one resume entry.

    Args:
        entry: The entry to render.

    Returns:
        The lines of the block: a header line carrying the bold title, then one ``- `` line
        per bullet. Empty when the entry carries neither a header nor a bullet.
    """
    lines: list[str] = []

    left_parts: list[str] = []
    if entry.title:
        left_parts.append(f"**{_one_line(entry.title)}**")
    if entry.organization:
        left_parts.append(_one_line(entry.organization))
    right_parts = [_one_line(part) for part in (entry.location, entry.date_range) if part]

    left = ", ".join(left_parts)
    right = CONTACT_SEPARATOR.join(right_parts)
    if left and right:
        lines.append(f"{left} — {right}")
    elif left or right:
        lines.append(left or right)

    lines.extend(f"- {_one_line(bullet)}" for bullet in entry.bullets if bullet.strip())
    return lines


def resume_markdown(doc: ResumeDocument) -> str:
    """Render a resume as Markdown.

    Args:
        doc: The resume to render. Values are escaped here, so pass the document exactly as
            the resume engine produced it — no pre-escaping.

    Returns:
        A Markdown document ending in a single newline: an ``#`` name heading, the contact
        line, then one ``##`` section per :class:`~app.documents.models.ResumeSection` with a
        header line and a ``-`` list per entry.
    """
    blocks: list[str] = []

    name = _one_line(doc.contact.name)
    if name:
        blocks.append(f"# {name}")

    contact_line = _contact_markdown(doc.contact)
    if contact_line:
        blocks.append(contact_line)

    if doc.summary.strip():
        heading = generated_heading(doc, SUMMARY_HEADING_META_KEY, DEFAULT_SUMMARY_HEADING)
        blocks.append(f"## {_one_line(heading)}")
        blocks.append(_one_line(doc.summary))

    for section in doc.sections:
        heading = _one_line(section.heading)
        if heading:
            blocks.append(f"## {heading}")
        for entry in section.entries:
            entry_lines = _entry_markdown(entry)
            if entry_lines:
                blocks.append("\n".join(entry_lines))

    if doc.skills_line.strip():
        heading = generated_heading(doc, SKILLS_HEADING_META_KEY, DEFAULT_SKILLS_HEADING)
        blocks.append(f"## {_one_line(heading)}")
        blocks.append(_one_line(doc.skills_line))

    return "\n\n".join(blocks) + "\n"


def cover_letter_markdown(letter: CoverLetterDocument, *, closing: str = DEFAULT_CLOSING) -> str:
    """Render a cover letter as Markdown.

    Args:
        letter: The letter to render, unescaped.
        closing: Sign-off placed before the sender's name.

    Returns:
        A Markdown document: sender heading and contact line, the date, the recipient block,
        the salutation, one paragraph per blank-line-separated chunk of
        :attr:`~app.documents.models.CoverLetterDocument.body`, then the sign-off. Lines
        inside the recipient and sign-off blocks are joined with a two-space hard break so
        they stack instead of reflowing into one paragraph.
    """
    blocks: list[str] = []

    name = _one_line(letter.contact.name)
    if name:
        blocks.append(f"# {name}")

    contact_line = _contact_markdown(letter.contact)
    if contact_line:
        blocks.append(contact_line)

    if letter.date.strip():
        blocks.append(_one_line(letter.date))

    addressee = [_one_line(line) for line in recipient_lines(letter)]
    if addressee:
        blocks.append("  \n".join(addressee))

    blocks.append(_one_line(letter.salutation()))
    blocks.extend(_one_line(paragraph) for paragraph in letter.paragraphs())

    sign_off = [_one_line(closing)]
    if name:
        sign_off.append(name)
    blocks.append("  \n".join(sign_off))

    return "\n\n".join(blocks) + "\n"


# ======================================================================================
# Plugin
# ======================================================================================


@plugin
class MarkdownTemplate(TemplatePlugin):
    """The ``markdown`` template (``PluginKind.TEMPLATE``) — plain text, zero dependencies.

    Produces ``.md`` only. There is no PDF path here by design: Markdown-to-PDF would need a
    converter, and the ``web`` template already guarantees a PDF from pure-pip dependencies.

    ``page_count`` on the returned :class:`~app.documents.renderer.RenderResult` is an
    estimate rather than a measurement — Markdown has no pages — so that ``render_resume``'s
    shrink loop still sees a meaningful signal when a caller asks for Markdown with
    ``max_pages=1``.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.TEMPLATE,
        name="markdown",
        version="1.0.0",
        display_name="Markdown",
        description="Plain-text resume for web forms and version diffing. No dependencies.",
        capabilities=frozenset({"resume", "cover_letter", "no_dependencies"}),
    )

    formats: ClassVar[frozenset[str]] = frozenset({MARKDOWN_FORMAT})

    async def render(
        self,
        doc: ResumeDocument,
        out: Path,
        *,
        fmt: str = MARKDOWN_FORMAT,
        options: dict[str, Any] | None = None,
    ) -> RenderResult:
        """Write *doc* to *out* as Markdown.

        Args:
            doc: The resume to render.
            out: Destination path. Parent directories are created.
            fmt: Must be ``"md"``.
            options: Accepts ``font_size`` and ``margin_in`` from the shrink loop. They do
                not change the output — Markdown has no typography — but they do change the
                page estimate reported back, which is what the loop reads.

        Returns:
            The written file, its size, and the estimated page count.

        Raises:
            DocumentRenderError: If *fmt* is not ``"md"``, or the file cannot be written.
        """
        require_format(self, fmt)
        out = self.resolve_output(out, fmt)
        settings = resolve_render_settings(self.merge_options(options))
        written = write_text(out, resume_markdown(doc))
        pages = estimated_page_count(doc, settings)

        self.logger.info(
            "markdown.rendered",
            path=str(out),
            bytes=written,
            sections=len(doc.sections),
            bullets=doc.total_bullets(),
            estimated_pages=pages,
        )
        return RenderResult.from_path(
            out, engine=MARKDOWN_ENGINE, template=self.name, page_count=pages
        )

    async def render_cover_letter(
        self,
        letter: CoverLetterDocument,
        out: Path,
        *,
        fmt: str = MARKDOWN_FORMAT,
        options: dict[str, Any] | None = None,
    ) -> RenderResult:
        """Write *letter* to *out* as Markdown.

        Args:
            letter: The cover letter to render.
            out: Destination path. Parent directories are created.
            fmt: Must be ``"md"``.
            options: Accepts ``closing`` to override the sign-off.

        Returns:
            The written file and its size. ``page_count`` is 1 — a cover letter that runs
            past one page has a content problem no renderer can fix.

        Raises:
            DocumentRenderError: If *fmt* is not ``"md"``, or the file cannot be written.
        """
        require_format(self, fmt)
        out = self.resolve_output(out, fmt)
        closing = str((options or {}).get("closing") or DEFAULT_CLOSING)
        written = write_text(out, cover_letter_markdown(letter, closing=closing))

        self.logger.info("markdown.cover_letter_rendered", path=str(out), bytes=written)
        return RenderResult.from_path(out, engine=MARKDOWN_ENGINE, template=self.name, page_count=1)


def require_format(template: TemplatePlugin, fmt: str) -> None:
    """Reject a format the template does not produce.

    Args:
        template: The plugin handling the call.
        fmt: The requested format.

    Raises:
        DocumentRenderError: If *fmt* is not in the template's ``formats``. The message lists
            what the template *can* produce, because the usual cause is a caller assuming
            every template renders PDF.
    """
    if fmt not in template.formats:
        raise DocumentRenderError(
            f"template {template.name!r} renders {sorted(template.formats)}, not {fmt!r}"
        )
