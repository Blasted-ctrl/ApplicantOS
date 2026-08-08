"""Application services — the layer between the API and the engines.

``docs/CONTRACTS.md`` §13. A service owns one workflow end to end: it holds a session,
orchestrates the modules under :mod:`app.knowledge`, :mod:`app.jobs`, :mod:`app.ai` and
:mod:`app.browser`, and returns pydantic schemas. Route handlers parse, delegate and
serialise; workers call the same methods through ``run_async``. Nothing above this layer
touches an ORM row, and nothing below it knows HTTP exists.

Error handling is uniform across every service, which is what lets ``app.api.errors`` map
exceptions centrally instead of each route inventing its own:

* :class:`LookupError` — the id does not exist → ``404``
* :class:`ValueError` — the input is malformed or unsupported → ``400``

Import from the package rather than the module::

    from app.services import KnowledgeService

Note:
    The re-exports below are individually guarded. Services are built in parallel against
    the contract, so a tree missing one of them must still import — otherwise a half-written
    ``discovery_service`` would take the whole API down instead of one route group. A name
    that failed to import is absent from the module and absent from :data:`__all__`, so
    ``from app.services import DiscoveryService`` raises a plain :exc:`ImportError` naming
    exactly what is missing, and ``hasattr(app.services, "DiscoveryService")`` is a reliable
    feature check.
"""

from __future__ import annotations

import structlog

from app.services.knowledge_service import KnowledgeService

__all__ = ["KnowledgeService"]

logger = structlog.get_logger(__name__)

# The redundant ``X as X`` form is the explicit-re-export spelling: it tells linters and
# type checkers that the name is part of this package's public surface, which a plain
# ``import X`` inside a ``try`` cannot express.
try:
    from app.services.dedupe_service import DedupeService as DedupeService
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="DedupeService", error=str(exc)
    )
else:
    __all__.append("DedupeService")

try:
    from app.services.discovery_service import DiscoveryService as DiscoveryService
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="DiscoveryService", error=str(exc)
    )
else:
    __all__.append("DiscoveryService")

__all__.sort()
