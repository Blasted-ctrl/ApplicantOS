"""Automation runs (``docs/CONTRACTS.md`` §14).

A :class:`~app.models.session.RunSession` is a rollup: the applications carry the detail, the
session carries the counters that answer "how is this run going?" in a single row. That is
what lets the live dashboard paint a progress bar on every WebSocket tick without aggregating
over a growing table.

**Starting a run is idempotent.** :meth:`~app.services.session_service.SessionService.start`
returns the run already in flight rather than opening a second one, because two concurrent
runs would compete for the same daily cap, double-count every counter, and give the dashboard
two progress bars for one thing. From the caller's side "start a run" and "give me the current
run" are the same intent, so the endpoint answers both.

**A start request can only ever narrow what a run may do.** ``auto_apply`` and ``dry_run`` on
:class:`~app.schemas.session.SessionStartRequest` are one-directional by design:
``settings.is_submission_allowed`` is evaluated independently inside
:meth:`app.services.pipeline.Pipeline.submit` and always wins, so no request can turn
submission *on*. The snapshot recorded on the run is
:meth:`~app.schemas.settings.SettingsRead.from_settings`, which is the allow-listed projection
that contains no credential — ``config_snapshot`` is a plain JSON column, and the structlog
redaction chain governs logs, not the database.

``POST /sessions/start`` returns 202 with the dispatch outcome because it queues work;
``POST /sessions/{id}/stop`` returns the closed session directly, because closing a run is a
state change this process makes itself and there is nothing to queue.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from fastapi import APIRouter, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    CurrentUser,
    DbSession,
    PaginationDep,
    SessionServiceDep,
    SettingsDep,
)
from app.api.events import EVENT_SESSION_FINISHED, EVENT_SESSION_STARTED, bus
from app.api.routes._support import require_owner
from app.api.tasks import TASK_JOBS_POLL_ALL, TASK_SESSION_ADVANCE, dispatch
from app.models.application import Application
from app.models.enums import SessionStatus, StopReason
from app.models.session import RunSession
from app.schemas.application import ApplicationRead
from app.schemas.common import OkResponse, Page, paginate
from app.schemas.session import SessionDetail, SessionRead, SessionStartRequest
from app.schemas.settings import SettingsRead

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/sessions"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["sessions"]

#: Key in :attr:`~app.models.session.RunSession.config_snapshot` holding the safe settings
#: projection, and the key holding the user's preferences at start.
SNAPSHOT_SETTINGS_KEY: Final[str] = "settings"
SNAPSHOT_PREFERENCES_KEY: Final[str] = "preferences"
SNAPSHOT_REQUEST_KEY: Final[str] = "request"

router = APIRouter()


def _config_snapshot(
    settings: SettingsDep,
    user: CurrentUser,
    payload: SessionStartRequest,
) -> dict[str, Any]:
    """Build the reproducibility snapshot stored on the run.

    Every value comes from :meth:`~app.schemas.settings.SettingsRead.from_settings`, the
    allow-listed projection that names each field explicitly and therefore cannot leak a
    credential added to :class:`~app.config.settings.Settings` later. Building the snapshot
    from ``settings.model_dump()`` instead would put five API keys and two connection URLs
    into a JSON column on day one.

    Args:
        settings: Runtime configuration.
        user: The acting user, supplying the preference document.
        payload: The start request, recorded so a run's narrowing overrides are visible
            afterwards.

    Returns:
        A JSON-ready snapshot.
    """
    return {
        SNAPSHOT_SETTINGS_KEY: SettingsRead.from_settings(settings).model_dump(mode="json"),
        SNAPSHOT_PREFERENCES_KEY: user.prefs.model_dump(mode="json"),
        SNAPSHOT_REQUEST_KEY: payload.model_dump(mode="json"),
    }


@router.get(
    "",
    response_model=Page[SessionRead],
    summary="This user's runs, newest first",
)
async def list_sessions(
    user: CurrentUser,
    session: DbSession,
    page: PaginationDep,
) -> Page[SessionRead]:
    """Return the run history.

    Every rollup counter is included rather than a curated subset: the session screen renders
    all of them, and a client that has to derive ``failures`` from somewhere else will derive
    it differently from the server.

    Args:
        user: The acting user.
        session: The request's database session.
        page: Offset/limit pagination.

    Returns:
        One page of runs.
    """
    statement = select(RunSession).where(RunSession.user_id == user.id)
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))

    rows = await session.execute(
        statement.order_by(RunSession.started_at.desc(), RunSession.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    runs = [SessionRead.model_validate(run) for run in rows.scalars().all()]
    return paginate(runs, total=int(total or 0), params=page)


@router.post(
    "/start",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Begin an automation run",
)
async def start_session(
    payload: SessionStartRequest,
    user: CurrentUser,
    service: SessionServiceDep,
    settings: SettingsDep,
) -> OkResponse:
    """Open a run — or return the one already in flight — and queue its first pass.

    The run row is created synchronously so the dashboard has something to bind to
    immediately, and the work is queued as ``jobs.poll_all`` carrying the run's id, so the
    worker attributes its counters to this session rather than opening its own.

    A ``session.started`` event is published carrying the same
    :class:`~app.schemas.session.SessionRead` embedded in the response, so a second window
    updates from the event without refetching.

    Args:
        payload: Trigger, optional discovery query, and the narrowing overrides.
        user: The acting user.
        service: The run-session service.
        settings: Runtime configuration, snapshotted onto the run.

    Returns:
        202 with the session and the dispatch outcome. ``degraded`` is true when the broker
        is unreachable: the run exists and is visible, but nothing is driving it yet — which
        is exactly what the user needs to be told, and is not a server error.
    """
    run = await service.start(
        user.id,
        payload.trigger,
        config_snapshot=_config_snapshot(settings, user, payload),
        max_applications=payload.max_applications,
        match_threshold=payload.match_threshold,
    )
    item = SessionRead.model_validate(run)
    bus.publish_model(EVENT_SESSION_STARTED, item)

    outcome = await dispatch(
        TASK_JOBS_POLL_ALL,
        str(user.id),
        session_id=str(run.id),
        query=payload.discover.model_dump(mode="json") if payload.discover else None,
    )

    # Kick the run loop rather than waiting up to `ADVANCE_INTERVAL_SECONDS` for beat. The
    # loop is what applies to what discovery finds, so without this a run that already has
    # scored postings waiting would sit idle for a minute before doing anything at all.
    await dispatch(TASK_SESSION_ADVANCE, str(run.id))

    logger.info(
        "api.session_started",
        user_id=str(user.id),
        session_id=str(run.id),
        trigger=payload.trigger,
        discover=payload.discover is not None,
        submission_allowed=settings.is_submission_allowed,
        degraded=outcome.degraded,
    )
    return OkResponse(
        message=(
            "Run started and queued."
            if outcome.dispatched
            else "Run started, but the background worker is not running, so nothing is "
            "driving it yet."
        ),
        data={
            **outcome.as_dict(),
            "session": item.model_dump(mode="json"),
            "submission_allowed": settings.is_submission_allowed,
        },
    )


@router.get(
    "/{session_id}",
    response_model=SessionDetail,
    summary="One run, with the applications it produced",
)
async def read_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    service: SessionServiceDep,
) -> SessionDetail:
    """Return one run with its applications and derived aggregates.

    ``applications`` mirrors a **lazily loaded** relationship, so the run is validated
    through :class:`~app.schemas.session.SessionRead` — which has no such field — and the
    detail is composed from that. Validating the ORM row straight into
    :class:`~app.schemas.session.SessionDetail` would make pydantic read the relationship
    during validation and raise ``MissingGreenlet`` inside the async session, *before* any
    assignment this handler makes could replace it.

    Args:
        session_id: The run to read.
        user: The acting user.
        session: The request's database session.
        service: The run-session service.

    Returns:
        The run, its applications, its outstanding checkpoints and its derived stats.

    Raises:
        LookupError: If no run has that id — mapped to 404.
    """
    run = await service.get(session_id)
    require_owner(run.user_id, user.id, "session")

    stats = await service.stats(run.id)
    rows = await session.execute(
        select(Application)
        .options(selectinload(Application.posting), selectinload(Application.company))
        .where(Application.session_id == run.id)
        .order_by(Application.created_at.asc(), Application.id.asc())
    )

    return SessionDetail(
        **SessionRead.model_validate(run).model_dump(),
        applications=[
            ApplicationRead.model_validate(application)
            for application in rows.scalars().unique().all()
        ],
        checkpoints_pending=int(stats.get("checkpoints_pending", 0)),
        stats=stats,
    )


@router.post(
    "/{session_id}/stop",
    response_model=SessionRead,
    summary="Stop a run",
)
async def stop_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    service: SessionServiceDep,
) -> SessionRead:
    """Close a run as cancelled, and refuse every submission it had queued.

    ``cancelled``, not ``completed`` or ``failed``: a user stopping a run is neither a clean
    finish nor a fault, and folding it into either would distort the reliability figures the
    dashboard reports.

    **Two writes, in this order, and the order is the point.** The stop is *requested* first,
    which is the signal a task already on the ``apply`` queue polls at rung 2 of
    :meth:`~app.services.pipeline.Pipeline.submit`; only then is the run closed. Closing it
    first would leave a window in which the run reads as over while its queued applications
    have seen no stop signal at all — and by the time the user sees "stopped", several more
    would have been sent.

    In-flight work is still not *killed*: a browser already filling a form finishes, because
    aborting mid-flow is what strands an application in ``submitting`` with an unknown
    outcome at the employer's end. What stops is everything that had not yet started.

    Idempotent — re-stopping preserves the original ``ended_at`` and the original stop
    reason, so a stop arriving after the watchdog reaped the run cannot relabel it.

    Args:
        session_id: The run to stop.
        user: The acting user.
        service: The run-session service.

    Returns:
        The closed run, carrying ``stop_reason`` and the sentence the UI renders.

    Raises:
        LookupError: If no run has that id — mapped to 404.
    """
    run = await service.get(session_id)
    require_owner(run.user_id, user.id, "session")

    await service.request_stop(run.id, StopReason.USER_STOPPED)
    closed = await service.finish(run.id, SessionStatus.CANCELLED, StopReason.USER_STOPPED)
    item = SessionRead.model_validate(closed)
    bus.publish_model(EVENT_SESSION_FINISHED, item)

    logger.info(
        "api.session_stopped",
        user_id=str(user.id),
        session_id=str(session_id),
        stop_reason=closed.stop_reason.value if closed.stop_reason is not None else None,
        applications_completed=closed.applications_completed,
    )
    return item
