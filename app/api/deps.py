"""FastAPI dependencies — the session, the settings, the current user, and the services.

Everything a route needs is resolved here so that handlers declare intent
(``user: CurrentUser``) rather than plumbing. Four groups:

**Infrastructure.** :data:`DbSession` yields a request-scoped
:class:`~sqlalchemy.ext.asyncio.AsyncSession` that never auto-commits — the service layer
owns transaction boundaries — and :data:`SettingsDep` yields the process-wide settings.

**Identity.** ApplicantOS is single-tenant by design: it is a desktop application managing
one person's job hunt, and there is no login. :func:`get_current_user` therefore resolves
the *only* user, with an ``X-User-Id`` header as the override for the multi-profile case and
for tests. When the database holds no user at all the answer is a 404 that says how to fix
it, not a 500 and not an empty dashboard — a fresh install with no seeded user is a
first-run state, not a server fault.

**Pagination.** :func:`pagination_params` is the dependency behind every list endpoint;
``limit`` is bounded by :data:`~app.schemas.common.MAX_PAGE_LIMIT` at the boundary so no
caller can ask for a full scan of ``job_postings`` or ``log_entries``.

**Services.** One provider per service in ``docs/CONTRACTS.md`` §13, each constructed over
the request's session. They are ordinary functions rather than a container: FastAPI already
*is* the container, and a second one would only hide the dependency graph.

Every service provider is imported from its own module rather than from
:mod:`app.services`, whose re-exports are individually guarded — a guarded import means a
missing service surfaces as a missing *attribute* at request time instead of a clear
``ImportError`` at startup.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.database.session import get_session
from app.models.user import User
from app.schemas.common import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    MIN_PAGE_LIMIT,
    PaginationParams,
)
from app.services.analytics_service import AnalyticsService
from app.services.application_service import ApplicationService
from app.services.checkpoint_service import CheckpointService
from app.services.dedupe_service import DedupeService
from app.services.discovery_service import DiscoveryService
from app.services.knowledge_service import KnowledgeService
from app.services.onboarding_service import OnboardingService
from app.services.pipeline import Pipeline
from app.services.review_service import ReviewService
from app.services.session_service import SessionService

__all__ = [
    "AnalyticsServiceDep",
    "ApplicationServiceDep",
    "CheckpointServiceDep",
    "CurrentUser",
    "DbSession",
    "DedupeServiceDep",
    "DiscoveryServiceDep",
    "KnowledgeServiceDep",
    "NO_USER_DETAIL",
    "OnboardingServiceDep",
    "PaginationDep",
    "PipelineDep",
    "ReviewServiceDep",
    "SessionServiceDep",
    "SettingsDep",
    "USER_ID_HEADER",
    "get_current_user",
    "pagination_params",
]

logger = structlog.get_logger(__name__)

#: Header a client uses to name which profile it is acting as. Absent in the normal
#: single-user desktop install.
USER_ID_HEADER: str = "X-User-Id"

#: Detail returned when the database holds no user. Actionable on purpose: a bare
#: "not found" here reads as a bug, when in fact it is the documented first-run state.
NO_USER_DETAIL: str = (
    "No user account exists yet. Seed one with `python -m scripts.seed`, or complete the "
    "onboarding wizard, which creates the account on its first step."
)

#: Detail returned when ``X-User-Id`` names a user that does not exist.
_UNKNOWN_USER_DETAIL: str = "No user exists with the id supplied in the {header} header."

#: Detail returned when ``X-User-Id`` is not a UUID.
_MALFORMED_USER_DETAIL: str = "The {header} header must be a UUID."


# ======================================================================================
# Infrastructure
# ======================================================================================

#: A request-scoped database session. Rolls back if the handler raises; never commits.
DbSession = Annotated[AsyncSession, Depends(get_session)]

#: The process-wide settings singleton.
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ======================================================================================
# Identity
# ======================================================================================


async def get_current_user(
    session: DbSession,
    x_user_id: Annotated[
        str | None,
        Header(
            alias=USER_ID_HEADER,
            description="Act as this user. Omit in the single-user desktop install.",
        ),
    ] = None,
) -> User:
    """Resolve the user this request acts as.

    Single-tenant resolution, in order:

    1. ``X-User-Id`` when supplied — the multi-profile and test path.
    2. Otherwise the oldest active user, which in a desktop install is the only one.

    Args:
        session: The request's database session.
        x_user_id: Optional user id from the request header.

    Returns:
        The resolved, active user.

    Raises:
        HTTPException: ``400`` when the header is not a UUID, ``404`` when it names nobody,
            and ``404`` with an actionable message when no user has been seeded at all.
    """
    if x_user_id is not None and x_user_id.strip():
        try:
            identifier = uuid.UUID(x_user_id.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_MALFORMED_USER_DETAIL.format(header=USER_ID_HEADER),
            ) from exc
        found = await session.get(User, identifier)
        if found is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_UNKNOWN_USER_DETAIL.format(header=USER_ID_HEADER),
            )
        return found

    statement = (
        select(User)
        .where(User.is_active.is_(True))
        .order_by(User.created_at.asc(), User.id.asc())
        .limit(1)
    )
    user = (await session.execute(statement)).scalars().first()
    if user is None:
        logger.info("api.no_user_seeded")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_USER_DETAIL)
    return user


#: The user this request acts as.
CurrentUser = Annotated[User, Depends(get_current_user)]


# ======================================================================================
# Pagination
# ======================================================================================


def pagination_params(
    limit: Annotated[
        int,
        Query(
            ge=MIN_PAGE_LIMIT,
            le=MAX_PAGE_LIMIT,
            description="Maximum number of items to return.",
        ),
    ] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of items to skip before this page begins."),
    ] = 0,
) -> PaginationParams:
    """Build the pagination parameters for a list endpoint.

    Args:
        limit: Page size, bounded by :data:`~app.schemas.common.MAX_PAGE_LIMIT`.
        offset: Rows to skip.

    Returns:
        The validated parameters.
    """
    return PaginationParams(limit=limit, offset=offset)


#: Offset/limit pagination for a list endpoint.
PaginationDep = Annotated[PaginationParams, Depends(pagination_params)]


# ======================================================================================
# Services
# ======================================================================================


def get_application_service(session: DbSession) -> ApplicationService:
    """Provide the application state machine.

    Args:
        session: The request's database session.

    Returns:
        A service bound to that session.
    """
    return ApplicationService(session)


def get_review_service(session: DbSession) -> ReviewService:
    """Provide the human review queue service.

    Args:
        session: The request's database session.

    Returns:
        A service bound to that session.
    """
    return ReviewService(session)


def get_knowledge_service(session: DbSession, settings: SettingsDep) -> KnowledgeService:
    """Provide the knowledge-engine facade.

    Args:
        session: The request's database session.
        settings: Runtime configuration.

    Returns:
        A service bound to that session.
    """
    return KnowledgeService(session, settings)


def get_onboarding_service(session: DbSession, settings: SettingsDep) -> OnboardingService:
    """Provide the onboarding wizard service.

    Args:
        session: The request's database session.
        settings: Runtime configuration.

    Returns:
        A service bound to that session.
    """
    return OnboardingService(session, settings)


def get_session_service(session: DbSession) -> SessionService:
    """Provide the run-session service.

    Args:
        session: The request's database session.

    Returns:
        A service bound to that session.
    """
    return SessionService(session)


def get_analytics_service(session: DbSession) -> AnalyticsService:
    """Provide the analytics aggregator.

    Args:
        session: The request's database session.

    Returns:
        A service bound to that session.
    """
    return AnalyticsService(session)


def get_checkpoint_service(session: DbSession) -> CheckpointService:
    """Provide the resumable-checkpoint service.

    Args:
        session: The request's database session.

    Returns:
        A service bound to that session.
    """
    return CheckpointService(session)


def get_dedupe_service(session: DbSession) -> DedupeService:
    """Provide the posting deduplication service.

    Args:
        session: The request's database session.

    Returns:
        A service bound to that session.
    """
    return DedupeService(session)


def get_discovery_service(session: DbSession, settings: SettingsDep) -> DiscoveryService:
    """Provide the discovery service.

    Args:
        session: The request's database session.
        settings: Runtime configuration.

    Returns:
        A service bound to that session.
    """
    return DiscoveryService(session, settings)


def get_pipeline(session: DbSession, settings: SettingsDep) -> Pipeline:
    """Provide the end-to-end pipeline.

    Note:
        A route should almost never call :meth:`~app.services.pipeline.Pipeline.submit`
        directly — submission is long-running browser work and belongs on the ``apply``
        queue. This exists for the read-only and preparation paths.

    Args:
        session: The request's database session.
        settings: Runtime configuration.

    Returns:
        A pipeline bound to that session.
    """
    return Pipeline(session, settings)


#: The application state machine.
ApplicationServiceDep = Annotated[ApplicationService, Depends(get_application_service)]

#: The human review queue.
ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]

#: The knowledge engine facade.
KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]

#: The onboarding wizard.
OnboardingServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]

#: Run sessions.
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]

#: Analytics aggregation.
AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]

#: Resumable checkpoints.
CheckpointServiceDep = Annotated[CheckpointService, Depends(get_checkpoint_service)]

#: Posting deduplication.
DedupeServiceDep = Annotated[DedupeService, Depends(get_dedupe_service)]

#: Provider discovery.
DiscoveryServiceDep = Annotated[DiscoveryService, Depends(get_discovery_service)]

#: The end-to-end pipeline.
PipelineDep = Annotated[Pipeline, Depends(get_pipeline)]
