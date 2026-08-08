"""The ``modern`` and ``classic`` template plugins — LaTeX resumes and cover letters.

This is the default document path (``settings.pdf_engine == "latex"``) and the one that
produces the best-looking output: real typesetting, real kerning, a PDF that a hiring manager
reads without noticing the tool that made it.

**Two templates, one engine.** ``modern`` prints a centred header, a restrained accent colour
on section headings and their rules, and optional Font Awesome glyphs beside the contact
details. ``classic`` is pure black, left-aligned, uppercase headings, no icons — the safe
choice for a conservative employer or an ATS with a reputation for mangling decorated PDFs.
Both share :data:`app.documents.templates.cover_letter.tex.j2` for letters.

**Custom Jinja2 delimiters.** LaTeX is made of braces and percent signs, so the standard
``{{ }}`` / ``{% %}`` / ``{# #}`` would collide with the language being generated. The
environment built by :func:`latex_environment` uses ``<< >>`` / ``<% %>`` / ``<# #>`` instead,
with ``autoescape=False`` — HTML escaping would be actively wrong here — and
``trim_blocks=True`` so control structures do not litter the output with blank lines.

**Escaping is not optional.** ``autoescape=False`` means the *only* thing standing between an
LLM-authored bullet containing ``50% & rising`` and a LaTeX syntax error is
:func:`~app.documents.renderer.escape_document`, which every render path here calls before
touching a template. URLs take the other road: they are percent-encoded rather than
backslash-escaped, because ``hyperref`` wants a URL, not TeX.

**Engine resolution.** ``settings.latex_binary`` (default ``tectonic``) is tried first, then
``xelatex``, then ``pdflatex``. Tectonic is preferred because it downloads exactly the
packages a document needs instead of requiring a four-gigabyte TeX Live install, and because
it and XeTeX default to Unicode font encoding, which is what makes the PDF's text layer
extract cleanly — the property the whole ATS story rests on. When none of the three is on
``PATH`` the error tells the operator how to fix it, including the ``PDF_ENGINE=html`` escape
hatch that needs no system binaries at all.

**Failures come with context.** A LaTeX error message without its log is unactionable, so a
non-zero exit raises :class:`~app.documents.renderer.DocumentRenderError` carrying the last
:data:`LOG_TAIL_LINES` lines of the run.
"""

from __future__ import annotations

import asyncio
import functools
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.documents.markdown import (
    DEFAULT_CLOSING,
    DEFAULT_SKILLS_HEADING,
    DEFAULT_SUMMARY_HEADING,
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
from app.documents.renderer import (
    DocumentRenderError,
    RenderResult,
    TemplatePlugin,
    escape_document,
    escape_latex,
)
from app.models.enums import PluginKind
from app.plugins import PluginMeta, plugin

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from collections.abc import Sequence

    from jinja2 import Environment

__all__ = [
    "CONTACT_ICONS",
    "LATEX_ENGINE_PREFERENCE",
    "LATEX_FORMATS",
    "LATEX_TIMEOUT_SECONDS",
    "LOG_TAIL_LINES",
    "TEMPLATE_DIR",
    "ClassicTemplate",
    "ModernTemplate",
    "compile_latex",
    "latex_environment",
    "latex_url",
    "resolve_latex_binary",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Directory holding the ``.tex.j2`` sources. Resolved from this file so a source checkout
#: and an installed package both work.
TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

#: Jinja2 delimiters chosen so that LaTeX's own ``{``/``}``/``%`` survive untouched.
BLOCK_START: Final[str] = "<%"
BLOCK_END: Final[str] = "%>"
VARIABLE_START: Final[str] = "<<"
VARIABLE_END: Final[str] = ">>"
COMMENT_START: Final[str] = "<#"
COMMENT_END: Final[str] = "#>"

#: Formats these plugins can produce. ``tex`` exists so the desktop app can show the
#: generated source when a compile fails, and so a user can hand-edit before printing.
PDF_FORMAT: Final[str] = "pdf"
TEX_FORMAT: Final[str] = "tex"
LATEX_FORMATS: Final[frozenset[str]] = frozenset({PDF_FORMAT, TEX_FORMAT})

#: Reported as ``RenderResult.engine`` when only the source was written.
TEX_SOURCE_ENGINE: Final[str] = "latex-source"

#: Engines tried in order after ``settings.latex_binary``. Tectonic first (self-contained,
#: Unicode by default), then XeTeX (Unicode, needs a TeX distribution), then pdfTeX (last
#: resort: its default 8-bit font encoding extracts less cleanly).
LATEX_ENGINE_PREFERENCE: Final[tuple[str, ...]] = ("tectonic", "xelatex", "pdflatex")

#: Wall-clock budget for one compile. Tectonic's first run on a machine downloads packages,
#: which is the slow case this number is sized for.
LATEX_TIMEOUT_SECONDS: Final[float] = 180.0

#: How much of the log a failure carries. Forty lines reaches back past TeX's trailing
#: statistics to the ``!`` line that actually says what went wrong.
LOG_TAIL_LINES: Final[int] = 40

#: A TeX run is repeated at most this many times. Nothing in these templates uses a label or
#: a table of contents, so the second pass only ever happens if a package asks for it.
MAX_LATEX_PASSES: Final[int] = 2

#: Log fragments meaning "the output is not stable yet, run again".
RERUN_MARKERS: Final[tuple[str, ...]] = (
    "Rerun to get",
    "Rerun LaTeX",
    "Please rerun",
    "There were undefined references",
)

#: Base name given to the generated source inside the scratch directory.
JOB_NAME: Final[str] = "document"

#: Accent colour of the ``modern`` template — a deep, desaturated navy. Restrained on
#: purpose: it should read as "typeset with care", never as "designed".
MODERN_ACCENT_HEX: Final[str] = "1B3A5B"

#: The ``classic`` template's "accent", used only by the shared cover-letter template.
CLASSIC_ACCENT_HEX: Final[str] = "000000"

#: Font Awesome macros for the contact line, by :attr:`ContactItem.label`. Restricted to
#: glyphs that exist in every fontawesome5 release; an unknown label simply gets no icon.
#: ``location`` is deliberately absent — the map-marker macro name has moved between
#: releases, and a missing icon is a compile error rather than a cosmetic loss.
CONTACT_ICONS: Final[dict[str, str]] = {
    "email": r"\faEnvelope",
    "phone": r"\faPhone",
    "github": r"\faGithub",
    "gitlab": r"\faGitlab",
    "linkedin": r"\faLinkedin",
    "portfolio": r"\faGlobe",
    "website": r"\faGlobe",
    "blog": r"\faGlobe",
}

#: Characters percent-encoded before a URL reaches ``\href``. ``hyperref`` normalises most of
#: TeX's special characters inside its first argument, but ``%`` (comment) and ``#``
#: (parameter) are consumed by the tokenizer before it ever sees them, and a backslash or a
#: brace would unbalance the argument. Percent-encoding is the URL-native fix and needs no
#: LaTeX escaping at all.
URL_PERCENT_ENCODE: Final[dict[str, str]] = {
    "\\": "%5C",
    "%": "%25",
    "#": "%23",
    "{": "%7B",
    "}": "%7D",
    "^": "%5E",
    " ": "%20",
    '"': "%22",
}

#: PDF metadata strings are written into the document catalogue, where TeX escapes and
#: control characters are meaningless at best. Anything outside this set is dropped.
_PDF_STRING_ALLOWED: Final[re.Pattern[str]] = re.compile(r"[^\w \-.,'&@()/+:]", re.UNICODE)

#: Cap on a PDF metadata string, so a pathological name cannot bloat the catalogue.
_PDF_STRING_MAX: Final[int] = 200


# ======================================================================================
# The Jinja2 environment
# ======================================================================================


@functools.lru_cache(maxsize=1)
def latex_environment() -> Environment:
    """Return the process-wide Jinja2 environment for the ``.tex.j2`` templates.

    Built once and cached: constructing an :class:`~jinja2.Environment` compiles and caches
    templates, and rebuilding it per render would throw that away on every attempt of the
    shrink loop.

    Returns:
        An environment loading from :data:`TEMPLATE_DIR` with LaTeX-safe delimiters,
        ``autoescape=False`` and ``trim_blocks=True``, exactly as ``docs/CONTRACTS.md`` §11
        requires.

    Raises:
        DocumentRenderError: If Jinja2 is not installed. It is a hard dependency of every
            template plugin but a lazy import, so the message names the fix.
    """
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ImportError as exc:  # pragma: no cover - exercised only without jinja2
        raise DocumentRenderError(
            "Jinja2 is required to render LaTeX templates. Install it with "
            "`pip install jinja2`, or set PDF_ENGINE=docx to use the python-docx renderer."
        ) from exc

    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR), encoding="utf-8"),
        block_start_string=BLOCK_START,
        block_end_string=BLOCK_END,
        variable_start_string=VARIABLE_START,
        variable_end_string=VARIABLE_END,
        comment_start_string=COMMENT_START,
        comment_end_string=COMMENT_END,
        autoescape=False,
        trim_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )


def latex_url(url: str) -> str:
    """Percent-encode *url* so it can be dropped straight into ``\\href``.

    Args:
        url: An absolute URL from :func:`~app.documents.markdown.contact_items`.

    Returns:
        The URL with every character in :data:`URL_PERCENT_ENCODE` replaced by its escape.
        The result needs no LaTeX escaping and stays a valid URL, which
        ``escape_latex(url)`` would not — ``https://x.com/a_b`` escaped for TeX becomes
        ``https://x.com/a\\_b``, and that is what the reader's browser would be sent to.
    """
    return "".join(URL_PERCENT_ENCODE.get(char, char) for char in url.strip())


def _pdf_string(text: str) -> str:
    """Reduce *text* to something safe for the PDF document catalogue.

    ``pdftitle`` and ``pdfauthor`` are not typeset — they are strings in the PDF's metadata —
    so a TeX escape sequence in them prints literally in a reader's title bar, and an
    unbalanced brace breaks ``\\hypersetup`` outright.

    Args:
        text: Raw, unescaped text — typically the candidate's name.

    Returns:
        The text with anything outside :data:`_PDF_STRING_ALLOWED` removed, whitespace
        collapsed, and length capped at :data:`_PDF_STRING_MAX`.
    """
    cleaned = _PDF_STRING_ALLOWED.sub("", text)
    return " ".join(cleaned.split())[:_PDF_STRING_MAX]


# ======================================================================================
# Engine resolution and compilation
# ======================================================================================


def resolve_latex_binary(preferred: str | None = None) -> tuple[str, str]:
    """Find a usable LaTeX engine on ``PATH``.

    Args:
        preferred: ``settings.latex_binary``, tried before the built-in preference order. A
            full path is accepted, in which case its stem names the engine.

    Returns:
        ``(executable_path, engine_name)``. The engine name is one of
        :data:`LATEX_ENGINE_PREFERENCE` when recognised, otherwise the executable's stem; it
        selects the command line in :func:`_engine_argv` and is reported as
        ``RenderResult.engine``.

    Raises:
        DocumentRenderError: If none of the candidates resolves. The message names the
            candidates tried and both remedies — install Tectonic, or switch the pipeline to
            the pure-Python HTML renderer with ``PDF_ENGINE=html``.
    """
    candidates: list[str] = []
    for candidate in (preferred, *LATEX_ENGINE_PREFERENCE):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            engine = Path(candidate).stem.lower()
            logger.debug("latex.engine_resolved", engine=engine, binary=found)
            return found, engine

    raise DocumentRenderError(
        "No LaTeX engine found on PATH. Tried: "
        + ", ".join(candidates)
        + ". Install Tectonic (https://tectonic-typesetting.github.io — a single binary, no "
        "TeX distribution required) and it will be picked up automatically, or point "
        "LATEX_BINARY at an existing xelatex/pdflatex. To render without any system binary "
        "at all, set PDF_ENGINE=html, which uses the pure-pip WeasyPrint/reportlab path."
    )


def _engine_argv(engine: str, binary: str, tex_path: Path, workdir: Path) -> list[str]:
    """Return the command line for one compile pass.

    Args:
        engine: Engine name from :func:`resolve_latex_binary`.
        binary: Absolute path to the executable.
        tex_path: The generated ``.tex`` file.
        workdir: Scratch directory that also receives the output.

    Returns:
        The argument vector. Tectonic takes ``--outdir``; the TeX-family engines take
        ``-output-directory`` plus the flags that make a failure non-interactive and
        machine-readable — without ``-interaction=nonstopmode`` a bad character would block
        forever on a ``?`` prompt inside a subprocess with no stdin.
    """
    if engine == "tectonic":
        return [binary, "--outdir", str(workdir), "--keep-logs", str(tex_path)]
    return [
        binary,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-output-directory",
        str(workdir),
        str(tex_path),
    ]


def _log_tail(text: str, lines: int = LOG_TAIL_LINES) -> str:
    """Return the last *lines* non-empty lines of a compiler log.

    Args:
        text: The captured log.
        lines: How many lines to keep.

    Returns:
        The tail, joined by newlines. Blank lines are dropped first because TeX logs are
        mostly blank lines and they would otherwise consume the whole budget.
    """
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _read_log(workdir: Path, stdout: str) -> str:
    """Return the most informative log available for a failed compile.

    Args:
        workdir: The scratch directory, which may hold ``<JOB_NAME>.log``.
        stdout: Combined stdout and stderr from the process.

    Returns:
        The ``.log`` file when TeX managed to write one — it is far more detailed than the
        console output — otherwise the captured stdout/stderr. Tectonic often fails before
        writing a log, which is exactly when the stdout fallback matters.
    """
    log_path = workdir / f"{JOB_NAME}.log"
    if log_path.is_file():
        try:
            contents = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("latex.log_unreadable", path=str(log_path), error=str(exc))
        else:
            if contents.strip():
                return contents
    return stdout


async def _run_pass(
    argv: Sequence[str],
    workdir: Path,
    timeout: float,
    *,
    template: str | None = None,
) -> tuple[int, str]:
    """Run one compile pass and capture everything it printed.

    Args:
        argv: The command line from :func:`_engine_argv`.
        workdir: Working directory for the child process.
        timeout: Seconds to wait before killing it.
        template: Name of the calling template, recorded on any error raised.

    Returns:
        ``(returncode, combined_output)``.

    Raises:
        DocumentRenderError: If the executable cannot be started, or the compile exceeds
            *timeout*. A hung TeX run is a real failure mode — an unmatched brace with
            ``-interaction=nonstopmode`` unavailable will sit at a prompt forever — so the
            process is killed and reaped rather than left behind.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise DocumentRenderError(
            f"could not start LaTeX engine {argv[0]!r}: {exc}", template=template
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise DocumentRenderError(
            f"LaTeX compile timed out after {timeout:.0f}s using {argv[0]!r}. "
            "A resume should typeset in seconds; this usually means the engine is waiting "
            "on input or downloading a package bundle over a slow link.",
            template=template,
        ) from exc

    combined = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
    return process.returncode or 0, combined


async def compile_latex(
    source: str,
    out: Path,
    *,
    binary: str | None = None,
    timeout: float = LATEX_TIMEOUT_SECONDS,
    template: str | None = None,
) -> str:
    """Compile LaTeX *source* to a PDF at *out*.

    The whole run happens inside a throwaway directory: TeX litters ``.aux``, ``.log``,
    ``.out`` files beside its input, and a resume rendered into the user's documents folder
    must not leave those behind. Only the finished PDF is copied out.

    Args:
        source: A complete LaTeX document.
        out: Destination path for the PDF. Parent directories are created.
        binary: ``settings.latex_binary``; ``None`` means "use the preference order".
        timeout: Per-pass wall-clock budget in seconds.
        template: Name of the calling template, recorded on any error raised.

    Returns:
        The engine name that produced the PDF, for ``RenderResult.engine``.

    Raises:
        DocumentRenderError: If no engine is available, a pass exits non-zero, the engine
            exits zero without producing a PDF, or the result cannot be copied to *out*. The
            message carries the last :data:`LOG_TAIL_LINES` lines of the log, which is the
            part that says what went wrong; the full log stays on the structlog event, since
            everything before the ``!`` line is TeX announcing which fonts it loaded.
    """
    executable, engine = resolve_latex_binary(binary)

    with tempfile.TemporaryDirectory(prefix="applicantos-latex-") as tmp:
        workdir = Path(tmp)
        tex_path = workdir / f"{JOB_NAME}.tex"
        tex_path.write_text(source, encoding="utf-8")
        pdf_path = workdir / f"{JOB_NAME}.pdf"
        argv = _engine_argv(engine, executable, tex_path, workdir)

        for attempt in range(1, MAX_LATEX_PASSES + 1):
            code, output = await _run_pass(argv, workdir, timeout, template=template)
            log = _read_log(workdir, output)
            if code != 0:
                tail = _log_tail(log)
                logger.error(
                    "latex.compile_failed",
                    engine=engine,
                    exit_code=code,
                    pass_=attempt,
                    log_tail=tail,
                    log_bytes=len(log),
                )
                raise DocumentRenderError(
                    f"{engine} exited with code {code} while typesetting the document.\n"
                    f"Last {LOG_TAIL_LINES} lines of the log:\n{tail}",
                    engine=engine,
                    template=template,
                )
            if not any(marker in log for marker in RERUN_MARKERS):
                break
            logger.debug("latex.rerun_requested", engine=engine, pass_=attempt)

        if not pdf_path.is_file():
            raise DocumentRenderError(
                f"{engine} reported success but produced no PDF.\n"
                f"Last {LOG_TAIL_LINES} lines of the log:\n"
                f"{_log_tail(_read_log(workdir, ''))}",
                engine=engine,
                template=template,
            )

        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pdf_path, out)
        except OSError as exc:
            raise DocumentRenderError(
                f"could not write {out}: {exc}", engine=engine, template=template
            ) from exc

    logger.info("latex.compiled", engine=engine, path=str(out), bytes=out.stat().st_size)
    return engine


# ======================================================================================
# Template context
# ======================================================================================


def _contact_context(
    items: Sequence[ContactItem],
    *,
    with_icons: bool,
) -> list[dict[str, str]]:
    """Convert contact items into the escaped dictionaries the templates iterate.

    Args:
        items: Items from :func:`~app.documents.markdown.contact_items`, built from the
            *unescaped* contact so the URLs are still real URLs.
        with_icons: Whether to attach a Font Awesome macro. Only ``modern`` does.

    Returns:
        One dict per item with ``text`` (LaTeX-escaped), ``url`` (percent-encoded, empty for
        non-links) and ``icon`` (a macro name, or empty).
    """
    context: list[dict[str, str]] = []
    for item in items:
        context.append(
            {
                "text": escape_latex(item.text),
                "url": latex_url(item.url) if item.is_link else "",
                "icon": CONTACT_ICONS.get(item.label, "") if with_icons else "",
            }
        )
    return context


def _resume_context(
    doc: ResumeDocument,
    *,
    settings: RenderSettings,
    accent: str,
    with_icons: bool,
) -> dict[str, Any]:
    """Build the full template context for a resume.

    The contact block is snapshotted *before* escaping and read from the snapshot, because
    :func:`~app.documents.renderer.escape_document` escapes every string in the document —
    including the URLs, where a TeX escape would corrupt the link. Display text is escaped
    here instead, and URLs are percent-encoded by :func:`latex_url`.

    Args:
        doc: The resume, exactly as the resume engine produced it.
        settings: Resolved font size and margin.
        accent: Six-digit hex colour for headings and rules.
        with_icons: Whether the header shows Font Awesome glyphs.

    Returns:
        A mapping whose every string value is already LaTeX-safe.
    """
    raw_contact = doc.contact.model_copy(deep=True)
    escaped = escape_document(doc)
    headline = doc.meta.get("headline")

    return {
        "font_size": round(settings.font_size, 2),
        "margin_in": round(settings.margin_in, 3),
        "accent": accent,
        "pdf_title": _pdf_string(f"{raw_contact.name} — Resume".strip(" —")),
        "pdf_author": _pdf_string(raw_contact.name),
        "name": escape_latex(raw_contact.name),
        "headline": escape_latex(headline) if isinstance(headline, str) else "",
        "contact_items": _contact_context(contact_items(raw_contact), with_icons=with_icons),
        "summary": escaped.summary,
        "summary_heading": escape_latex(
            generated_heading(doc, SUMMARY_HEADING_META_KEY, DEFAULT_SUMMARY_HEADING)
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
                        "bullets": [bullet for bullet in entry.bullets if bullet.strip()],
                    }
                    for entry in section.entries
                ],
            }
            for section in escaped.sections
        ],
        "skills_line": escaped.skills_line,
        "skills_heading": escape_latex(
            generated_heading(doc, SKILLS_HEADING_META_KEY, DEFAULT_SKILLS_HEADING)
        ),
    }


def _cover_letter_context(
    letter: CoverLetterDocument,
    *,
    settings: RenderSettings,
    accent: str,
    closing: str,
) -> dict[str, Any]:
    """Build the template context for a cover letter.

    Every string is escaped here with :func:`~app.documents.renderer.escape_latex` rather
    than by ``escape_document``, which is typed for resumes; the letter has few enough fields
    that escaping them explicitly is clearer than a generic walk, and it keeps the sender's
    URLs out of the escaper for the same reason as in :func:`_resume_context`.

    Args:
        letter: The letter, unescaped.
        settings: Resolved font size and margin.
        accent: Six-digit hex colour for the letterhead.
        closing: Sign-off line.

    Returns:
        A mapping whose every string value is already LaTeX-safe.
    """
    contact = letter.contact
    return {
        "font_size": round(settings.font_size, 2),
        "margin_in": round(settings.margin_in, 3),
        "accent": accent,
        "pdf_title": _pdf_string(f"{contact.name} — Cover letter".strip(" —")),
        "pdf_author": _pdf_string(contact.name),
        "sender_name": escape_latex(contact.name),
        "sender_items": _contact_context(contact_items(contact), with_icons=False),
        "date": escape_latex(letter.date),
        "recipient_lines": [escape_latex(line) for line in recipient_lines(letter)],
        "salutation": escape_latex(letter.salutation()),
        "paragraphs": [escape_latex(paragraph) for paragraph in letter.paragraphs()],
        "closing": escape_latex(closing),
        "signature": escape_latex(contact.name),
    }


# ======================================================================================
# Plugins
# ======================================================================================


class _LatexTemplate(TemplatePlugin):
    """Shared behaviour of the LaTeX templates.

    Abstract on purpose and never registered: the plugin registry requires a class to declare
    its own ``meta``, so an intermediate class without one cannot be mistaken for a plugin.
    Subclasses supply three class attributes and nothing else.

    Attributes:
        resume_template: File name of the ``.tex.j2`` resume template.
        accent_hex: Six-digit hex colour used for headings, rules and the letterhead.
        with_icons: Whether the contact line carries Font Awesome glyphs.
    """

    formats: ClassVar[frozenset[str]] = LATEX_FORMATS

    resume_template: ClassVar[str]
    accent_hex: ClassVar[str]
    with_icons: ClassVar[bool] = False

    #: Shared by both templates — a letter differs only in its accent colour.
    cover_letter_template: ClassVar[str] = "cover_letter.tex.j2"

    def build_resume_source(
        self,
        doc: ResumeDocument,
        *,
        settings: RenderSettings,
    ) -> str:
        """Render the resume template to LaTeX source without compiling it.

        Args:
            doc: The resume to typeset.
            settings: Resolved font size and margin.

        Returns:
            A complete LaTeX document.

        Raises:
            DocumentRenderError: If Jinja2 is missing, the template file is absent, or the
                template references a variable the context does not provide — the environment
                uses ``StrictUndefined`` so that a typo fails loudly here instead of silently
                printing an empty section.
        """
        context = _resume_context(
            doc,
            settings=settings,
            accent=self.accent_hex,
            with_icons=self.with_icons,
        )
        return self._render_template(self.resume_template, context)

    def build_cover_letter_source(
        self,
        letter: CoverLetterDocument,
        *,
        settings: RenderSettings,
        closing: str,
    ) -> str:
        """Render the cover-letter template to LaTeX source without compiling it.

        Args:
            letter: The letter to typeset.
            settings: Resolved font size and margin.
            closing: Sign-off line.

        Returns:
            A complete LaTeX document.

        Raises:
            DocumentRenderError: On the same conditions as :meth:`build_resume_source`.
        """
        context = _cover_letter_context(
            letter,
            settings=settings,
            accent=self.accent_hex,
            closing=closing,
        )
        return self._render_template(self.cover_letter_template, context)

    def _render_template(self, name: str, context: dict[str, Any]) -> str:
        """Render one template file with *context*.

        Args:
            name: File name inside :data:`TEMPLATE_DIR`.
            context: Fully escaped template variables.

        Returns:
            The rendered source.

        Raises:
            DocumentRenderError: If the template is missing or fails to render.
        """
        from jinja2 import TemplateError

        environment = latex_environment()
        try:
            return environment.get_template(name).render(**context)
        except TemplateError as exc:
            raise DocumentRenderError(
                f"template {name!r} failed to render: {type(exc).__name__}: {exc}"
            ) from exc

    async def render(
        self,
        doc: ResumeDocument,
        out: Path,
        *,
        fmt: str = PDF_FORMAT,
        options: dict[str, Any] | None = None,
    ) -> RenderResult:
        """Typeset *doc* into *out*.

        Args:
            doc: The resume to render, unescaped — escaping happens here.
            out: Destination path. Parent directories are created.
            fmt: ``"pdf"`` to compile, ``"tex"`` to write the generated source instead.
            options: ``font_size`` (points) and ``margin_in`` (inches) from
                ``render_resume``'s shrink loop; ``latex_binary`` and ``timeout`` override
                the engine and its budget.

        Returns:
            The written file, its size, and its page count — measured from the PDF, or
            estimated from the document model when only the source was written.

        Raises:
            DocumentRenderError: If *fmt* is unsupported, the template fails, no LaTeX engine
                is available, or the compile fails.
        """
        require_format(self, fmt)
        out = self.resolve_output(out, fmt)
        values = self.merge_options(options)
        settings = resolve_render_settings(values)
        source = self.build_resume_source(doc, settings=settings)

        if fmt == TEX_FORMAT:
            written = write_text(out, source)
            pages = estimated_page_count(doc, settings)
            self.logger.info("latex.source_written", path=str(out), bytes=written)
            return RenderResult.from_path(
                out, engine=TEX_SOURCE_ENGINE, template=self.name, page_count=pages
            )

        engine = await compile_latex(
            source,
            out,
            binary=self._binary_option(values),
            timeout=float(values.get("timeout") or LATEX_TIMEOUT_SECONDS),
            template=self.name,
        )
        result = RenderResult.from_path(out, engine=engine, template=self.name)
        self.logger.info(
            "latex.rendered",
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
        """Typeset *letter* into *out*.

        Args:
            letter: The cover letter to render, unescaped.
            out: Destination path. Parent directories are created.
            fmt: ``"pdf"`` to compile, ``"tex"`` to write the generated source.
            options: ``font_size``, ``margin_in``, ``closing``, ``latex_binary``, ``timeout``.

        Returns:
            The written file, its size, and its page count.

        Raises:
            DocumentRenderError: On the same conditions as :meth:`render`.
        """
        require_format(self, fmt)
        out = self.resolve_output(out, fmt)
        values = self.merge_options(options)
        settings = resolve_render_settings(values)
        source = self.build_cover_letter_source(
            letter,
            settings=settings,
            closing=str(values.get("closing") or DEFAULT_CLOSING),
        )

        if fmt == TEX_FORMAT:
            write_text(out, source)
            return RenderResult.from_path(
                out, engine=TEX_SOURCE_ENGINE, template=self.name, page_count=1
            )

        engine = await compile_latex(
            source,
            out,
            binary=self._binary_option(values),
            timeout=float(values.get("timeout") or LATEX_TIMEOUT_SECONDS),
            template=self.name,
        )
        result = RenderResult.from_path(out, engine=engine, template=self.name)
        self.logger.info(
            "latex.cover_letter_rendered",
            path=str(out),
            engine=engine,
            pages=result.page_count,
        )
        return result

    def _binary_option(self, values: dict[str, Any]) -> str | None:
        """Return the LaTeX binary to try first.

        Args:
            values: The caller's ``options`` mapping.

        Returns:
            ``options["latex_binary"]`` when given, otherwise ``settings.latex_binary``.
        """
        override = values.get("latex_binary")
        if isinstance(override, str) and override.strip():
            return override.strip()
        return getattr(self.settings, "latex_binary", None)

    async def healthcheck(self) -> bool:
        """Report whether this template can actually produce a PDF right now.

        Returns:
            ``True`` when Jinja2 imports, both template files exist, and a LaTeX engine is on
            ``PATH``. ``GET /ready`` aggregates this, so a machine with no TeX shows the
            ``modern`` template as unhealthy rather than failing at submission time.
        """
        for name in (self.resume_template, self.cover_letter_template):
            if not (TEMPLATE_DIR / name).is_file():
                self.logger.warning("latex.template_missing", template=name)
                return False
        try:
            latex_environment()
            resolve_latex_binary(getattr(self.settings, "latex_binary", None))
        except DocumentRenderError as exc:
            self.logger.warning("latex.unhealthy", error=str(exc))
            return False
        return True


@plugin
class ModernTemplate(_LatexTemplate):
    """The ``modern`` template — centred header, accent headings, optional icons.

    The default (``settings.resume_template == "modern"``). Single column, no tables, no
    images, no headers or footers: everything an ATS needs, with enough typography that a
    human wants to keep reading.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.TEMPLATE,
        name="modern",
        version="1.0.0",
        display_name="Modern (LaTeX)",
        description="Centred header with a restrained accent colour. ATS-safe single column.",
        capabilities=frozenset({"resume", "cover_letter", "ats_safe", "latex"}),
    )

    resume_template: ClassVar[str] = "resume_modern.tex.j2"
    accent_hex: ClassVar[str] = MODERN_ACCENT_HEX
    with_icons: ClassVar[bool] = True


@plugin
class ClassicTemplate(_LatexTemplate):
    """The ``classic`` template — pure black, left-aligned, uppercase headings, no icons.

    Identical structure to ``modern`` and identical ATS properties; it simply removes every
    decorative choice. Reach for it when the employer is conservative, when the posting asks
    for a "plain" resume, or when a particular ATS is known to mangle coloured PDFs.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.TEMPLATE,
        name="classic",
        version="1.0.0",
        display_name="Classic (LaTeX)",
        description="Pure black, left-aligned, uppercase headings. Maximum conservatism.",
        capabilities=frozenset({"resume", "cover_letter", "ats_safe", "latex"}),
    )

    resume_template: ClassVar[str] = "resume_classic.tex.j2"
    accent_hex: ClassVar[str] = CLASSIC_ACCENT_HEX
    with_icons: ClassVar[bool] = False
