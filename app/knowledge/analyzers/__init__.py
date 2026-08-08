"""Knowledge source analyzers — importing this package registers every one of them.

``docs/CONTRACTS.md`` §6 makes analyzers plugins, and
:func:`app.plugins.loader.load_all` discovers them by importing exactly one thing: this
package. Every concrete analyzer module is therefore imported below, purely so that its
``@plugin`` decorator runs. Nothing else in ApplicantOS may import a concrete analyzer
(golden rule #5) — callers ask :func:`analyzer_for` or :func:`get_analyzer` and receive
whichever plugin claims the source.

Two of the imports are guarded. ``github`` and ``website`` reach the network and are built
independently of the four local analyzers, so a tree in which one of them is absent still
yields a working knowledge engine over project folders, resumes, LinkedIn exports and
documents. An import failure that is *not* a missing module — a syntax error, a broken
dependency — is logged at warning level rather than swallowed, because that is a defect
someone has to see.

The base types are re-exported so the rest of the system can write
``from app.knowledge.analyzers import AnalysisResult, SourceRef`` without reaching into a
private module path.
"""

from __future__ import annotations

import structlog

from app.knowledge.analyzers.base import (
    AnalysisResult,
    Analyzer,
    AnalyzerError,
    ExtractedDocument,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedFact,
    SourceAccessDenied,
    SourceRef,
    SourceUnavailableError,
    analyzer_for,
    chunk_text,
    close_http_client,
    compute_fingerprint,
    estimate_tokens,
    get_analyzer,
    http_client,
)

# Imported for their registration side effect. Each module's `@plugin` decorator runs at
# import time; the names themselves are deliberately not re-exported.
from app.knowledge.analyzers import document as _document  # noqa: F401
from app.knowledge.analyzers import linkedin_export as _linkedin_export  # noqa: F401
from app.knowledge.analyzers import project_folder as _project_folder  # noqa: F401
from app.knowledge.analyzers import resume_parser as _resume_parser  # noqa: F401

__all__ = [
    "AnalysisResult",
    "Analyzer",
    "AnalyzerError",
    "ExtractedDocument",
    "ExtractedEdge",
    "ExtractedEntity",
    "ExtractedFact",
    "SourceAccessDenied",
    "SourceRef",
    "SourceUnavailableError",
    "analyzer_for",
    "chunk_text",
    "close_http_client",
    "compute_fingerprint",
    "estimate_tokens",
    "get_analyzer",
    "http_client",
]

logger = structlog.get_logger(__name__)

#: Analyzer modules whose absence is tolerated, because they are network-backed and are
#: built independently of the local four.
_OPTIONAL_ANALYZER_MODULES: tuple[str, ...] = ("github", "website")


def _import_optional(name: str) -> None:
    """Import one optional analyzer module for its registration side effect.

    Args:
        name: Module name relative to this package.
    """
    import importlib

    qualified = f"{__name__}.{name}"
    try:
        importlib.import_module(qualified)
    except ImportError as exc:
        missing = getattr(exc, "name", None)
        if missing == qualified or (missing or "").startswith(f"{qualified}."):
            logger.debug("analyzers.optional_absent", module=qualified)
        else:
            logger.warning(
                "analyzers.optional_import_failed",
                module=qualified,
                missing=missing,
                error=str(exc),
            )
    except Exception as exc:  # noqa: BLE001 - one broken analyzer must not break the rest
        logger.warning("analyzers.optional_import_failed", module=qualified, error=str(exc))


for _optional in _OPTIONAL_ANALYZER_MODULES:
    _import_optional(_optional)
