"""Rendering — turning a :class:`~app.documents.models.ResumeDocument` into a file.

``docs/CONTRACTS.md`` §11. This module owns three things and delegates everything else to a
template plugin:

**The template contract.** :class:`TemplatePlugin` is what a LaTeX, DOCX, HTML or Markdown
renderer implements: one abstract :meth:`~TemplatePlugin.render` method, a declared set of
:attr:`~TemplatePlugin.formats`, and a :class:`~app.plugins.base.PluginMeta` with
``kind=PluginKind.TEMPLATE``. Templates are resolved through
:func:`app.plugins.registry.registry` and never imported directly (golden rule #5), which
is what :func:`get_template` is for.

**The one-page shrink loop.** :func:`render_resume` is the whole point of the module. A
resume that spills onto a second page is a resume that does not get read, so the loop
renders, counts pages, and — while it is over budget — walks :data:`SHRINK_LADDER`: smaller
type, then tighter margins, and only when both have run out, fewer bullets. It never goes
below :data:`MIN_FONT_SIZE_PT`, because a resume nobody can read is worse than one that lost
a bullet, and it raises rather than quietly shipping a two-page document.

**LaTeX escaping.** :func:`escape_latex` is mandatory on every model-produced string that
reaches a LaTeX template. The order matters and is not negotiable: the backslash is handled
first, and handled *once*, or every subsequent escape re-escapes the backslashes the earlier
ones introduced and the whole document turns into ``\\textbackslash{}``. A user named
``O'Brien & Sons``, a project called ``C#``, a bullet claiming ``100% uptime`` — each of
these breaks an unescaped document at exactly the moment it matters, which is while an
application is being submitted.

Escaping is applied by the *template*, not here: :func:`render_resume` hands a DOCX renderer
the same document it hands a LaTeX one, and pre-escaping would corrupt the former.
:func:`escape_document` is the helper the LaTeX templates call, and it marks its output so
that escaping twice is impossible.
"""

from __future__ import annotations

import abc
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.documents.models import (
    Contact,
    CoverLetterDocument,
    ResumeDocument,
    ResumeEntry,
    ResumeSection,
    lines_per_page,
)
from app.models.enums import DocumentKind, PluginKind
from app.plugins.base import BasePlugin, PluginDisabled, PluginError, PluginMeta, PluginNotFound
from app.plugins.registry import registry

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = [
    "COVER_LETTER_META_KEY",
    "DEFAULT_FONT_SIZE_PT",
    "DEFAULT_MARGIN_IN",
    "DEFAULT_TEMPLATE",
    "DOCUMENT_KIND_META_KEY",
    "DocumentRenderError",
    "ESCAPED_META_KEY",
    "LATEX_ESCAPE_ORDER",
    "MIN_BULLET_DROP",
    "MIN_FONT_SIZE_PT",
    "OPTION_ATTEMPT",
    "OPTION_FONT_SIZE",
    "OPTION_MARGIN_IN",
    "OPTION_MAX_PAGES",
    "OPTION_STEP",
    "RenderResult",
    "SHRINK_LADDER",
    "ShrinkStep",
    "TemplatePlugin",
    "UNDERESTIMATE_FLOOR_LINES",
    "cover_letter_to_resume_document",
    "escape_document",
    "escape_latex",
    "escape_latex_dict",
    "get_template",
    "is_escaped",
    "page_count",
    "render_cover_letter",
    "render_resume",
    "with_page_count",
]

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: The template every fallback lands on. ``modern`` is a built-in LaTeX template and is the
#: default value of ``settings.resume_template``, so it is the one name that must exist.
DEFAULT_TEMPLATE: Final[str] = "modern"

#: Body font size of the first shrink attempt, and the default a template should assume when
#: it is handed no options at all.
DEFAULT_FONT_SIZE_PT: Final[float] = 10.5

#: Page margin of the first shrink attempt, in inches.
DEFAULT_MARGIN_IN: Final[float] = 0.5

#: **The readability floor.** The shrink loop never typesets a resume smaller than this.
#: Below roughly 9.5pt a printed resume stops being comfortably legible, and a resume nobody
#: can read is worse than one that lost a bullet — so the ladder drops content instead.
MIN_FONT_SIZE_PT: Final[float] = 9.5

#: The fewest bullets the drop rung will remove once it decides to remove any. Removing one
#: bullet almost never changes the page count, so a rung that fires at all fires properly.
MIN_BULLET_DROP: Final[int] = 2

#: How many lines of overshoot to assume when the estimator says the document fits but the
#: renderer has already produced too many pages. The estimator is a heuristic and the page
#: count is a fact; when they disagree, the fact wins and the drop rung removes enough to
#: matter (roughly a tenth of a page) instead of trusting an estimate reality has refuted.
UNDERESTIMATE_FLOOR_LINES: Final[int] = 6

#: Keys of the ``options`` mapping :meth:`TemplatePlugin.render` receives. Named constants
#: because the shrink loop writes them and every template reads them — a typo on either side
#: would silently disable one-page enforcement.
OPTION_FONT_SIZE: Final[str] = "font_size"
OPTION_MARGIN_IN: Final[str] = "margin_in"
OPTION_MAX_PAGES: Final[str] = "max_pages"
OPTION_ATTEMPT: Final[str] = "attempt"
OPTION_STEP: Final[str] = "step"

#: ``meta`` key marking a document that :func:`escape_document` has already escaped. Present
#: only on the escaped *copy*; the stored ``content_json`` never carries it.
ESCAPED_META_KEY: Final[str] = "_latex_escaped"

#: ``meta`` key naming what kind of document this is, so a template that knows how to set a
#: letter can tell one from a resume. Values are :class:`~app.models.enums.DocumentKind`.
DOCUMENT_KIND_META_KEY: Final[str] = "document_kind"

#: ``meta`` key holding the letter-specific fields when a
#: :class:`~app.documents.models.CoverLetterDocument` is projected onto a
#: :class:`~app.documents.models.ResumeDocument` — see
#: :func:`cover_letter_to_resume_document`.
COVER_LETTER_META_KEY: Final[str] = "cover_letter"

#: How much of an engine's stderr to show in an exception message. The full text stays on
#: :attr:`DocumentRenderError.stderr`; a LaTeX log is thousands of lines and the error is
#: always at the end.
STDERR_TAIL_CHARS: Final[int] = 2000


# --------------------------------------------------------------------------------------
# LaTeX escaping
# --------------------------------------------------------------------------------------

#: The escape table, **in the order the contract specifies**: backslash first, then the
#: brace/sigil group, with ``~`` and ``^`` mapped to their text-mode control sequences
#: rather than to accents.
#:
#: The order is load-bearing but the implementation does not iterate it. Applying these
#: pairwise with :meth:`str.replace` is the classic way to get this wrong: escaping ``\``
#: to ``\textbackslash{}`` *introduces* braces, which the very next replacement would
#: escape again, turning ``a\b`` into ``a\textbackslash\{\}b``. :func:`escape_latex`
#: therefore does a single regex pass, so each source character is translated exactly once
#: and the backslash rule genuinely applies first — to the input, and to nothing else.
LATEX_ESCAPE_ORDER: Final[tuple[tuple[str, str], ...]] = (
    ("\\", r"\textbackslash{}"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("$", r"\$"),
    ("&", r"\&"),
    ("#", r"\#"),
    ("^", r"\textasciicircum{}"),
    ("_", r"\_"),
    ("~", r"\textasciitilde{}"),
    ("%", r"\%"),
)

_LATEX_ESCAPES: Final[dict[str, str]] = dict(LATEX_ESCAPE_ORDER)

_LATEX_PATTERN: Final[re.Pattern[str]] = re.compile(
    "[" + re.escape("".join(_LATEX_ESCAPES)) + "]"
)


def escape_latex(s: str) -> str:
    r"""Escape *s* so it can be dropped into a LaTeX document verbatim.

    Mandatory on every model-produced string reaching a LaTeX template
    (``docs/CONTRACTS.md`` §11). The ten characters in :data:`LATEX_ESCAPE_ORDER` are
    translated in a single pass, backslash first, so nothing is escaped twice.

    Examples:
        >>> escape_latex("O'Brien & Sons")
        "O'Brien \\& Sons"
        >>> escape_latex("C#")
        'C\\#'
        >>> escape_latex("100% uptime")
        '100\\% uptime'
        >>> escape_latex("a_b^c")
        'a\\_b\\textasciicircum{}c'

    Args:
        s: The raw string. ``None`` and non-strings are coerced rather than raising, because
            this sits on the path between an LLM response and a PDF and must not be the
            thing that fails.

    Returns:
        The escaped string, safe for LaTeX text mode.
    """
    text = s if isinstance(s, str) else ("" if s is None else str(s))
    return _LATEX_PATTERN.sub(lambda match: _LATEX_ESCAPES[match.group()], text)


def escape_latex_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with every string value LaTeX-escaped, recursively.

    Descends into nested mappings, lists and tuples; leaves numbers, booleans and ``None``
    alone. **Keys are not escaped** — they are template lookup keys, and rewriting
    ``links["portfolio"]`` would break the very template that is asking for it. Any key
    intended to be *printed* must be escaped by the template at the point of printing.

    Args:
        data: The mapping to escape.

    Returns:
        A new dictionary; *data* is not modified.
    """
    return {key: _escape_value(value) for key, value in data.items()}


def _escape_value(value: Any) -> Any:
    """Escape one value of arbitrary shape, recursing through containers.

    Args:
        value: A string, container, or scalar.

    Returns:
        The escaped equivalent, preserving list/tuple/dict structure.
    """
    if isinstance(value, str):
        return escape_latex(value)
    if isinstance(value, Mapping):
        return escape_latex_dict(value)
    if isinstance(value, (list, tuple)):
        escaped = [_escape_value(item) for item in value]
        return tuple(escaped) if isinstance(value, tuple) else escaped
    return value


def is_escaped(doc: ResumeDocument) -> bool:
    """Return whether *doc* has already been through :func:`escape_document`.

    Args:
        doc: The document to inspect.

    Returns:
        ``True`` when the escape marker is present in :attr:`~ResumeDocument.meta`.
    """
    return bool(doc.meta.get(ESCAPED_META_KEY))


def escape_document(doc: ResumeDocument) -> ResumeDocument:
    """Return a deep copy of *doc* with every printable string LaTeX-escaped.

    What a LaTeX template calls once, at the top of its render, so the rest of its code can
    interpolate without thinking about it. The original is never modified — it is the thing
    that gets persisted to ``ResumeVersion.content_json`` and must stay clean (golden rule
    #6).

    Escaped: the contact block (including link URLs, where a stray ``%`` would comment out
    the rest of the line and a stray ``#`` would break ``\\href``), the summary, every
    heading, entry header field and bullet, and the skills line.

    Not escaped: :attr:`~ResumeDocument.meta`, which is machine-facing generation data
    rather than printable text, and :attr:`~ResumeEntry.fact_ids`, which are identifiers.
    A template that prints something out of ``meta`` must escape it itself.

    Calling this on an already-escaped document returns an unchanged copy rather than
    double-escaping it, so a template that escapes defensively and a caller that escaped
    eagerly cannot conspire to ruin the output.

    Args:
        doc: The document to escape.

    Returns:
        A new :class:`~app.documents.models.ResumeDocument`, carrying
        :data:`ESCAPED_META_KEY` in its metadata.
    """
    if is_escaped(doc):
        return doc.model_copy(deep=True)

    contact = Contact(
        name=escape_latex(doc.contact.name),
        email=escape_latex(doc.contact.email),
        phone=escape_latex(doc.contact.phone),
        location=escape_latex(doc.contact.location),
        # Values only: the keys are how a template looks a link up.
        links={key: escape_latex(url) for key, url in doc.contact.links.items()},
    )

    sections = [
        ResumeSection(
            heading=escape_latex(section.heading),
            entries=[
                ResumeEntry(
                    title=escape_latex(entry.title),
                    organization=escape_latex(entry.organization),
                    location=escape_latex(entry.location),
                    date_range=escape_latex(entry.date_range),
                    bullets=[escape_latex(bullet) for bullet in entry.bullets],
                    fact_ids=list(entry.fact_ids),
                )
                for entry in section.entries
            ],
        )
        for section in doc.sections
    ]

    meta = dict(doc.meta)
    meta[ESCAPED_META_KEY] = True
    return ResumeDocument(
        contact=contact,
        summary=escape_latex(doc.summary),
        sections=sections,
        skills_line=escape_latex(doc.skills_line),
        meta=meta,
    )


# --------------------------------------------------------------------------------------
# Results and errors
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RenderResult:
    """What one successful render produced.

    Attributes:
        path: The file that was written.
        page_count: Pages in the output. ``0`` means *unknown*, not *empty* — a DOCX or
            Markdown render has no page count until something paginates it, and a PDF whose
            structure could not be parsed reports the same. :func:`render_resume` treats
            ``0`` as "cannot verify" and accepts the render rather than shrinking blindly.
        engine: The tool that produced the file — ``"tectonic"``, ``"pdflatex"``,
            ``"python-docx"``, ``"markdown"``. Feeds
            ``applicantos_documents_rendered_total{engine}``.
        template: Name of the :class:`TemplatePlugin` that rendered it.
        bytes_written: Size of :attr:`path` on disk.
    """

    path: Path
    page_count: int
    engine: str
    template: str
    bytes_written: int

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        engine: str,
        template: str,
        page_count: int | None = None,
    ) -> RenderResult:
        """Build a result by measuring a file that was just written.

        The convenience every :class:`TemplatePlugin` would otherwise reimplement: it stats
        the file and, for a PDF, counts the pages.

        Args:
            path: The rendered file.
            engine: The tool that produced it.
            template: The template plugin's name.
            page_count: Override the measured count. Leave as ``None`` to count PDFs and
                report ``0`` (unknown) for every other format.

        Returns:
            A populated :class:`RenderResult`.

        Raises:
            DocumentRenderError: If *path* does not exist — a template that reports success
                without writing a file is a bug that must not reach the browser step.
        """
        resolved = Path(path)
        if not resolved.is_file():
            raise DocumentRenderError(
                f"template {template!r} reported success but wrote no file at {resolved}",
                engine=engine,
                template=template,
            )
        if page_count is None:
            page_count = page_count_of(resolved) if resolved.suffix.lower() == ".pdf" else 0
        return cls(
            path=resolved,
            page_count=max(0, page_count),
            engine=engine,
            template=template,
            bytes_written=resolved.stat().st_size,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready description, for logs and ``ResumeVersion`` metadata."""
        return {
            "path": str(self.path),
            "page_count": self.page_count,
            "engine": self.engine,
            "template": self.template,
            "bytes_written": self.bytes_written,
        }


class DocumentRenderError(PluginError):
    """A document could not be rendered.

    Carries the engine's own diagnostics when there are any: a LaTeX failure is only ever
    debuggable from ``tectonic``'s log, and losing it turns "the PDF didn't build" into an
    afternoon. Derives from :class:`~app.plugins.base.PluginError` so that a caller catching
    plugin failures catches this too.

    Attributes:
        engine: The engine that failed, when known.
        template: The template that was rendering, when known.
        stderr: The engine's captured stderr, in full.
        page_count: The page count that was measured, when the failure was a page-limit
            failure rather than a crash.
    """

    def __init__(
        self,
        message: str,
        *,
        engine: str | None = None,
        template: str | None = None,
        stderr: str | None = None,
        page_count: int | None = None,
    ) -> None:
        """Record the failure alongside whatever diagnostics the engine produced.

        Args:
            message: Human-readable explanation.
            engine: The rendering engine involved.
            template: The template plugin involved.
            stderr: The engine's stderr output, kept in full.
            page_count: The measured page count, for page-limit failures.
        """
        super().__init__(message, kind=PluginKind.TEMPLATE, name=template)
        self.message = message
        self.engine = engine
        self.template = template
        self.stderr = stderr
        self.page_count = page_count

    def __str__(self) -> str:
        """Return the message with the tail of the engine's stderr appended, if any."""
        if not self.stderr:
            return self.message
        tail = self.stderr.strip()
        if len(tail) > STDERR_TAIL_CHARS:
            tail = "…" + tail[-STDERR_TAIL_CHARS:]
        label = self.engine or "engine"
        return f"{self.message}\n--- {label} stderr ---\n{tail}"


# --------------------------------------------------------------------------------------
# The template plugin contract
# --------------------------------------------------------------------------------------


class TemplatePlugin(BasePlugin, abc.ABC):
    """A document renderer (``PluginKind.TEMPLATE``).

    Subclasses declare their own ``meta`` and the formats they can emit, then implement
    :meth:`render`::

        @plugin
        class ModernTemplate(TemplatePlugin):
            meta = PluginMeta(
                kind=PluginKind.TEMPLATE,
                name="modern",
                display_name="Modern",
                capabilities=frozenset({"pdf", "cover_letter"}),
            )
            formats = frozenset({"pdf", "tex"})

            async def render(self, doc, out, *, fmt="pdf", options=None):
                opts = self.merge_options(options)
                body = build_tex(escape_document(doc), opts)
                ...

    Implementation notes for template authors:

    * **Honour the options.** :data:`OPTION_FONT_SIZE` and :data:`OPTION_MARGIN_IN` are how
      :func:`render_resume` enforces the page limit. A template that ignores them turns the
      shrink loop into four identical renders and a raised exception.
    * **Escape at the top.** LaTeX templates call :func:`escape_document` once and work
      from the result. Non-LaTeX templates must not.
    * **Report honestly.** Return ``page_count=0`` when the format has no page count rather
      than guessing 1; :func:`RenderResult.from_path` already does the right thing.
    * **Keep third-party imports lazy**, inside the method that uses them.

    Attributes:
        formats: The formats this template can emit, e.g. ``{"pdf", "tex"}``. Checked by
            :func:`render_resume` before anything is written.
    """

    meta: ClassVar[PluginMeta]
    formats: ClassVar[frozenset[str]] = frozenset({"pdf"})

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the template.

        Args:
            settings: Application settings; :attr:`~app.config.settings.Settings.pdf_engine`
                and :attr:`~app.config.settings.Settings.latex_binary` are the ones that
                matter here.
            **kw: Extra options, kept on ``self.options``.
        """
        super().__init__(settings, **kw)

    # -- the contract ------------------------------------------------------------------

    @abc.abstractmethod
    async def render(
        self,
        doc: ResumeDocument,
        out: Path,
        *,
        fmt: str = "pdf",
        options: dict[str, Any] | None = None,
    ) -> RenderResult:
        """Render *doc* to *out*.

        Args:
            doc: The document to typeset. Never modified.
            out: Destination path. The parent directory may not exist yet; call
                :meth:`resolve_output` to create it and normalise the suffix.
            fmt: One of :attr:`formats`.
            options: Layout options, including :data:`OPTION_FONT_SIZE` and
                :data:`OPTION_MARGIN_IN`. Merge them over the defaults with
                :meth:`merge_options`.

        Returns:
            A :class:`RenderResult` describing the file that was written.

        Raises:
            DocumentRenderError: If the engine failed, carrying its stderr.
        """

    async def render_cover_letter(
        self,
        letter: CoverLetterDocument,
        out: Path,
        *,
        fmt: str = "pdf",
        options: dict[str, Any] | None = None,
    ) -> RenderResult:
        """Render a cover letter to *out*.

        Not abstract, so that every template supports letters the moment it supports
        resumes. The default projects the letter onto a
        :class:`~app.documents.models.ResumeDocument` with
        :func:`cover_letter_to_resume_document` and calls :meth:`render`; a template that
        wants proper letter typography — a real salutation, block paragraphs, a sign-off —
        overrides this and reads the letter fields out of
        ``doc.meta[``:data:`COVER_LETTER_META_KEY```]``.

        Args:
            letter: The letter to typeset.
            out: Destination path.
            fmt: One of :attr:`formats`.
            options: Layout options, as for :meth:`render`.

        Returns:
            A :class:`RenderResult` describing the file that was written.

        Raises:
            DocumentRenderError: If the engine failed.
        """
        projected = cover_letter_to_resume_document(letter)
        return await self.render(projected, out, fmt=fmt, options=options)

    # -- helpers for implementers -------------------------------------------------------

    def supports(self, fmt: str) -> bool:
        """Return whether this template can emit *fmt*.

        Args:
            fmt: A format name such as ``"pdf"``.
        """
        return fmt.lower().lstrip(".") in self.formats

    def default_options(self) -> dict[str, Any]:
        """Return the layout options assumed when the caller supplies none."""
        return {
            OPTION_FONT_SIZE: DEFAULT_FONT_SIZE_PT,
            OPTION_MARGIN_IN: DEFAULT_MARGIN_IN,
            OPTION_MAX_PAGES: self.settings.resume_max_pages,
        }

    def merge_options(self, options: Mapping[str, Any] | None) -> dict[str, Any]:
        """Layer caller options over :meth:`default_options`.

        Args:
            options: The mapping handed to :meth:`render`, possibly ``None``.

        Returns:
            A new dictionary; the caller's mapping is not modified.
        """
        merged = self.default_options()
        if options:
            merged.update(options)
        return merged

    def resolve_output(self, out: Path, fmt: str) -> Path:
        """Normalise a destination path and make sure its directory exists.

        Args:
            out: The requested destination.
            fmt: The format being written; used as the suffix when *out* has none or has a
                different one.

        Returns:
            An absolute path with the right suffix, whose parent directory now exists.
        """
        suffix = "." + fmt.lower().lstrip(".")
        resolved = Path(out)
        if resolved.suffix.lower() != suffix:
            resolved = resolved.with_suffix(suffix)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved


# --------------------------------------------------------------------------------------
# Template resolution
# --------------------------------------------------------------------------------------


def get_template(name: str | None = None) -> TemplatePlugin:
    """Return the template plugin called *name*, falling back to :data:`DEFAULT_TEMPLATE`.

    An unknown or disabled template is a configuration problem, not a reason to abandon an
    application: a user preference naming a template a later version removed would otherwise
    strand every resume they generate. The fallback is logged at warning level so the
    misconfiguration is visible.

    Args:
        name: Template name. ``None`` uses ``settings.resume_template``.

    Returns:
        The shared instance from :data:`app.plugins.registry.registry`.

    Raises:
        DocumentRenderError: If neither the requested template nor
            :data:`DEFAULT_TEMPLATE` can be resolved — at that point nothing can be
            rendered at all.
    """
    from app.config.settings import get_settings

    requested = (name or get_settings().resume_template or DEFAULT_TEMPLATE).strip()

    resolved = _lookup_template(requested)
    if resolved is not None:
        return resolved

    if requested.lower() != DEFAULT_TEMPLATE:
        logger.warning(
            "documents.template_fallback",
            requested=requested,
            fallback=DEFAULT_TEMPLATE,
            available=registry.names(PluginKind.TEMPLATE),
        )
        fallback = _lookup_template(DEFAULT_TEMPLATE)
        if fallback is not None:
            return fallback

    raise DocumentRenderError(
        f"no usable template plugin: {requested!r} is unavailable and so is the "
        f"{DEFAULT_TEMPLATE!r} fallback "
        f"(registered: {registry.names(PluginKind.TEMPLATE) or 'none'})",
        template=requested,
    )


def _lookup_template(name: str) -> TemplatePlugin | None:
    """Resolve one template name through the plugin registry.

    Loads the built-in plugins on first use, so a caller that renders a resume without
    having gone through the FastAPI lifespan still gets a working renderer.

    Args:
        name: The template name.

    Returns:
        The instance, or ``None`` when it is missing, disabled, broken, or turns out not to
        be a :class:`TemplatePlugin` at all.
    """
    if not registry.names(PluginKind.TEMPLATE):
        from app.plugins.loader import load_all

        load_all()

    try:
        instance = registry.get(PluginKind.TEMPLATE, name)
    except (PluginNotFound, PluginDisabled) as exc:
        logger.debug("documents.template_unavailable", template=name, error=str(exc))
        return None
    except PluginError as exc:
        logger.warning("documents.template_load_failed", template=name, error=str(exc))
        return None

    if not isinstance(instance, TemplatePlugin):
        logger.warning(
            "documents.template_wrong_type",
            template=name,
            actual=type(instance).__name__,
        )
        return None
    return instance


# --------------------------------------------------------------------------------------
# Page counting
# --------------------------------------------------------------------------------------

#: Matches a page object in an uncompressed PDF body. The negative lookahead is the
#: ``/Type\s*/Page[^s]`` idiom from the contract, written so that a page object sitting at
#: the very end of the file still counts: ``/Pages`` (the page *tree*) must not match, but
#: end-of-input must not disqualify a real ``/Page`` either.
_PDF_PAGE_RE: Final[re.Pattern[bytes]] = re.compile(rb"/Type\s*/Page(?![s])")


def page_count(pdf_path: Path) -> int:
    """Return how many pages the PDF at *pdf_path* has.

    Two strategies, in order. ``pypdf`` parses the document properly and is imported lazily,
    so a machine without it still works. Failing that, the raw bytes are scanned for page
    objects — crude, defeated by object-stream compression, but free and correct for the
    output of every LaTeX engine the built-in templates use.

    **Never raises.** This function is called in the middle of the shrink loop and on the
    error path; a malformed or half-written PDF must produce a number, not an exception.
    ``0`` means "could not tell", and :func:`render_resume` treats that as unverifiable
    rather than as a failure.

    Args:
        pdf_path: Path to the PDF.

    Returns:
        The page count, or ``0`` when it could not be determined.
    """
    path = Path(pdf_path)
    if not path.is_file():
        logger.warning("documents.page_count_missing_file", path=str(path))
        return 0

    counted = _page_count_pypdf(path)
    if counted > 0:
        return counted

    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("documents.page_count_unreadable", path=str(path), error=str(exc))
        return 0

    counted = len(_PDF_PAGE_RE.findall(raw))
    if counted == 0:
        logger.warning(
            "documents.page_count_unknown",
            path=str(path),
            size_bytes=len(raw),
            reason="no uncompressed page objects found",
        )
    return counted


#: Alias used inside this module, so :meth:`RenderResult.from_path` can call the function
#: without its ``page_count`` parameter name shadowing it.
page_count_of = page_count


def _page_count_pypdf(path: Path) -> int:
    """Count pages with ``pypdf``, returning ``0`` if that is not possible.

    Args:
        path: Path to the PDF.

    Returns:
        The page count, or ``0`` when ``pypdf`` is absent or the file defeats it.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.debug("documents.pypdf_absent", path=str(path))
        return 0

    try:
        return len(PdfReader(str(path)).pages)
    except Exception as exc:  # noqa: BLE001 - a page count must never break a render
        logger.warning("documents.pypdf_failed", path=str(path), error=str(exc))
        return 0


# --------------------------------------------------------------------------------------
# The shrink ladder
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShrinkStep:
    """One rung of :data:`SHRINK_LADDER`.

    Attributes:
        font_size: Body font size in points. Never below :data:`MIN_FONT_SIZE_PT`.
        margin_in: Page margin in inches.
        drop_bullets: Whether this rung may remove content. ``0`` means typography only;
            a positive value is the *minimum* number of bullets to drop — the loop computes
            the real number from how far over the limit the last render came in.
        label: Short name for the log line, so an operator reading
            ``documents.render_attempt`` can see which lever was pulled.
    """

    font_size: float
    margin_in: float
    drop_bullets: int = 0
    label: str = ""

    @property
    def drops_content(self) -> bool:
        """Whether this rung removes bullets rather than only tightening typography."""
        return self.drop_bullets > 0

    def as_options(self, max_pages: int, attempt: int) -> dict[str, Any]:
        """Return the ``options`` mapping a template receives for this rung.

        Args:
            max_pages: The page budget being enforced.
            attempt: 1-based attempt number.

        Returns:
            The option dictionary, keyed by the ``OPTION_*`` constants.
        """
        return {
            OPTION_FONT_SIZE: self.font_size,
            OPTION_MARGIN_IN: self.margin_in,
            OPTION_MAX_PAGES: max_pages,
            OPTION_ATTEMPT: attempt,
            OPTION_STEP: self.label,
        }


#: **The shrink ladder.** Tried in order by :func:`render_resume` until the document fits.
#:
#: Typography first, content last, and the type stops shrinking at
#: :data:`MIN_FONT_SIZE_PT`: rungs three and four are the same size because there is nowhere
#: smaller to go that a human would still read. Exposed as a module constant so it is
#: testable and tunable without editing the loop — but any edit is checked against the
#: readability floor at import time.
SHRINK_LADDER: Final[tuple[ShrinkStep, ...]] = (
    ShrinkStep(font_size=10.5, margin_in=0.50, drop_bullets=0, label="baseline"),
    ShrinkStep(font_size=10.0, margin_in=0.45, drop_bullets=0, label="tighten"),
    ShrinkStep(font_size=9.5, margin_in=0.40, drop_bullets=0, label="minimum-type"),
    ShrinkStep(
        font_size=9.5,
        margin_in=0.40,
        drop_bullets=MIN_BULLET_DROP,
        label="drop-bullets",
    ),
)


def _validate_ladder(ladder: Sequence[ShrinkStep]) -> None:
    """Check a ladder against the invariants the contract fixes.

    Runs at import so that a future edit to :data:`SHRINK_LADDER` that quietly breaks the
    readability floor fails loudly and immediately, rather than shipping 8pt resumes.

    Args:
        ladder: The ladder to check.

    Raises:
        ValueError: If the ladder is empty, goes below :data:`MIN_FONT_SIZE_PT`, uses a
            non-positive margin, or ever grows the type from one rung to the next.
    """
    if not ladder:
        raise ValueError("SHRINK_LADDER must have at least one step")
    previous: ShrinkStep | None = None
    for index, step in enumerate(ladder):
        if step.font_size < MIN_FONT_SIZE_PT:
            raise ValueError(
                f"SHRINK_LADDER[{index}] uses {step.font_size}pt, below the "
                f"{MIN_FONT_SIZE_PT}pt readability floor"
            )
        if step.margin_in <= 0:
            raise ValueError(f"SHRINK_LADDER[{index}] has a non-positive margin")
        if previous is not None and step.font_size > previous.font_size:
            raise ValueError(f"SHRINK_LADDER[{index}] grows the font instead of shrinking it")
        previous = step


_validate_ladder(SHRINK_LADDER)


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


async def render_resume(
    doc: ResumeDocument,
    out: Path,
    *,
    template: str | None = None,
    fmt: str = "pdf",
    max_pages: int = 1,
) -> RenderResult:
    """Render *doc* to *out*, enforcing the page limit by shrinking it until it fits.

    The loop that makes a one-page resume actually one page. Each rung of
    :data:`SHRINK_LADDER` is rendered and counted, and the first result inside *max_pages*
    wins. Typography is spent before content: 10.5pt at half-inch margins, then 10pt at
    0.45in, then 9.5pt at 0.4in — and only then, with the type already at the
    :data:`MIN_FONT_SIZE_PT` floor, bullets start coming off. How many come off is computed
    from the overshoot via :meth:`~app.documents.models.ResumeDocument.estimated_lines`, so
    a document that is one line long loses two bullets and one that is half a page long
    loses proportionally more.

    Bullets are dropped lowest-priority first, from a *copy* — the caller's document, which
    is what gets stored in ``ResumeVersion.content_json``, is never modified. Priorities come
    from ``doc.meta["priorities"]`` and default to document order.

    Every attempt is logged as ``documents.render_attempt`` with its font size, margin and
    resulting page count, so an operator can see exactly which lever ran out.

    Args:
        doc: The document to render.
        out: Destination path.
        template: Template name; ``None`` uses ``settings.resume_template``.
        fmt: Output format. Anything other than ``"pdf"`` has no meaningful page count, so
            the page limit is not enforced and a single render is performed.
        max_pages: The page budget. ``0`` or negative disables enforcement.

    Returns:
        The :class:`RenderResult` of the attempt that fit.

    Raises:
        DocumentRenderError: If the template cannot emit *fmt*, if the engine failed, or if
            the document still exceeded *max_pages* after the whole ladder — with the final
            page count named in the message.
    """
    plugin_instance = get_template(template)
    template_name = plugin_instance.name

    if not plugin_instance.supports(fmt):
        raise DocumentRenderError(
            f"template {template_name!r} cannot emit {fmt!r} "
            f"(supported: {', '.join(sorted(plugin_instance.formats)) or 'none'})",
            template=template_name,
        )

    enforce = fmt.lower() == "pdf" and max_pages > 0
    working = doc.model_copy(deep=True)
    priorities = doc.configured_priorities()

    log = logger.bind(template=template_name, fmt=fmt, out=str(out))
    if not enforce:
        log.info(
            "documents.page_limit_not_enforced",
            reason="non-pdf format" if fmt.lower() != "pdf" else "no page budget",
            max_pages=max_pages,
        )
        step = SHRINK_LADDER[0]
        return await _attempt(plugin_instance, working, out, fmt, step, max_pages, 1, log)

    dropped_total = 0
    result: RenderResult | None = None

    for attempt, step in enumerate(SHRINK_LADDER, start=1):
        if step.drops_content:
            measured = result.page_count if result is not None else 0
            wanted = _bullets_to_drop(working, step, max_pages, measured)
            dropped = working.drop_lowest_priority_bullets(wanted, priorities)
            dropped_total += dropped
            log.info(
                "documents.shrink_dropping_bullets",
                attempt=attempt,
                requested=wanted,
                dropped=dropped,
                dropped_total=dropped_total,
                bullets_remaining=working.total_bullets(),
            )
            if dropped == 0 and result is not None:
                # Nothing left that may be removed; another identical render would only
                # produce the same page count.
                log.warning("documents.shrink_exhausted", attempt=attempt)
                break

        result = await _attempt(
            plugin_instance, working, out, fmt, step, max_pages, attempt, log
        )

        if result.page_count <= 0:
            log.warning(
                "documents.page_limit_unverified",
                attempt=attempt,
                path=str(result.path),
            )
            return result

        if result.page_count <= max_pages:
            log.info(
                "documents.render_succeeded",
                attempt=attempt,
                page_count=result.page_count,
                font_size=step.font_size,
                margin_in=step.margin_in,
                bullets_dropped=dropped_total,
                bytes_written=result.bytes_written,
            )
            return result

    final_pages = result.page_count if result is not None else 0
    log.error(
        "documents.page_limit_exceeded",
        page_count=final_pages,
        max_pages=max_pages,
        attempts=len(SHRINK_LADDER),
        bullets_dropped=dropped_total,
    )
    raise DocumentRenderError(
        f"resume still renders as {final_pages} page(s) after {len(SHRINK_LADDER)} "
        f"attempts (limit {max_pages}); the smallest permitted type is "
        f"{MIN_FONT_SIZE_PT}pt and {dropped_total} bullet(s) were already dropped",
        engine=result.engine if result is not None else None,
        template=template_name,
        page_count=final_pages,
    )


async def _attempt(
    plugin_instance: TemplatePlugin,
    doc: ResumeDocument,
    out: Path,
    fmt: str,
    step: ShrinkStep,
    max_pages: int,
    attempt: int,
    log: Any,
) -> RenderResult:
    """Perform one render attempt and log its outcome.

    Args:
        plugin_instance: The resolved template.
        doc: The (possibly already shrunk) working document.
        out: Destination path.
        fmt: Output format.
        step: The ladder rung being tried.
        max_pages: The page budget, passed through to the template.
        attempt: 1-based attempt number.
        log: The bound logger from the caller.

    Returns:
        The template's :class:`RenderResult`.

    Raises:
        DocumentRenderError: If the template raised. A non-render exception is wrapped so
            that callers only ever have to catch one type.
    """
    options = step.as_options(max_pages, attempt)
    try:
        result = await plugin_instance.render(doc, Path(out), fmt=fmt, options=options)
    except DocumentRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalised into the document error type
        raise DocumentRenderError(
            f"template {plugin_instance.name!r} failed on attempt {attempt} "
            f"({step.label or 'unnamed step'}): {exc}",
            template=plugin_instance.name,
        ) from exc

    log.info(
        "documents.render_attempt",
        attempt=attempt,
        step=step.label,
        font_size=step.font_size,
        margin_in=step.margin_in,
        page_count=result.page_count,
        estimated_lines=doc.estimated_lines(step.font_size, step.margin_in),
        estimated_pages=round(doc.estimated_pages(step.font_size, step.margin_in), 2),
        bullets=doc.total_bullets(),
        engine=result.engine,
        bytes_written=result.bytes_written,
    )
    return result


def _bullets_to_drop(
    doc: ResumeDocument,
    step: ShrinkStep,
    max_pages: int,
    measured_pages: int,
) -> int:
    """Decide how many bullets to remove for a content-dropping rung.

    Converts the overshoot into a bullet count: how many lines too long the document is,
    divided by what one bullet costs. That keeps the loop proportionate — a document two
    lines over loses the minimum, and one that is genuinely half a page over loses enough
    to have a chance of fitting on the very next attempt, instead of gutting a resume to
    save one line.

    The estimate is cross-checked against reality. If the previous attempt actually
    rendered too many pages while :meth:`~app.documents.models.ResumeDocument.estimated_lines`
    claims the document fits, the estimator is wrong about this document and the overshoot
    is floored at :data:`UNDERESTIMATE_FLOOR_LINES` rather than trusting it.

    Args:
        doc: The current working document.
        step: The rung about to be rendered, whose font size and margin set the geometry.
        max_pages: The page budget.
        measured_pages: Page count of the previous attempt, or ``0`` when unknown.

    Returns:
        The number of bullets to remove, at least :data:`MIN_BULLET_DROP` and never more
        than the document holds.
    """
    capacity = lines_per_page(step.font_size, step.margin_in) * max(1, max_pages)
    excess = doc.estimated_lines(step.font_size, step.margin_in) - capacity
    if measured_pages > max_pages:
        excess = max(excess, UNDERESTIMATE_FLOOR_LINES)

    wanted = max(step.drop_bullets, MIN_BULLET_DROP)
    if excess > 0:
        per_bullet = doc.average_bullet_lines(step.font_size, step.margin_in)
        wanted = max(wanted, math.ceil(excess / per_bullet))
    return min(wanted, doc.total_bullets())


async def render_cover_letter(
    body: str | CoverLetterDocument,
    contact: Contact | None,
    out: Path,
    *,
    template: str | None = None,
    fmt: str = "pdf",
) -> RenderResult:
    """Render a cover letter to *out*.

    No shrink loop: a cover letter is prose, and the honest fix for one that runs long is a
    shorter letter, not 9.5pt type. The page count is still measured and logged so an
    over-long letter is visible in the run log.

    Args:
        body: The letter text, or a fully-populated
            :class:`~app.documents.models.CoverLetterDocument` when the caller knows the
            recipient, company, role and date. Passing the document is preferred — the
            plain-string form can only produce an unaddressed letter.
        contact: The sender's header block. Ignored when *body* is already a document that
            carries one; used to fill it in when that document's contact is empty.
        out: Destination path.
        template: Template name; ``None`` uses ``settings.resume_template`` so a letter
            matches the resume it is sent with.
        fmt: Output format.

    Returns:
        The :class:`RenderResult` describing the written file.

    Raises:
        DocumentRenderError: If no template can be resolved, the template cannot emit
            *fmt*, or the engine failed.
    """
    letter = _as_cover_letter(body, contact)

    plugin_instance = get_template(template)
    template_name = plugin_instance.name

    if not plugin_instance.supports(fmt):
        raise DocumentRenderError(
            f"template {template_name!r} cannot emit {fmt!r} "
            f"(supported: {', '.join(sorted(plugin_instance.formats)) or 'none'})",
            template=template_name,
        )

    options = {
        OPTION_FONT_SIZE: DEFAULT_FONT_SIZE_PT,
        OPTION_MARGIN_IN: DEFAULT_MARGIN_IN,
        OPTION_STEP: "cover-letter",
    }
    try:
        result = await plugin_instance.render_cover_letter(
            letter, Path(out), fmt=fmt, options=options
        )
    except DocumentRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalised into the document error type
        raise DocumentRenderError(
            f"template {template_name!r} failed to render the cover letter: {exc}",
            template=template_name,
        ) from exc

    logger.info(
        "documents.cover_letter_rendered",
        template=template_name,
        engine=result.engine,
        fmt=fmt,
        page_count=result.page_count,
        paragraphs=len(letter.paragraphs()),
        bytes_written=result.bytes_written,
        path=str(result.path),
    )
    return result


def _as_cover_letter(
    body: str | CoverLetterDocument,
    contact: Contact | None,
) -> CoverLetterDocument:
    """Coerce the two accepted forms of ``render_cover_letter``'s input into one document.

    Args:
        body: Letter text, or an already-built document.
        contact: The sender's header block, used when *body* carries none.

    Returns:
        A :class:`~app.documents.models.CoverLetterDocument`. The caller's document is
        copied rather than modified when its contact has to be filled in.
    """
    if isinstance(body, CoverLetterDocument):
        if contact is not None and not body.contact.name:
            return body.model_copy(update={"contact": contact}, deep=True)
        return body
    return CoverLetterDocument(contact=contact or Contact(), body=body or "")


def cover_letter_to_resume_document(letter: CoverLetterDocument) -> ResumeDocument:
    """Project a cover letter onto the document shape every template already understands.

    The bridge that lets :meth:`TemplatePlugin.render_cover_letter` have a working default.
    The letter becomes a single section whose one entry holds the paragraphs as "bullets",
    with the addressing details in the entry header — legible in any template, and clearly
    marked in :attr:`~app.documents.models.ResumeDocument.meta` so a template that knows
    what a letter is can lay it out properly instead:

    * ``meta[``:data:`DOCUMENT_KIND_META_KEY```]`` is
      :attr:`~app.models.enums.DocumentKind.COVER_LETTER`.
    * ``meta[``:data:`COVER_LETTER_META_KEY```]`` holds ``recipient``, ``company``,
      ``role``, ``date``, ``salutation`` and ``paragraphs``.

    Args:
        letter: The letter to project.

    Returns:
        An equivalent :class:`~app.documents.models.ResumeDocument`.
    """
    paragraphs = letter.paragraphs()
    entry = ResumeEntry(
        title=letter.role,
        organization=letter.company,
        location="",
        date_range=letter.date,
        bullets=paragraphs,
    )
    return ResumeDocument(
        contact=letter.contact,
        summary="",
        sections=[ResumeSection(heading=letter.salutation(), entries=[entry])],
        skills_line="",
        meta={
            DOCUMENT_KIND_META_KEY: DocumentKind.COVER_LETTER.value,
            COVER_LETTER_META_KEY: {
                "recipient": letter.recipient,
                "company": letter.company,
                "role": letter.role,
                "date": letter.date,
                "salutation": letter.salutation(),
                "paragraphs": paragraphs,
            },
        },
    )


def with_page_count(result: RenderResult, pages: int) -> RenderResult:
    """Return a copy of *result* with a corrected page count.

    Useful to a template that writes a file through a helper and only learns the true page
    count afterwards.

    Args:
        result: The result to copy.
        pages: The corrected count.

    Returns:
        A new :class:`RenderResult`.
    """
    return replace(result, page_count=max(0, pages))
