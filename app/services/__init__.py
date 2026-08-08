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

# ``ApplicationService`` comes with the state machine and the error type that guards it.
# All three are re-exported together: a caller that catches ``InvalidTransition`` needs the
# name, and a caller rendering "what can I do next?" needs ``ALLOWED_TRANSITIONS``.
try:
    from app.services.application_service import (
        ALLOWED_TRANSITIONS as ALLOWED_TRANSITIONS,
    )
    from app.services.application_service import (
        ApplicationService as ApplicationService,
    )
    from app.services.application_service import InvalidTransition as InvalidTransition
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="ApplicationService", error=str(exc)
    )
else:
    __all__ += ["ALLOWED_TRANSITIONS", "ApplicationService", "InvalidTransition"]

try:
    from app.services.review_service import ReviewService as ReviewService
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="ReviewService", error=str(exc)
    )
else:
    __all__.append("ReviewService")

# ``CheckpointService`` comes with the step vocabulary and the two key builders. All four are
# re-exported together: a caller writing a checkpoint needs ``step_key``/``owner_key`` to
# build a key that is actually unique, and a caller rendering "5 of 7" needs
# ``STEPS_BY_OWNER`` for the total.
try:
    from app.services.checkpoint_service import (
        STEPS_BY_OWNER as STEPS_BY_OWNER,
    )
    from app.services.checkpoint_service import CheckpointService as CheckpointService
    from app.services.checkpoint_service import owner_key as owner_key
    from app.services.checkpoint_service import step_key as step_key
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="CheckpointService", error=str(exc)
    )
else:
    __all__ += ["STEPS_BY_OWNER", "CheckpointService", "owner_key", "step_key"]

try:
    from app.services.session_service import SessionService as SessionService
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="SessionService", error=str(exc)
    )
else:
    __all__.append("SessionService")

# ``STEPS`` travels with ``OnboardingService`` because the wizard definition is the service's
# public surface: the route that answers ``GET /onboarding/steps`` renders it directly.
try:
    from app.services.onboarding_service import (
        STEPS as ONBOARDING_STEPS,
    )
    from app.services.onboarding_service import OnboardingService as OnboardingService
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="OnboardingService", error=str(exc)
    )
else:
    __all__ += ["ONBOARDING_STEPS", "OnboardingService"]

try:
    from app.services.analytics_service import AnalyticsService as AnalyticsService
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug(
        "services.optional_import_failed", service="AnalyticsService", error=str(exc)
    )
else:
    __all__.append("AnalyticsService")

# ``Pipeline`` is imported last: it depends on every service above it, so a failure here is
# most usefully read as "one of my collaborators is missing" rather than as its own fault.
try:
    from app.services.pipeline import Pipeline as Pipeline
    from app.services.pipeline import PipelineResult as PipelineResult
except ImportError as exc:  # pragma: no cover - depends on build order
    logger.debug("services.optional_import_failed", service="Pipeline", error=str(exc))
else:
    __all__ += ["Pipeline", "PipelineResult"]

__all__.sort()
