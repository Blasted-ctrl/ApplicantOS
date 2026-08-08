"""Enqueueing background work **by name**, without importing :mod:`app.workers`.

Every endpoint that starts long-running work — a discovery poll, a submission, a reindex —
hands it to Celery rather than doing it inside the request. That much is ordinary. What is
deliberate is *how*: this module sends tasks by their string name (``"apply.submit"``) using
a bare :class:`celery.Celery` client, and never imports the task functions.

Two reasons, and both are structural rather than stylistic:

1. **The API and the workers are separately deployable.** Importing ``app.workers`` into the
   web process would pull the entire pipeline — Playwright, the document renderers, every
   provider — into a process that only needs to serve JSON, and would make a broken worker
   module a broken API.
2. **Celery is optional at runtime.** The desktop install runs with no broker at all. The
   import is lazy and the failure path is a first-class outcome, not an exception.

**A missing broker is a 202, never a 500.** ``docs/CONTRACTS.md`` §14 endpoints that trigger
work return ``202 Accepted``; when the broker is unreachable they still return 202, with
:attr:`Dispatch.degraded` set and a reason the desktop app can show ("queued work could not
be started — the background worker is not running"). The alternative — a 500 — tells the user
their request failed when in fact the *request* was fine and the *system* is partly down, and
it makes a perfectly usable read-only install look broken.

Task names and queue routing mirror ``docs/CONTRACTS.md`` §15 exactly. They are constants
here so that a typo is a failed import rather than a task that vanishes into a queue nobody
consumes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from typing import Any, Final

import structlog

from app.config.settings import get_settings

__all__ = [
    "BROKER_TIMEOUT_SECONDS",
    "DEGRADED_KEY",
    "QUEUE_AI",
    "QUEUE_APPLY",
    "QUEUE_DISCOVERY",
    "QUEUE_KNOWLEDGE",
    "QUEUE_MAINTENANCE",
    "TASK_APPLY_PREPARE",
    "TASK_APPLY_RUN_ONE",
    "TASK_APPLY_SUBMIT",
    "TASK_CLEANUP_TEMP_DOCUMENTS",
    "TASK_JOBS_POLL_ALL",
    "TASK_JOBS_POLL_PROVIDER",
    "TASK_JOBS_SCORE_POSTING",
    "TASK_KNOWLEDGE_INDEX_ALL",
    "TASK_KNOWLEDGE_INDEX_SOURCE",
    "TASK_KNOWLEDGE_REFRESH_STALE",
    "TASK_QUEUES",
    "TASK_SESSION_WATCHDOG",
    "TASK_SYNC_DETECT_GHOSTED",
    "TASK_SYNC_ON_LAUNCH",
    "TASK_SYNC_POLL_ACCOUNT",
    "TASK_SYNC_POLL_ALL",
    "Dispatch",
    "dispatch",
    "reset_dispatcher",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Queues and task names (docs/CONTRACTS.md §15 — frozen)
# ======================================================================================

QUEUE_DISCOVERY: Final[str] = "discovery"
QUEUE_AI: Final[str] = "ai"
QUEUE_APPLY: Final[str] = "apply"
QUEUE_KNOWLEDGE: Final[str] = "knowledge"
QUEUE_MAINTENANCE: Final[str] = "maintenance"

TASK_JOBS_POLL_ALL: Final[str] = "jobs.poll_all"
TASK_JOBS_POLL_PROVIDER: Final[str] = "jobs.poll_provider"
TASK_JOBS_SCORE_POSTING: Final[str] = "jobs.score_posting"
TASK_APPLY_PREPARE: Final[str] = "apply.prepare"
TASK_APPLY_SUBMIT: Final[str] = "apply.submit"
TASK_APPLY_RUN_ONE: Final[str] = "apply.run_one"
TASK_KNOWLEDGE_INDEX_SOURCE: Final[str] = "knowledge.index_source"
TASK_KNOWLEDGE_INDEX_ALL: Final[str] = "knowledge.index_all"
TASK_KNOWLEDGE_REFRESH_STALE: Final[str] = "knowledge.refresh_stale"
TASK_KNOWLEDGE_EMBED_BACKLOG: Final[str] = "knowledge.embed_backlog"
TASK_CLEANUP_TEMP_DOCUMENTS: Final[str] = "cleanup.temp_documents"
TASK_CLEANUP_EXPIRE_POSTINGS: Final[str] = "cleanup.expire_postings"
TASK_CLEANUP_PRUNE_ARTIFACTS: Final[str] = "cleanup.prune_artifacts"
TASK_CLEANUP_PRUNE_CACHE: Final[str] = "cleanup.prune_cache"
TASK_CLEANUP_REFRESH_GAUGES: Final[str] = "cleanup.refresh_gauges"
TASK_SESSION_WATCHDOG: Final[str] = "session.watchdog"
TASK_SYNC_POLL_ALL: Final[str] = "sync.poll_all"
TASK_SYNC_POLL_ACCOUNT: Final[str] = "sync.poll_account"
TASK_SYNC_DETECT_GHOSTED: Final[str] = "sync.detect_ghosted"
TASK_SYNC_ON_LAUNCH: Final[str] = "sync.on_launch"

#: Task name → the queue that consumes it. Routing lives here rather than in the caller so
#: an endpoint names *what* it wants done and never *where*.
TASK_QUEUES: Final[dict[str, str]] = {
    TASK_JOBS_POLL_ALL: QUEUE_DISCOVERY,
    TASK_JOBS_POLL_PROVIDER: QUEUE_DISCOVERY,
    TASK_JOBS_SCORE_POSTING: QUEUE_AI,
    TASK_APPLY_PREPARE: QUEUE_AI,
    TASK_APPLY_SUBMIT: QUEUE_APPLY,
    TASK_APPLY_RUN_ONE: QUEUE_APPLY,
    TASK_KNOWLEDGE_INDEX_SOURCE: QUEUE_KNOWLEDGE,
    TASK_KNOWLEDGE_INDEX_ALL: QUEUE_KNOWLEDGE,
    TASK_KNOWLEDGE_REFRESH_STALE: QUEUE_KNOWLEDGE,
    TASK_KNOWLEDGE_EMBED_BACKLOG: QUEUE_KNOWLEDGE,
    TASK_CLEANUP_TEMP_DOCUMENTS: QUEUE_MAINTENANCE,
    TASK_CLEANUP_EXPIRE_POSTINGS: QUEUE_MAINTENANCE,
    TASK_CLEANUP_PRUNE_ARTIFACTS: QUEUE_MAINTENANCE,
    TASK_CLEANUP_PRUNE_CACHE: QUEUE_MAINTENANCE,
    TASK_CLEANUP_REFRESH_GAUGES: QUEUE_MAINTENANCE,
    TASK_SESSION_WATCHDOG: QUEUE_MAINTENANCE,
    # Status sync (docs/CONTRACTS.md §17.7). Maintenance rather than discovery: these read a
    # mailbox and write a status, they never touch a provider or a browser.
    TASK_SYNC_POLL_ALL: QUEUE_MAINTENANCE,
    TASK_SYNC_POLL_ACCOUNT: QUEUE_MAINTENANCE,
    TASK_SYNC_DETECT_GHOSTED: QUEUE_MAINTENANCE,
    TASK_SYNC_ON_LAUNCH: QUEUE_MAINTENANCE,
}

#: Key under which :class:`Dispatch` reports degradation in an ``OkResponse.data`` body.
DEGRADED_KEY: Final[str] = "degraded"

#: Seconds to wait for the broker before giving up and reporting degradation. Short on
#: purpose: this runs inside a request the user is watching, and a slow broker must not turn
#: a button press into a hung UI.
BROKER_TIMEOUT_SECONDS: Final[float] = 3.0

#: Celery configuration applied to the client. Every value exists to make failure *fast*:
#: with retries enabled, an unreachable broker blocks a request for minutes.
_CLIENT_CONFIG: Final[dict[str, Any]] = {
    "broker_connection_retry": False,
    "broker_connection_retry_on_startup": False,
    "broker_connection_max_retries": 0,
    "broker_transport_options": {
        "max_retries": 0,
        "socket_timeout": BROKER_TIMEOUT_SECONDS,
        "socket_connect_timeout": BROKER_TIMEOUT_SECONDS,
    },
    "task_ignore_result": True,
}

#: Reason reported when the optional Celery dependency is not installed.
_REASON_NO_CELERY: Final[str] = (
    "Celery is not installed, so background work cannot be queued. The request was accepted "
    "and can be re-run once a worker is available."
)

#: Reason reported when the broker refused or timed out.
_REASON_NO_BROKER: Final[str] = (
    "The background worker's message broker is unreachable, so this work has not started "
    "yet. Start the worker (or Redis) and try again."
)


# ======================================================================================
# The result of an enqueue attempt
# ======================================================================================


@dataclass(slots=True, frozen=True)
class Dispatch:
    """The outcome of one attempt to enqueue background work.

    Attributes:
        task: The task name that was (or would have been) sent.
        queue: The queue it routes to.
        dispatched: Whether the broker accepted it.
        task_id: Celery's id for the enqueued task, when there is one.
        reason: Why it was not dispatched, phrased for a user. ``None`` on success.
    """

    task: str
    queue: str
    dispatched: bool
    task_id: str | None = None
    reason: str | None = None

    @property
    def degraded(self) -> bool:
        """Whether the caller should tell the user the work has not actually started."""
        return not self.dispatched

    def as_dict(self) -> dict[str, Any]:
        """Render the outcome for an :class:`~app.schemas.common.OkResponse` body.

        Returns:
            A JSON-ready mapping. ``degraded`` is always present so a client can branch on
            one key rather than inferring from the absence of ``task_id``.
        """
        payload: dict[str, Any] = {
            "task": self.task,
            "queue": self.queue,
            "dispatched": self.dispatched,
            DEGRADED_KEY: self.degraded,
        }
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


# ======================================================================================
# The client
# ======================================================================================

#: Memoised ``(broker_url, celery_app)``. Rebuilt when the configured broker changes.
_client: tuple[str, Any] | None = None

#: Guards :data:`_client` — FastAPI serves concurrent requests and two of them racing to
#: build a Celery app would open two connection pools.
_client_lock: Final[Lock] = Lock()


def _celery_client() -> Any | None:
    """Return a configured Celery client, or ``None`` when Celery is not installed.

    Returns:
        The memoised client for the currently configured broker URL.
    """
    global _client

    settings = get_settings()
    broker = settings.celery_broker_url or settings.redis_url

    with _client_lock:
        if _client is not None and _client[0] == broker:
            return _client[1]

        try:
            from celery import Celery
        except ImportError:
            logger.debug("api.celery_unavailable")
            return None

        application = Celery("applicantos", broker=broker)
        application.conf.update(_CLIENT_CONFIG)
        if settings.celery_result_backend:
            application.conf.result_backend = settings.celery_result_backend
        _client = (broker, application)
        logger.debug("api.celery_client_created")
        return application


def reset_dispatcher() -> None:
    """Discard the memoised Celery client so the next dispatch rebuilds it.

    For tests, and for a clean shutdown.
    """
    global _client
    with _client_lock:
        _client = None


def _send(
    client: Any,
    task: str,
    queue: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str | None:
    """Send one task synchronously. Runs on a worker thread.

    Args:
        client: The Celery application.
        task: Task name.
        queue: Target queue.
        args: Positional task arguments.
        kwargs: Keyword task arguments.

    Returns:
        The task id, or ``None`` when the broker did not supply one.
    """
    result = client.send_task(task, args=list(args), kwargs=kwargs, queue=queue)
    identifier = getattr(result, "id", None)
    return str(identifier) if identifier is not None else None


async def dispatch(
    task: str,
    *args: Any,
    queue: str | None = None,
    **kwargs: Any,
) -> Dispatch:
    """Enqueue one Celery task by name, reporting rather than raising on failure.

    The blocking ``send_task`` call runs on a worker thread and is bounded by
    :data:`BROKER_TIMEOUT_SECONDS`, so an unreachable broker costs the request three seconds
    and not a hung connection.

    Args:
        task: One of the ``TASK_*`` constants.
        *args: Positional arguments for the task. Must be JSON-serialisable — they cross a
            process boundary, so pass ids, never ORM rows.
        queue: Override the queue :data:`TASK_QUEUES` routes this task to.
        **kwargs: Keyword arguments for the task, with the same serialisability rule.

    Returns:
        The outcome. Callers return ``202`` either way and surface
        :attr:`Dispatch.degraded` to the user.
    """
    target_queue = queue or TASK_QUEUES.get(task, QUEUE_MAINTENANCE)

    client = _celery_client()
    if client is None:
        return Dispatch(task=task, queue=target_queue, dispatched=False, reason=_REASON_NO_CELERY)

    try:
        task_id = await asyncio.wait_for(
            asyncio.to_thread(_send, client, task, target_queue, args, kwargs),
            timeout=BROKER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("api.dispatch_timed_out", task=task, queue=target_queue)
        return Dispatch(task=task, queue=target_queue, dispatched=False, reason=_REASON_NO_BROKER)
    except Exception as exc:
        logger.warning(
            "api.dispatch_failed",
            task=task,
            queue=target_queue,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return Dispatch(task=task, queue=target_queue, dispatched=False, reason=_REASON_NO_BROKER)

    logger.info("api.dispatched", task=task, queue=target_queue, task_id=task_id)
    return Dispatch(task=task, queue=target_queue, dispatched=True, task_id=task_id)
