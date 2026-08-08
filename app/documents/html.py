"""The ``web`` template plugin — HTML, and the PDF path that always works.

Two audiences, one template file. ``templates/resume.html.j2`` renders the resume preview the
desktop app shows, and it is also what WeasyPrint turns into a PDF. Standard Jinja2 delimiters
and ``autoescape=True`` here, unlike the LaTeX templates next door: HTML escaping is exactly
what is wanted when the input is LLM-authored prose.

**This module is the guarantee.** ``modern``/``classic`` need a TeX engine and ``ats_plain``
needs LibreOffice for its PDF — both are system binaries, and on a fresh Windows machine there
is no reason for either to be present. So this renderer has two backends:

``weasyprint``
    The good one. A real CSS layout engine: the PDF matches the preview, ``@page`` is honoured,
    hyphenation and page-break avoidance work. Preferred whenever it imports.

``reportlab``
    The floor. Pure Python, installs from PyPI with no compiler and no system libraries, and
    is therefore *always available* in a `pip install` of this project. It does not read the
    HTML — it lays the :class:`~app.documents.models.ResumeDocument` out directly with
    Platypus flowables, deliberately mirroring the same visual structure.

Because the fallback is the guaranteed path rather than a degraded courtesy, it is built
properly: right-aligned dates on the same baseline as the role, rules under headings, hanging
bullet indents, clickable links, entries that avoid splitting across a page break.

The right-aligned date is a custom flowable (``SplitLine``) rather than a two-column
``Table``, for the same reason the LaTeX and DOCX renderers avoid tables: it emits two text
operations on one line in reading order, which is what a resume parser recovers cleanly.

No external resources are referenced by any output of this module — no webfont, no CDN, no
image. WeasyPrint is invoked with ``base_url=None`` so a stray absolute URL cannot turn a
render into a network fetch, and reportlab uses only the PDF base-14 fonts.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final
from xml.sax.saxutils import escape as xml_escape

import structlog

from app.documents.markdown import (
    DEFAULT_CLOSING,
    DEFAULT_SKILLS_HEADING,
    DEFAULT_SUMMARY_HEADING,
    LETTER_HEIGHT_IN,
    LETTER_WIDTH_IN,
    SKILLS_HEADING_META_KEY,
    SUMMARY_HEADING_META_KEY,
    ContactItem,
    RenderSettings,
    contact_items,
    estimated_page_count,
    generated_heading,
    recipient_lines,
    require_format,
    resolve_render_settings,
    write_text,
)
from app.documents.models import CoverLetterDocument, ResumeDocument
from app.documents.renderer import DocumentRenderError, RenderResult, TemplatePlugin
from app.models.enums import PluginKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from collections.abc import Callable, Sequence

    from jinja2 import Environment

__all__ = [
    "HTML_FORMAT",
    "REPORTLAB_ENGINE",
    "WEASYPRINT_ENGINE",
    "WebTemplate",
    "html_environment",
    "render_cover_letter_html",
    "render_resume_html",
    "weasyprint_available",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Directory holding the ``.html.j2`` sources.
TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

#: Template file names.
RESUME_TEMPLATE: Final[str] = "resume.html.j2"
COVER_LETTER_TEMPLATE: Final[str] = "cover_letter.html.j2"

#: Formats this plugin produces.
HTML_FORMAT: Final[str] = "html"
PDF_FORMAT: Final[str] = "pdf"
WEB_FORMATS: Final[frozenset[str]] = frozenset({HTML_FORMAT, PDF_FORMAT})

#: Reported as ``RenderResult.engine``.
HTML_ENGINE: Final[str] = "jinja2"
WEASYPRINT_ENGINE: Final[str] = "weasyprint"
REPORTLAB_ENGINE: Final[str] = "reportlab"

#: ``options["pdf_backend"]`` accepts these, to pin a backend instead of auto-detecting.
PDF_BACKENDS: Final[frozenset[str]] = frozenset({WEASYPRINT_ENGINE, REPORTLAB_ENGINE})

#: Accent colour, matching :data:`app.documents.latex.MODERN_ACCENT_HEX` so that a resume
#: rendered through either path is recognisably the same document.
ACCENT_CSS: Final[str] = "#1b3a5b"

#: Hairline colour for the rules under section headings and the contact separators.
RULE_CSS: Final[str] = "#9aa3ad"

#: Body text colour, and the slightly lighter tone used for dates and locations.
INK_CSS: Final[str] = "#111111"
MUTED_CSS: Final[str] = "#3d3d3d"

#: ``lang`` attribute on the generated documents.
DEFAULT_LANG: Final[str] = "en"

#: Alignment of the resume's name/contact block. Centred, matching the ``modern`` LaTeX
#: template; the CSS reads it so the choice is one constant rather than a rule to edit.
RESUME_HEADER_ALIGN: Final[str] = "center"

#: Type scale for the reportlab fallback, as multiples of the body size. Kept in step with
#: the ratios in ``resume.html.j2`` and the LaTeX templates.
NAME_SIZE_RATIO: Final[float] = 2.05
HEADLINE_SIZE_RATIO: Final[float] = 1.02
CONTACT_SIZE_RATIO: Final[float] = 0.94
HEADING_SIZE_RATIO: Final[float] = 1.06
BODY_LEADING_RATIO: Final[float] = 1.28

#: Vertical rhythm for the reportlab fallback, in points.
SPACE_BEFORE_HEADING_PT: Final[float] = 9.0
SPACE_AFTER_HEADING_PT: Final[float] = 3.0
SPACE_BEFORE_ENTRY_PT: Final[float] = 4.5
RULE_OFFSET_PT: Final[float] = 1.5
BULLET_INDENT_PT: Final[float] = 11.0

#: Minimum gap between the role and the right-aligned date, in points.
SPLIT_LINE_GAP_PT: Final[float] = 14.0

#: Base-14 PDF fonts. Always present in any reportlab install, never substituted, and their
#: metrics are built in — which is what lets ``SplitLine`` measure a string exactly.
FONT_REGULAR: Final[str] = "Helvetica"
FONT_BOLD: Final[str] = "Helvetica-Bold"

#: Separator drawn between contact items in the reportlab fallback.
CONTACT_DOT: Final[str] = "&nbsp;&middot;&nbsp;"

#: Whitespace runs collapsed before text is measured or drawn.
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


# ======================================================================================
# The Jinja2 environment
# ======================================================================================


@functools.lru_cache(maxsize=1)
def html_environment() -> Environment:
    """Return the process-wide Jinja2 environment for the ``.html.j2`` templates.

    Returns:
        An environment loading from :data:`TEMPLATE_DIR` with the *standard* delimiters and
        ``autoescape=True``. Autoescape is forced on rather than inferred from the file
        extension: :func:`jinja2.select_autoescape` keys on the last suffix, and ``.j2`` is
        not in its list, so inference would silently leave every template unescaped.

    Raises:
        DocumentRenderError: If Jinja2 is not installed.
    """
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ImportError as exc:  # pragma: no cover - exercised only without jinja2
        raise DocumentRenderError(
            "Jinja2 is required to render HTML templates. Install it with `pip install jinja2`."
        ) from exc

    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR), encoding="utf-8"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )


def _render_template(name: str, context: dict[str, Any]) -> str:
    """Render one HTML template.

    Args:
        name: File name inside :data:`TEMPLATE_DIR`.
        context: Template variables. Escaping is the environment's job here.

    Returns:
        The rendered document.

    Raises:
        DocumentRenderError: If the template is missing or fails to render.
    """
    from jinja2 import TemplateError

    try:
        return html_environment().get_template(name).render(**context)
    except TemplateError as exc:
        raise DocumentRenderError(
            f"template {name!r} failed to render: {type(exc).__name__}: {exc}"
        ) from exc


def _shared_context(settings: RenderSettings) -> dict[str, Any]:
    """Return the context keys both HTML templates need.

    Args:
        settings: Resolved font size and margin.

    Returns:
        Typography and palette values.
    """
    return {
        "font_size": round(settings.font_size, 2),
        "margin_in": round(settings.margin_in, 3),
        "content_width_in": round(LETTER_WIDTH_IN, 2),
        "accent_css": ACCENT_CSS,
        "rule_css": RULE_CSS,
        "lang": DEFAULT_LANG,
    }


def _contact_context(items: Sequence[ContactItem]) -> list[dict[str, str]]:
    """Convert contact items into the dictionaries the HTML templates iterate.

    Args:
        items: Items from :func:`~app.documents.markdown.contact_items`.

    Returns:
        One dict per item with ``text`` and ``url``. Values are *not* pre-escaped — the
        environment's autoescape handles that, and escaping twice would print ``&amp;amp;``.
    """
    return [{"text": item.text, "url": item.url} for item in items]


def render_resume_html(doc: ResumeDocument, *, settings: RenderSettings) -> str:
    """Render a resume to a standalone HTML document.

    Args:
        doc: The resume to render, unescaped.
        settings: Resolved font size and margin.

    Returns:
        A complete HTML document with its CSS inlined and no external references.

    Raises:
        DocumentRenderError: If Jinja2 is missing or the template fails.
    """
    context = _shared_context(settings)
    context.update(
        {
            "pdf_title": f"{doc.contact.name} — Resume".strip(" —") or "Resume",
            "pdf_author": doc.contact.name,
            "header_align": RESUME_HEADER_ALIGN,
            "name": doc.contact.name,
            "headline": _headline(doc),
            "contact_items": _contact_context(contact_items(doc.contact)),
            "summary": doc.summary,
            "summary_heading": generated_heading(
                doc, SUMMARY_HEADING_META_KEY, DEFAULT_SUMMARY_HEADING
            ),
            "sections": [
                {
                    "heading": section.heading,
                    "entries": [
                        {
                            "title": entry.title,
                            "organization": entry.organization,
                            "location": entry.location,
                            "date_range": entry.date_range,
                            "bullets": [b for b in entry.bullets if b.strip()],
                        }
                        for entry in section.entries
                    ],
                }
                for section in doc.sections
            ],
            "skills_line": doc.skills_line,
            "skills_heading": generated_heading(
                doc, SKILLS_HEADING_META_KEY, DEFAULT_SKILLS_HEADING
            ),
        }
    )
    return _render_template(RESUME_TEMPLATE, context)


def render_cover_letter_html(
    letter: CoverLetterDocument,
    *,
    settings: RenderSettings,
    closing: str = DEFAULT_CLOSING,
) -> str:
    """Render a cover letter to a standalone HTML document.

    Args:
        letter: The letter to render, unescaped.
        settings: Resolved font size and margin.
        closing: Sign-off line.

    Returns:
        A complete HTML document with its CSS inlined and no external references.

    Raises:
        DocumentRenderError: If Jinja2 is missing or the template fails.
    """
    context = _shared_context(settings)
    context.update(
        {
            "pdf_title": f"{letter.contact.name} — Cover letter".strip(" —") or "Cover letter",
            "pdf_author": letter.contact.name,
            "sender_name": letter.contact.name,
            "sender_items": _contact_context(contact_items(letter.contact)),
            "date": letter.date,
            "recipient_lines": recipient_lines(letter),
            "salutation": letter.salutation(),
            "paragraphs": letter.paragraphs(),
            "closing": closing,
            "signature": letter.contact.name,
        }
    )
    return _render_template(COVER_LETTER_TEMPLATE, context)


def _headline(doc: ResumeDocument) -> str:
    """Return the optional headline printed under the name.

    Args:
        doc: The resume, whose ``meta`` may carry a ``headline``.

    Returns:
        The headline, or the empty string.
    """
    headline = doc.meta.get("headline")
    return headline.strip() if isinstance(headline, str) else ""


# ======================================================================================
# PDF backend: WeasyPrint
# ======================================================================================


def weasyprint_available() -> bool:
    """Report whether WeasyPrint can be imported.

    Uses :func:`importlib.util.find_spec` rather than a ``try: import`` because importing
    WeasyPrint loads Pango, Cairo and HarfBuzz through ``ctypes``; on a machine where those
    libraries are installed but mismatched it raises ``OSError`` at import time, and a
    capability probe must not be able to take a process down.

    Returns:
        ``True`` when the package is present and its spec resolves.
    """
    try:
        return importlib.util.find_spec("weasyprint") is not None
    except (ImportError, ValueError):
        return False


def _weasyprint_write(html: str, out: Path) -> None:
    """Write *html* to *out* as a PDF using WeasyPrint. Blocking.

    Args:
        html: A complete HTML document.
        out: Destination path.

    Raises:
        DocumentRenderError: If WeasyPrint is unusable, or the write fails.
    """
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:
        raise DocumentRenderError(f"WeasyPrint is not usable: {exc}") from exc

    try:
        # base_url=None: the document references nothing external by construction, and
        # leaving it unset would let a stray absolute URL turn a render into a network fetch.
        HTML(string=html, base_url=None).write_pdf(str(out))
    except OSError as exc:
        raise DocumentRenderError(f"WeasyPrint could not write {out}: {exc}") from exc


# ======================================================================================
# PDF backend: reportlab (the guaranteed path)
# ======================================================================================


def _clean(text: str) -> str:
    """Collapse whitespace runs in *text*.

    Args:
        text: Raw text, possibly hard-wrapped by an LLM.

    Returns:
        The text with every whitespace run replaced by a single space, trimmed.
    """
    return _WHITESPACE.sub(" ", text).strip()


def _para_text(text: str) -> str:
    """Escape *text* for reportlab's paragraph markup.

    Platypus ``Paragraph`` parses a small XML dialect, so an ampersand or an angle bracket in
    a bullet — ``R&D``, ``latency < 50ms`` — is a parse error, not a character.

    Args:
        text: Raw text.

    Returns:
        The whitespace-collapsed, XML-escaped text.
    """
    return xml_escape(_clean(text))


def _link_markup(item: ContactItem, *, color: str) -> str:
    """Return one contact item as reportlab paragraph markup.

    Args:
        item: The item to render.
        color: Hex colour for links.

    Returns:
        An ``<a>`` element for links, plain escaped text otherwise.
    """
    text = _para_text(item.text)
    if not item.is_link:
        return text
    href = xml_escape(item.url, {'"': "&quot;"})
    return f'<a href="{href}" color="{color}">{text}</a>'


@functools.lru_cache(maxsize=1)
def _split_line_class() -> type:
    """Build and cache the right-aligned-date flowable class.

    Defined inside a function so reportlab stays a lazy import while the class is still a
    genuine :class:`reportlab.platypus.Flowable` subclass. Platypus calls a dozen protocol
    methods on everything in a story — ``wrapOn``, ``splitOn``, ``getSpaceBefore``,
    ``isIndexing``, ``getKeepWithNext`` — and hand-rolling them is a long tail of subtle
    breakage; inheriting gets all of them right.

    Returns:
        The ``SplitLine`` class. Cached, because a class object rebuilt per render would
        defeat the ``isinstance`` checks Platypus makes internally.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Flowable

    class SplitLine(Flowable):
        """Left-aligned runs and one right-aligned string sharing a baseline.

        The reportlab equivalent of LaTeX's ``\\hfill`` and of a right tab stop in Word:
        ``Senior Engineer, Acme`` on the left, ``Remote · 2021 – Present`` hard against the
        right margin. A two-column ``Table`` would do it in three lines of code and is
        exactly what this avoids — a table interleaves the PDF's text stream, which is the
        single most reliable way to scramble a resume parse.

        Long left-hand text wraps onto continuation lines that use the full width, since only
        the first line has to leave room for the date.
        """

        def __init__(
            self,
            runs: Sequence[tuple[str, bool]],
            right: str,
            *,
            font_size: float,
            leading: float,
            right_color: Any,
            ink_color: Any,
        ) -> None:
            """Store the content and metrics for one header line.

            Args:
                runs: ``(text, bold)`` pairs for the left side, in order.
                right: The right-aligned string; may be empty.
                font_size: Point size for every run.
                leading: Baseline-to-baseline distance in points.
                right_color: Colour for the right-aligned string.
                ink_color: Colour for the left-hand runs.
            """
            super().__init__()
            self.runs = [(text, bold) for text, bold in runs if text]
            self.right = right
            self.font_size = font_size
            self.leading = leading
            self.right_color = right_color
            self.ink_color = ink_color
            self.keepWithNext = 1
            self._lines: list[list[tuple[str, bool]]] = []

        def wrap(self, avail_width: float, avail_height: float) -> tuple[float, float]:
            """Lay the line out and report the space it needs.

            Args:
                avail_width: Frame width in points.
                avail_height: Remaining frame height in points. Unused — this flowable is at
                    most a few lines tall and refuses to split.

            Returns:
                ``(width, height)`` in points.
            """
            del avail_height
            self.width = avail_width
            right_width = self._measure(self.right, bold=False) if self.right else 0.0
            first_width = avail_width - right_width
            if self.right:
                first_width -= SPLIT_LINE_GAP_PT
            self._lines = self._wrap_runs(max(first_width, 1.0), avail_width)
            self.height = self.leading * max(1, len(self._lines))
            return self.width, self.height

        def draw(self) -> None:
            """Paint the laid-out lines. The canvas origin is this flowable's bottom-left."""
            canvas = self.canv
            baseline = self.height - self.font_size

            canvas.setFillColor(self.ink_color)
            for index, line in enumerate(self._lines):
                x = 0.0
                y = baseline - index * self.leading
                for text, bold in line:
                    font = FONT_BOLD if bold else FONT_REGULAR
                    canvas.setFont(font, self.font_size)
                    canvas.drawString(x, y, text)
                    x += stringWidth(text, font, self.font_size)

            if self.right:
                canvas.setFillColor(self.right_color)
                canvas.setFont(FONT_REGULAR, self.font_size)
                canvas.drawRightString(self.width, baseline, self.right)

        def split(self, avail_width: float, avail_height: float) -> list[Any]:
            """Refuse to split: a role header broken across pages reads as two jobs.

            Args:
                avail_width: Frame width in points, unused.
                avail_height: Remaining frame height in points, unused.

            Returns:
                An empty list, which moves the whole flowable to the next frame.
            """
            del avail_width, avail_height
            return []

        def _measure(self, text: str, *, bold: bool) -> float:
            """Return the printed width of *text* in points, from the font's own metrics.

            Args:
                text: The string to measure.
                bold: Whether it is set in the bold face.

            Returns:
                The width in points.
            """
            return stringWidth(text, FONT_BOLD if bold else FONT_REGULAR, self.font_size)

        def _wrap_runs(self, first_width: float, full_width: float) -> list[list[tuple[str, bool]]]:
            """Greedily wrap the left-hand runs into lines.

            Args:
                first_width: Width available on the first line, which must leave room for the
                    right-aligned string.
                full_width: Width available on continuation lines.

            Returns:
                One list of ``(text, bold)`` pieces per line, with inter-word spaces already
                attached so :meth:`draw` can emit plain ``drawString`` calls.
            """
            words: list[tuple[str, bool]] = []
            for text, bold in self.runs:
                words.extend((piece, bold) for piece in _clean(text).split(" ") if piece)
            if not words:
                return [[]]

            lines: list[list[tuple[str, bool]]] = []
            current: list[tuple[str, bool]] = []
            used = 0.0
            limit = first_width

            for word, bold in words:
                padded = word if not current else f" {word}"
                width = self._measure(padded, bold=bold)
                if current and used + width > limit:
                    lines.append(current)
                    current = [(word, bold)]
                    used = self._measure(word, bold=bold)
                    limit = full_width
                    continue
                current.append((padded, bold))
                used += width

            lines.append(current)
            return lines

    return SplitLine


def _split_line(
    runs: Sequence[tuple[str, bool]],
    right: str,
    *,
    font_size: float,
    leading: float,
    right_color: Any,
    ink_color: Any,
) -> Any:
    """Construct one right-aligned-date header line.

    Args:
        runs: ``(text, bold)`` pairs for the left side.
        right: The right-aligned string.
        font_size: Point size.
        leading: Baseline-to-baseline distance in points.
        right_color: Colour for the right-aligned string.
        ink_color: Colour for the left-hand runs.

    Returns:
        A ``SplitLine`` flowable.

    Raises:
        DocumentRenderError: If reportlab is not installed.
    """
    try:
        factory = _split_line_class()
    except ImportError as exc:  # pragma: no cover - exercised only without reportlab
        raise DocumentRenderError(
            "reportlab is required for the 'web' template's PDF fallback. Install it with "
            "`pip install reportlab` (pure Python, no system libraries)."
        ) from exc
    return factory(
        runs,
        right,
        font_size=font_size,
        leading=leading,
        right_color=right_color,
        ink_color=ink_color,
    )


def _reportlab_styles(settings: RenderSettings) -> dict[str, Any]:
    """Build the paragraph styles for the reportlab fallback.

    Args:
        settings: Resolved font size and margin.

    Returns:
        A mapping of style name to ``ParagraphStyle``, plus the two colours the custom
        flowable needs under ``"_ink"`` and ``"_muted"``.
    """
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle

    size = settings.font_size
    ink = HexColor(INK_CSS)
    muted = HexColor(MUTED_CSS)
    accent = HexColor(ACCENT_CSS)

    return {
        "_ink": ink,
        "_muted": muted,
        "_accent": accent,
        "_rule": HexColor(RULE_CSS),
        "name": ParagraphStyle(
            "name",
            fontName=FONT_BOLD,
            fontSize=size * NAME_SIZE_RATIO,
            leading=size * NAME_SIZE_RATIO * 1.1,
            alignment=TA_CENTER,
            textColor=ink,
            spaceAfter=1.0,
        ),
        "headline": ParagraphStyle(
            "headline",
            fontName=FONT_REGULAR,
            fontSize=size * HEADLINE_SIZE_RATIO,
            leading=size * HEADLINE_SIZE_RATIO * 1.25,
            alignment=TA_CENTER,
            textColor=muted,
            spaceBefore=2.0,
        ),
        "contact": ParagraphStyle(
            "contact",
            fontName=FONT_REGULAR,
            fontSize=size * CONTACT_SIZE_RATIO,
            leading=size * CONTACT_SIZE_RATIO * 1.4,
            alignment=TA_CENTER,
            textColor=muted,
            spaceBefore=3.0,
        ),
        "heading": ParagraphStyle(
            "heading",
            fontName=FONT_BOLD,
            fontSize=size * HEADING_SIZE_RATIO,
            leading=size * HEADING_SIZE_RATIO * 1.15,
            textColor=accent,
            spaceBefore=SPACE_BEFORE_HEADING_PT,
            spaceAfter=0.0,
            keepWithNext=1,
        ),
        "body": ParagraphStyle(
            "body",
            fontName=FONT_REGULAR,
            fontSize=size,
            leading=size * BODY_LEADING_RATIO,
            alignment=TA_LEFT,
            textColor=ink,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            fontName=FONT_REGULAR,
            fontSize=size,
            leading=size * BODY_LEADING_RATIO,
            alignment=TA_LEFT,
            textColor=ink,
            leftIndent=BULLET_INDENT_PT,
            bulletIndent=2.0,
            spaceBefore=0.5,
        ),
    }


def _reportlab_story(doc: ResumeDocument, styles: dict[str, Any]) -> list[Any]:
    """Build the Platypus story for a resume.

    Args:
        doc: The resume to lay out.
        styles: Styles from :func:`_reportlab_styles`.

    Returns:
        The flowables, in printed order.
    """
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, Spacer

    body_style = styles["body"]
    story: list[Any] = []

    if doc.contact.name:
        story.append(Paragraph(_para_text(doc.contact.name), styles["name"]))

    headline = _headline(doc)
    if headline:
        story.append(Paragraph(_para_text(headline), styles["headline"]))

    items = contact_items(doc.contact)
    if items:
        markup = CONTACT_DOT.join(_link_markup(item, color=ACCENT_CSS) for item in items)
        story.append(Paragraph(markup, styles["contact"]))

    def heading(text: str) -> list[Any]:
        """Return a section heading and its rule.

        Args:
            text: The heading text.

        Returns:
            The heading paragraph followed by a hairline rule.
        """
        return [
            Paragraph(_para_text(text), styles["heading"]),
            HRFlowable(
                width="100%",
                thickness=0.7,
                color=styles["_rule"],
                spaceBefore=RULE_OFFSET_PT,
                spaceAfter=SPACE_AFTER_HEADING_PT,
            ),
        ]

    if doc.summary.strip():
        story.extend(
            heading(generated_heading(doc, SUMMARY_HEADING_META_KEY, DEFAULT_SUMMARY_HEADING))
        )
        story.append(Paragraph(_para_text(doc.summary), body_style))

    for section in doc.sections:
        if section.heading:
            story.extend(heading(section.heading))
        for index, entry in enumerate(section.entries):
            block: list[Any] = []
            if index:
                block.append(Spacer(1, SPACE_BEFORE_ENTRY_PT))

            runs: list[tuple[str, bool]] = []
            if entry.title:
                runs.append((entry.title, True))
            if entry.title and entry.organization:
                runs.append((", ", False))
            if entry.organization:
                runs.append((entry.organization, False))
            right = " · ".join(part for part in (entry.location, entry.date_range) if part)

            if runs or right:
                block.append(
                    _split_line(
                        runs,
                        _clean(right),
                        font_size=body_style.fontSize,
                        leading=body_style.leading,
                        right_color=styles["_muted"],
                        ink_color=styles["_ink"],
                    )
                )
            for bullet in entry.bullets:
                if bullet.strip():
                    block.append(Paragraph(_para_text(bullet), styles["bullet"], bulletText="•"))
            if block:
                # An entry split across a page break reads as two different jobs.
                story.append(KeepTogether(block))

    if doc.skills_line.strip():
        story.extend(
            heading(generated_heading(doc, SKILLS_HEADING_META_KEY, DEFAULT_SKILLS_HEADING))
        )
        story.append(Paragraph(_para_text(doc.skills_line), body_style))

    return story


def _reportlab_letter_story(
    letter: CoverLetterDocument,
    styles: dict[str, Any],
    closing: str,
) -> list[Any]:
    """Build the Platypus story for a cover letter.

    Args:
        letter: The letter to lay out.
        styles: Styles from :func:`_reportlab_styles`.
        closing: Sign-off line.

    Returns:
        The flowables, in printed order.
    """
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    body_style = styles["body"]
    letterhead = ParagraphStyle(
        "letterhead",
        parent=styles["name"],
        alignment=TA_LEFT,
        fontSize=body_style.fontSize * 1.55,
        leading=body_style.fontSize * 1.75,
        textColor=styles["_accent"],
    )
    contact_style = ParagraphStyle("letter-contact", parent=styles["contact"], alignment=TA_LEFT)
    prose = ParagraphStyle("prose", parent=body_style, leading=body_style.fontSize * 1.45)

    story: list[Any] = []
    if letter.contact.name:
        story.append(Paragraph(_para_text(letter.contact.name), letterhead))

    items = contact_items(letter.contact)
    if items:
        markup = CONTACT_DOT.join(_link_markup(item, color=ACCENT_CSS) for item in items)
        story.append(Paragraph(markup, contact_style))

    story.append(
        HRFlowable(width="100%", thickness=0.7, color=styles["_rule"], spaceBefore=4, spaceAfter=12)
    )

    if letter.date.strip():
        story.append(Paragraph(_para_text(letter.date), prose))
        story.append(Spacer(1, 12))

    address = recipient_lines(letter)
    if address:
        for line in address:
            story.append(Paragraph(_para_text(line), prose))
        story.append(Spacer(1, 12))

    story.append(Paragraph(_para_text(letter.salutation()), prose))
    story.append(Spacer(1, 8))
    for paragraph in letter.paragraphs():
        story.append(Paragraph(_para_text(paragraph), prose))
        story.append(Spacer(1, 8))

    story.append(Spacer(1, 8))
    story.append(Paragraph(_para_text(closing), prose))
    story.append(Spacer(1, 14))
    if letter.contact.name:
        story.append(Paragraph(_para_text(letter.contact.name), prose))

    return story


def _reportlab_build(
    story: list[Any],
    out: Path,
    *,
    settings: RenderSettings,
    title: str,
    author: str,
    subject: str,
) -> None:
    """Lay a story out onto US Letter pages and write the PDF. Blocking.

    Args:
        story: Flowables from :func:`_reportlab_story` or :func:`_reportlab_letter_story`.
        out: Destination path.
        settings: Resolved font size and margin.
        title: PDF document title, shown in a reader's title bar.
        author: PDF author metadata — the candidate's name.
        subject: PDF subject metadata, ``"Resume"`` or ``"Cover letter"``.

    Raises:
        DocumentRenderError: If reportlab is missing, the layout cannot be satisfied, or the
            file cannot be written.
    """
    try:
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate
        from reportlab.platypus.doctemplate import LayoutError
    except ImportError as exc:  # pragma: no cover - exercised only without reportlab
        raise DocumentRenderError(
            "Neither WeasyPrint nor reportlab is installed, so the 'web' template cannot "
            "produce a PDF. Install one with `pip install reportlab` (pure Python, no system "
            "libraries) or `pip install weasyprint`."
        ) from exc

    margin = settings.margin_in * inch
    document = SimpleDocTemplate(
        str(out),
        pagesize=(LETTER_WIDTH_IN * inch, LETTER_HEIGHT_IN * inch),
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=title,
        author=author,
        creator="ApplicantOS",
        subject=subject,
    )
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        document.build(story)
    except LayoutError as exc:
        # The usual cause is a single flowable taller than the printable area — a bullet so
        # long it cannot fit between the margins at the requested font size.
        raise DocumentRenderError(
            f"reportlab could not lay the document out at {settings.font_size}pt with "
            f"{settings.margin_in}in margins: {exc}"
        ) from exc
    except OSError as exc:
        raise DocumentRenderError(f"reportlab could not write {out}: {exc}") from exc


# ======================================================================================
# Plugin
# ======================================================================================


@plugin
class WebTemplate(TemplatePlugin):
    """The ``web`` template (``PluginKind.TEMPLATE``) — HTML, and a PDF that always works.

    ``fmt="html"`` writes the standalone document the desktop preview loads. ``fmt="pdf"``
    prefers WeasyPrint and falls back to reportlab, so a ``pip install`` of this project can
    always produce a PDF with no system binaries at all — no TeX, no LibreOffice.

    Pin a backend with ``options={"pdf_backend": "reportlab"}`` when a deterministic byte
    stream matters, such as in tests.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.TEMPLATE,
        name="web",
        version="1.0.0",
        display_name="Web (HTML → PDF)",
        description="HTML resume rendered to PDF via WeasyPrint, or reportlab with no "
        "system dependencies at all.",
        capabilities=frozenset({"resume", "cover_letter", "ats_safe", "html", "no_binaries"}),
    )

    formats: ClassVar[frozenset[str]] = WEB_FORMATS

    async def render(
        self,
        doc: ResumeDocument,
        out: Path,
        *,
        fmt: str = PDF_FORMAT,
        options: dict[str, Any] | None = None,
    ) -> RenderResult:
        """Render *doc* to *out* as HTML or PDF.

        Args:
            doc: The resume to render, unescaped — the Jinja2 environment escapes it.
            out: Destination path. Parent directories are created.
            fmt: ``"html"`` or ``"pdf"``.
            options: ``font_size`` (points) and ``margin_in`` (inches) from
                ``render_resume``'s shrink loop, and ``pdf_backend`` to pin a backend.

        Returns:
            The written file, its size, and its page count — measured from the PDF, estimated
            from the document model for HTML.

        Raises:
            DocumentRenderError: If *fmt* is unsupported, the template fails, an unknown
                backend is requested, or no PDF backend is installed.
        """
        require_format(self, fmt)
        out = self.resolve_output(out, fmt)
        values = self.merge_options(options)
        settings = resolve_render_settings(values)
        html = render_resume_html(doc, settings=settings)

        if fmt == HTML_FORMAT:
            written = write_text(out, html)
            pages = estimated_page_count(doc, settings)
            self.logger.info("html.rendered", path=str(out), bytes=written, estimated_pages=pages)
            return RenderResult.from_path(
                out, engine=HTML_ENGINE, template=self.name, page_count=pages
            )

        engine = await self._write_pdf(
            html,
            out,
            settings=settings,
            options=values,
            story_factory=lambda styles: _reportlab_story(doc, styles),
            title=f"{doc.contact.name} — Resume".strip(" —") or "Resume",
            author=doc.contact.name,
            subject="Resume",
        )
        result = RenderResult.from_path(out, engine=engine, template=self.name)
        self.logger.info(
            "html.rendered_pdf",
            path=str(out),
            engine=engine,
            pages=result.page_count,
            bytes=result.bytes_written,
            font_size=settings.font_size,
            margin_in=settings.margin_in,
        )
        return result

    async def render_cover_letter(
        self,
        letter: CoverLetterDocument,
        out: Path,
        *,
        fmt: str = PDF_FORMAT,
        options: dict[str, Any] | None = None,
    ) -> RenderResult:
        """Render *letter* to *out* as HTML or PDF.

        Args:
            letter: The cover letter to render, unescaped.
            out: Destination path. Parent directories are created.
            fmt: ``"html"`` or ``"pdf"``.
            options: ``font_size``, ``margin_in``, ``closing``, ``pdf_backend``.

        Returns:
            The written file, its size, and its page count.

        Raises:
            DocumentRenderError: On the same conditions as :meth:`render`.
        """
        require_format(self, fmt)
        out = self.resolve_output(out, fmt)
        values = self.merge_options(options)
        settings = resolve_render_settings(values)
        closing = str(values.get("closing") or DEFAULT_CLOSING)
        html = render_cover_letter_html(letter, settings=settings, closing=closing)

        if fmt == HTML_FORMAT:
            write_text(out, html)
            return RenderResult.from_path(out, engine=HTML_ENGINE, template=self.name, page_count=1)

        engine = await self._write_pdf(
            html,
            out,
            settings=settings,
            options=values,
            story_factory=lambda styles: _reportlab_letter_story(letter, styles, closing),
            title=f"{letter.contact.name} — Cover letter".strip(" —") or "Cover letter",
            author=letter.contact.name,
            subject="Cover letter",
        )
        result = RenderResult.from_path(out, engine=engine, template=self.name)
        self.logger.info("html.cover_letter_rendered_pdf", path=str(out), engine=engine)
        return result

    async def _write_pdf(
        self,
        html: str,
        out: Path,
        *,
        settings: RenderSettings,
        options: dict[str, Any] | None,
        story_factory: Callable[[dict[str, Any]], list[Any]],
        title: str,
        author: str,
        subject: str,
    ) -> str:
        """Produce a PDF with whichever backend is available, and report which one ran.

        WeasyPrint and reportlab are both synchronous and CPU-bound, so each runs in a worker
        thread: a resume render takes tens to hundreds of milliseconds and blocking the event
        loop for that long would stall every other job in the pipeline.

        Args:
            html: The rendered HTML, used by the WeasyPrint backend.
            out: Destination path.
            settings: Resolved font size and margin.
            options: The caller's options, read for ``pdf_backend``.
            story_factory: Callable taking the style mapping and returning the Platypus story,
                used by the reportlab backend.
            title: PDF document title.
            author: PDF author metadata.
            subject: PDF subject metadata.

        Returns:
            :data:`WEASYPRINT_ENGINE` or :data:`REPORTLAB_ENGINE`.

        Raises:
            DocumentRenderError: If ``pdf_backend`` names something unknown, a pinned backend
                is unavailable, or neither backend is installed.
        """
        backend = self._backend(options)
        out.parent.mkdir(parents=True, exist_ok=True)

        if backend in (None, WEASYPRINT_ENGINE) and weasyprint_available():
            try:
                await asyncio.to_thread(_weasyprint_write, html, out)
            except DocumentRenderError:
                if backend == WEASYPRINT_ENGINE:
                    raise
                # Installed but broken — a mismatched Pango/Cairo is common on Windows.
                # The floor exists precisely for this.
                self.logger.warning("html.weasyprint_failed", fallback=REPORTLAB_ENGINE)
            else:
                return WEASYPRINT_ENGINE
        elif backend == WEASYPRINT_ENGINE:
            raise DocumentRenderError(
                "pdf_backend='weasyprint' was requested but WeasyPrint is not installed. "
                "Install it with `pip install weasyprint`, or drop the option to fall back "
                "to reportlab."
            )

        styles = _reportlab_styles(settings)
        await asyncio.to_thread(
            _reportlab_build,
            story_factory(styles),
            out,
            settings=settings,
            title=title,
            author=author,
            subject=subject,
        )
        return REPORTLAB_ENGINE

    def _backend(self, options: dict[str, Any] | None) -> str | None:
        """Resolve ``options["pdf_backend"]``.

        Args:
            options: The caller's options mapping.

        Returns:
            The pinned backend name, or ``None`` to auto-detect.

        Raises:
            DocumentRenderError: If the value is not one of :data:`PDF_BACKENDS`.
        """
        requested = (options or {}).get("pdf_backend")
        if requested is None:
            return None
        if isinstance(requested, str) and requested.strip().lower() in PDF_BACKENDS:
            return requested.strip().lower()
        raise DocumentRenderError(
            f"pdf_backend must be one of {sorted(PDF_BACKENDS)}, got {requested!r}"
        )

    async def healthcheck(self) -> bool:
        """Report whether this template can render right now.

        Returns:
            ``True`` when Jinja2 imports, both template files exist, and at least one PDF
            backend is importable.
        """
        for name in (RESUME_TEMPLATE, COVER_LETTER_TEMPLATE):
            if not (TEMPLATE_DIR / name).is_file():
                self.logger.warning("html.template_missing", template=name)
                return False
        try:
            html_environment()
        except DocumentRenderError as exc:
            self.logger.warning("html.unhealthy", error=str(exc))
            return False
        if weasyprint_available():
            return True
        if importlib.util.find_spec("reportlab") is not None:
            return True
        self.logger.warning("html.no_pdf_backend")
        return False
