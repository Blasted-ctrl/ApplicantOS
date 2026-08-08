"""Document generation — the render model, the one-page loop, and the built-in templates.

This is the package :data:`app.plugins.loader.BUILTIN_PLUGIN_MODULES` imports to register
the resume templates (``docs/CONTRACTS.md`` §6, §11). Registration is a side effect of
import: each concrete template module is decorated with :func:`app.plugins.plugin`, whose
body runs when the module is executed, so the imports below are the reason
``registry.get(PluginKind.TEMPLATE, "modern")`` resolves to anything.

Every one of those imports is guarded. The template modules carry the optional dependencies
— a LaTeX binary, ``python-docx``, an HTML-to-PDF engine — and a tree that is missing one
of them must still produce the others rather than taking down every route that touches a
resume. A module that is simply absent is logged at debug; one that is present but fails to
import is logged at warning, because that is a defect somebody has to see.

The public surface::

    from app.documents import ResumeDocument, render_resume

    doc = ResumeDocument(contact=..., sections=[...])
    result = await render_resume(doc, Path("var/out/resume.pdf"), max_pages=1)

Golden rule #5 still applies inside this package: nothing outside it imports
:mod:`app.documents.latex` or its siblings. Callers ask :func:`get_template` — or just call
:func:`render_resume` and let it resolve the template from settings.
"""

from __future__ import annotations

import importlib
from typing import Final

import structlog

from app.documents.models import (
    BASE_CHARS_PER_LINE,
    BASE_FONT_SIZE_PT,
    BASE_LINES_PER_PAGE,
    BASE_MARGIN_IN,
    MIN_BULLETS_PER_SECTION,
    PRIORITIES_META_KEY,
    Contact,
    CoverLetterDocument,
    ResumeDocument,
    ResumeEntry,
    ResumeSection,
    chars_per_line,
    lines_per_page,
    wrapped_lines,
)
from app.documents.renderer import (
    COVER_LETTER_META_KEY,
    DEFAULT_FONT_SIZE_PT,
    DEFAULT_MARGIN_IN,
    DEFAULT_TEMPLATE,
    DOCUMENT_KIND_META_KEY,
    ESCAPED_META_KEY,
    LATEX_ESCAPE_ORDER,
    MIN_BULLET_DROP,
    MIN_FONT_SIZE_PT,
    OPTION_ATTEMPT,
    OPTION_FONT_SIZE,
    OPTION_MARGIN_IN,
    OPTION_MAX_PAGES,
    OPTION_STEP,
    SHRINK_LADDER,
    DocumentRenderError,
    RenderResult,
    ShrinkStep,
    TemplatePlugin,
    cover_letter_to_resume_document,
    escape_document,
    escape_latex,
    escape_latex_dict,
    get_template,
    is_escaped,
    page_count,
    render_cover_letter,
    render_resume,
    with_page_count,
)

__all__ = [
    "BASE_CHARS_PER_LINE",
    "BASE_FONT_SIZE_PT",
    "BASE_LINES_PER_PAGE",
    "BASE_MARGIN_IN",
    "COVER_LETTER_META_KEY",
    "DEFAULT_FONT_SIZE_PT",
    "DEFAULT_MARGIN_IN",
    "DEFAULT_TEMPLATE",
    "DOCUMENT_KIND_META_KEY",
    "ESCAPED_META_KEY",
    "LATEX_ESCAPE_ORDER",
    "MIN_BULLETS_PER_SECTION",
    "MIN_BULLET_DROP",
    "MIN_FONT_SIZE_PT",
    "OPTION_ATTEMPT",
    "OPTION_FONT_SIZE",
    "OPTION_MARGIN_IN",
    "OPTION_MAX_PAGES",
    "OPTION_STEP",
    "PRIORITIES_META_KEY",
    "SHRINK_LADDER",
    "TEMPLATE_MODULES",
    "Contact",
    "CoverLetterDocument",
    "DocumentRenderError",
    "RenderResult",
    "ResumeDocument",
    "ResumeEntry",
    "ResumeSection",
    "ShrinkStep",
    "TemplatePlugin",
    "chars_per_line",
    "cover_letter_to_resume_document",
    "escape_document",
    "escape_latex",
    "escape_latex_dict",
    "get_template",
    "is_escaped",
    "lines_per_page",
    "page_count",
    "render_cover_letter",
    "render_resume",
    "with_page_count",
    "wrapped_lines",
]

logger = structlog.get_logger(__name__)

#: Modules whose import registers the built-in templates, in the order the contract lists
#: them: ``latex`` (``modern`` and ``classic``), ``docx`` (``ats_plain``), ``html``
#: (``web``) and ``markdown`` (``markdown``). Named here rather than imported statically so
#: that a missing optional renderer is a log line instead of an ``ImportError`` at startup.
TEMPLATE_MODULES: Final[tuple[str, ...]] = ("latex", "docx", "html", "markdown")


def _import_template_module(name: str) -> bool:
    """Import one template module for its plugin-registration side effect.

    Args:
        name: Module name relative to this package.

    Returns:
        ``True`` when the module was imported and its templates registered.
    """
    qualified = f"{__name__}.{name}"
    try:
        importlib.import_module(qualified)
    except ImportError as exc:
        missing = getattr(exc, "name", None)
        if missing == qualified or (missing or "").startswith(f"{qualified}."):
            logger.debug("documents.template_module_absent", module=qualified)
        else:
            logger.warning(
                "documents.template_module_dependency_missing",
                module=qualified,
                missing=missing,
                error=str(exc),
            )
        return False
    except Exception as exc:
        logger.warning(
            "documents.template_module_import_failed",
            module=qualified,
            error=str(exc),
        )
        return False
    return True


for _module_name in TEMPLATE_MODULES:
    _import_template_module(_module_name)
