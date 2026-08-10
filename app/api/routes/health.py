"""Liveness, readiness, and the Prometheus scrape (``docs/CONTRACTS.md`` §14).

These three paths sit outside ``/api/v1`` on purpose: a process supervisor, the Tauri
shell's startup poll, and a metrics scraper must not have to know the API's version.

The distinction between the first two is the whole point of having both:

``GET /health``
    **Liveness.** Answers "is this process running?" and touches nothing else — no
    database, no Redis, no plugins. It has to stay true while a dependency is down, because
    a supervisor that restarts the process on a database outage turns a recoverable outage
    into a crash loop. The Tauri sidecar polls this to decide when the backend is up
    (``docs/CONTRACTS.md`` §18), so it must answer before the schema exists.

``GET /ready``
    **Readiness.** Answers "can this process serve traffic?" and probes each dependency
    *separately*, returning 503 when a required one is down. Reporting them separately is
    what makes the answer actionable: "database unreachable" and "Redis unreachable" have
    completely different fixes, and a single boolean would hide which.

Redis is required only when it is actually in use — ``cache_backend == "redis"``. The
zero-infrastructure install (``SQLITE_MODE=true``) switches the cache to disk, so Redis is
reported as ``skipped`` and its absence cannot make a perfectly healthy desktop install look
unready.

``GET /metrics`` renders :data:`app.observability.metrics.registry`. It returns 404 rather
than an empty document when ``metrics_enabled`` is off, so a scraper's own error rate reports
the misconfiguration instead of silently graphing zeroes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import structlog
from fastapi import APIRouter, Response, status
from pydantic import Field

from app import __version__
from app.api.deps import SettingsDep
from app.config.settings import Settings
from app.database.session import check_database
from app.observability.metrics import render_metrics
from app.schemas.common import Schema

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from enum import Enum

__all__ = ["MOUNT_AT_ROOT", "PREFIX", "TAGS", "DependencyStatus", "ReadinessReport", "router"]

logger = structlog.get_logger(__name__)

#: These paths live at the root, not under ``/api/v1``.
MOUNT_AT_ROOT: Final[bool] = True

#: No prefix — the paths are absolute.
PREFIX: Final[str] = ""

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["health"]

#: Seconds to wait for a Redis ``PING`` before calling it unreachable. A readiness probe
#: that hangs is worse than one that fails: an orchestrator reads a timeout as "unknown".
REDIS_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0

#: Cache backend value that makes Redis a required dependency.
_REDIS_BACKEND: Final[str] = "redis"

# FastAPI declares ``tags`` as ``list[str | Enum]`` and only ever reads it, but ``list`` is
# invariant, so the ``list[str]`` every route module exports cannot be passed as-is. Cast at
# the one call site rather than widening the constant that thirteen sibling modules mirror.
router = APIRouter(tags=cast("list[str | Enum]", TAGS))


# ======================================================================================
# Response models
# ======================================================================================


class HealthReport(Schema):
    """Body of ``GET /health``.

    Attributes:
        status: Always ``"ok"``. The endpoint returns 200 or the process is not running.
        app: Configured application name.
        version: Package version, so the desktop shell can detect a sidecar mismatch.
        environment: ``local`` / ``dev`` / ``prod``.
    """

    status: str = Field(default="ok", description="Always 'ok' when the process answers.")
    app: str = Field(description="Configured application name.")
    version: str = Field(description="Backend package version.")
    environment: str = Field(description="Environment profile this process is running under.")


class DependencyStatus(Schema):
    """One probed dependency.

    Attributes:
        ok: Whether the probe succeeded. ``True`` for a skipped optional dependency —
            "not needed" is not a failure.
        required: Whether a failure here should make the process unready.
        skipped: Whether the probe was not run because the dependency is not in use.
        detail: Human-readable explanation, present on failure and on a skip.
    """

    ok: bool = Field(description="Whether this dependency answered.")
    required: bool = Field(default=True, description="Whether a failure makes us unready.")
    skipped: bool = Field(default=False, description="Whether the probe was not run.")
    detail: str | None = Field(default=None, description="Why it failed, or why it was skipped.")


class ReadinessReport(Schema):
    """Body of ``GET /ready``.

    Attributes:
        ready: Whether every *required* dependency answered.
        checks: One entry per dependency, so a failure names itself.
    """

    ready: bool = Field(description="Whether every required dependency answered.")
    checks: dict[str, DependencyStatus] = Field(
        default_factory=dict,
        description="Per-dependency probe results.",
    )


# ======================================================================================
# Probes
# ======================================================================================


async def _probe_redis(redis_url: str, *, required: bool) -> DependencyStatus:
    """Ping Redis, reporting rather than raising.

    The client import is lazy and its absence is a reportable state, not an error: ``redis``
    is an optional extra and the desktop install ships without it.

    Args:
        redis_url: The connection URL.
        required: Whether a failure should make the process unready.

    Returns:
        The probe result.
    """
    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        return DependencyStatus(
            ok=not required,
            required=required,
            skipped=True,
            detail="The redis client is not installed (pip install 'applicantos[redis]').",
        )

    client: Any = None
    try:
        client = redis_asyncio.from_url(
            redis_url,
            socket_timeout=REDIS_PROBE_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_PROBE_TIMEOUT_SECONDS,
        )
        await client.ping()
    except Exception as exc:
        logger.warning("health.redis_unreachable", error_type=type(exc).__name__)
        return DependencyStatus(
            ok=False,
            required=required,
            detail="Redis did not answer a PING within the probe timeout.",
        )
    else:
        return DependencyStatus(ok=True, required=required)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:
                logger.debug("health.redis_close_failed", error=str(exc))


# ======================================================================================
# Endpoints
# ======================================================================================


@router.get(
    "/health",
    response_model=HealthReport,
    summary="Liveness probe",
    description="Answers whether the process is running. Touches no dependency.",
)
async def health(settings: SettingsDep) -> HealthReport:
    """Report that the process is alive.

    Deliberately dependency-free: this must keep answering while the database is down, or a
    supervisor will restart the process during an outage it cannot fix by restarting.

    Args:
        settings: Runtime configuration.

    Returns:
        The liveness report.
    """
    return HealthReport(
        app=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )


@router.get(
    "/ready",
    response_model=ReadinessReport,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessReport}},
    summary="Readiness probe",
    description="Probes each dependency separately and returns 503 if a required one is down.",
)
async def ready(settings: SettingsDep, response: Response) -> ReadinessReport:
    """Probe every dependency and report each one separately.

    Args:
        settings: Runtime configuration, which decides whether Redis is required.
        response: The outgoing response, whose status is set to 503 on failure.

    Returns:
        The readiness report. The HTTP status is 503 when a required dependency failed —
        the body is returned either way, because "which one" is the useful part.
    """
    checks: dict[str, DependencyStatus] = {}

    database_ok = await check_database()
    checks["database"] = DependencyStatus(
        ok=database_ok,
        required=True,
        detail=None if database_ok else "The database did not answer a trivial query.",
    )

    redis_required = settings.cache_backend == _REDIS_BACKEND
    if redis_required:
        checks["redis"] = await _probe_redis(settings.redis_url, required=True)
    else:
        checks["redis"] = DependencyStatus(
            ok=True,
            required=False,
            skipped=True,
            detail=f"Not in use: cache_backend is {settings.cache_backend!r}.",
        )

    checks["task_execution"] = await _probe_task_execution(settings)

    is_ready = all(check.ok for check in checks.values() if check.required)
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessReport(ready=is_ready, checks=checks)


async def _probe_task_execution(settings: Settings) -> DependencyStatus:
    """Report whether anything is actually able to run background work.

    The check this endpoint most needed and did not have. ``ready: true`` used to mean "the
    database answered", which it did happily on an install where every queued task went to a
    broker no worker consumed — so the probe designed to catch a half-down system reported
    full health while nothing could run at all.

    Args:
        settings: Runtime configuration.

    Returns:
        Which executor will pick up the next task, and whether one exists.
    """
    from app.api.tasks import QUEUE_APPLY, QUEUE_DISCOVERY, worker_serves

    mode = settings.task_execution

    if mode == "inline":
        return DependencyStatus(
            ok=True,
            required=True,
            detail="Background work runs in this process (task_execution='inline').",
        )

    # The two queues that carry the work a user waits on. A pool serving only maintenance
    # is not a pool that will run a discovery poll.
    served = [queue for queue in (QUEUE_DISCOVERY, QUEUE_APPLY) if await worker_serves(queue)]

    if len(served) == 2:
        return DependencyStatus(
            ok=True, required=True, detail="A Celery worker is consuming the work queues."
        )

    if mode == "auto":
        missing = "no worker is" if not served else f"no worker serves {QUEUE_APPLY!r}, so it is"
        return DependencyStatus(
            ok=True,
            required=True,
            detail=f"Background work runs in this process: {missing} consuming the queues.",
        )

    return DependencyStatus(
        ok=False,
        required=True,
        detail=(
            "No Celery worker is consuming the work queues, and task_execution='worker' "
            "forbids running it here — queued work will never start. Start a worker, or set "
            "TASK_EXECUTION=auto."
        ),
    )


@router.get(
    "/metrics",
    summary="Prometheus scrape",
    description="Renders every collector in docs/CONTRACTS.md §16.",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {"text/plain": {}}},
        status.HTTP_404_NOT_FOUND: {"description": "Metrics collection is disabled."},
    },
)
async def metrics(settings: SettingsDep) -> Response:
    """Render the metrics registry for a Prometheus scrape.

    Args:
        settings: Runtime configuration, which can switch collection off.

    Returns:
        The exposition document, or a 404 when ``metrics_enabled`` is off. A 404 rather
        than an empty 200: a scraper's error rate should report the misconfiguration
        instead of silently graphing zeroes.
    """
    if not settings.metrics_enabled:
        return Response(
            content="metrics collection is disabled (METRICS_ENABLED=false)\n",
            media_type="text/plain; charset=utf-8",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
