"""The Celery application, its routing table, and its process lifecycle.

``docs/CONTRACTS.md`` §15 freezes the app name (``applicantos``), the five queues, the task
names and the beat schedule. This module is where those become configuration.

Unlike every other module in the backend, Celery is imported at **module scope** here. The
lazy-import rule exists so the application can be imported without its optional
dependencies; this module *is* the Celery dependency, and a lazily-constructed app would not
let :func:`celery_app.task` decorate anything at import time. Every other third-party import
in this package stays lazy. :mod:`app.api.tasks` is the mirror image: it talks to the same
broker with a bare client and imports Celery inside a function, because the API process must
run with no broker at all.

The configuration below is deliberate rather than defaulted:

``task_acks_late=True`` + ``worker_prefetch_multiplier=1``
    A task is acknowledged when it *finishes*, not when it is delivered, and a worker holds
    exactly one message at a time. Together these mean a worker killed mid-application
    redelivers that application instead of losing it, and a slow ``apply.submit`` cannot sit
    on a prefetched backlog that another idle worker could have run.

``task_reject_on_worker_lost=True``
    A hard-killed child (OOM, ``SIGKILL``) returns its message to the queue rather than
    silently dropping it. Combined with the idempotency guarantees in §13 — ``ingest``
    upserts, ``prepare`` is a no-op past ``READY``, ``submit`` refuses past ``SUBMITTED`` —
    redelivery is safe: golden rule #1 is enforced in the database, not in the queue.

``worker_hijack_root_logger=False``
    Celery would otherwise reconfigure the root logger and destroy the structlog chain,
    taking the ``redact_secrets`` processor with it. Golden rule #4 makes that unacceptable,
    so logging is configured from :func:`app.config.logging.configure_logging` by the
    ``setup_logging`` signal handler below, whose mere existence tells Celery to keep its
    hands off.

``@worker_process_init`` disposing the engine
    ``app.database.session`` builds the engine at import time, so a prefork parent that has
    imported the task modules hands every child a **copy of its connection pool** — the same
    file descriptors, in several processes at once. That produces interleaved protocol
    traffic and "connection is closed" errors that look like database flakiness. The handler
    replaces the pool without closing the parent's sockets (``dispose(close=False)``), which
    is the only correct thing to do after a fork.
"""

from __future__ import annotations

from datetime import timedelta
from threading import Event
from typing import Any, Final

import structlog
from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    setup_logging,
    worker_process_init,
    worker_process_shutdown,
    worker_ready,
    worker_shutdown,
)

from app.api.tasks import (
    QUEUE_AI,
    QUEUE_APPLY,
    QUEUE_DISCOVERY,
    QUEUE_KNOWLEDGE,
    QUEUE_MAINTENANCE,
    TASK_CLEANUP_EXPIRE_POSTINGS,
    TASK_CLEANUP_REFRESH_GAUGES,
    TASK_CLEANUP_TEMP_DOCUMENTS,
    TASK_JOBS_POLL_ALL,
    TASK_KNOWLEDGE_REFRESH_STALE,
    TASK_QUEUES,
    TASK_SESSION_WATCHDOG,
    TASK_SYNC_DETECT_GHOSTED,
    TASK_SYNC_POLL_ALL,
)
from app.config.settings import get_settings
from app.workers import shutdown_loop

#: Set once this process is a running Celery worker.
#:
#: ``app.workers.celery_app`` is imported by the API too — the in-process executor resolves
#: tasks through its registry — so "was this module imported" says nothing about what the
#: process *is*. Only the ``worker_ready`` signal does, and :func:`_executing_inline` needs
#: the distinction: inside a real worker a fan-out must go back to the broker so the whole
#: pool shares it, while in an API process that has adopted the work it must stay here.
_IN_CELERY_WORKER: Final[Event] = Event()

__all__ = [
    "APPLY_HARD_TIME_LIMIT_SECONDS",
    "APPLY_SOFT_TIME_LIMIT_SECONDS",
    "BEAT_SCHEDULE",
    "CELERY_APP_NAME",
    "DAILY_MAINTENANCE_HOUR_UTC",
    "HARD_TIME_LIMIT_SECONDS",
    "MIN_SYNC_INTERVAL_MINUTES",
    "SOFT_TIME_LIMIT_SECONDS",
    "SYNC_INTERVAL_MINUTES",
    "TASK_MODULES",
    "TASK_ROUTES",
    "celery_app",
    "enqueue",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: The application name every worker, beat process and client must agree on.
CELERY_APP_NAME: Final[str] = "applicantos"

#: Modules Celery imports on startup so their ``@celery_app.task`` decorators run. Missing
#: one would mean a queue that nobody consumes and a task name that resolves to nothing.
TASK_MODULES: Final[tuple[str, ...]] = (
    "app.workers.poll_jobs",
    "app.workers.apply_jobs",
    "app.workers.index_knowledge",
    "app.workers.cleanup",
    "app.workers.sync_status",
)

#: Seconds a task may run before ``SoftTimeLimitExceeded`` is raised inside it, giving it a
#: chance to close a browser and record a failure rather than vanishing.
SOFT_TIME_LIMIT_SECONDS: Final[int] = 20 * 60

#: Seconds before the worker kills the child outright. The gap to the soft limit is the
#: unwind budget.
HARD_TIME_LIMIT_SECONDS: Final[int] = 25 * 60

#: Apply tasks drive a real browser through a multi-page form and get a longer leash.
APPLY_SOFT_TIME_LIMIT_SECONDS: Final[int] = 45 * 60

#: Hard ceiling for the apply queue.
APPLY_HARD_TIME_LIMIT_SECONDS: Final[int] = 50 * 60

#: Hour (UTC) the daily maintenance sweeps run. Off-peak on purpose: they take table-wide
#: locks on nothing, but they do walk the filesystem.
DAILY_MAINTENANCE_HOUR_UTC: Final[int] = 3

#: Seconds a queued message stays relevant. A discovery poll that has been sitting for
#: longer than its own interval is stale — the next tick supersedes it, and running both
#: doubles the provider traffic for no new postings.
POLL_EXPIRY_SECONDS: Final[int] = 25 * 60

#: How often the mailbox sweep runs (``docs/CONTRACTS.md`` §17.7). Read from settings at
#: import so an operator can slow it down without editing code; clamped to a sane floor
#: because a one-minute schedule against a mail provider is how an account gets rate limited.
MIN_SYNC_INTERVAL_MINUTES: Final[int] = 5
SYNC_INTERVAL_MINUTES: Final[int] = max(
    MIN_SYNC_INTERVAL_MINUTES,
    int(get_settings().status_sync_interval_minutes),
)

#: Task name → routing options. Derived from the single frozen mapping in
#: :data:`app.api.tasks.TASK_QUEUES`, so the API and the workers can never disagree about
#: which queue a task belongs to.
TASK_ROUTES: Final[dict[str, dict[str, str]]] = {
    name: {"queue": queue} for name, queue in TASK_QUEUES.items()
}

#: The scheduler, exactly as ``docs/CONTRACTS.md`` §15 specifies it. Keys are the task names
#: themselves so an entry is greppable from the task it drives.
BEAT_SCHEDULE: Final[dict[str, dict[str, Any]]] = {
    TASK_JOBS_POLL_ALL: {
        "task": TASK_JOBS_POLL_ALL,
        "schedule": timedelta(minutes=30),
        "options": {"queue": QUEUE_DISCOVERY, "expires": POLL_EXPIRY_SECONDS},
    },
    TASK_KNOWLEDGE_REFRESH_STALE: {
        "task": TASK_KNOWLEDGE_REFRESH_STALE,
        "schedule": timedelta(minutes=60),
        "options": {"queue": QUEUE_KNOWLEDGE},
    },
    TASK_CLEANUP_TEMP_DOCUMENTS: {
        "task": TASK_CLEANUP_TEMP_DOCUMENTS,
        "schedule": timedelta(hours=1),
        # Scheduled sweeps delete for real; an operator running one by hand still gets the
        # dry run, because ``apply`` defaults to False everywhere else.
        "kwargs": {"apply": True},
        "options": {"queue": QUEUE_MAINTENANCE},
    },
    TASK_CLEANUP_EXPIRE_POSTINGS: {
        "task": TASK_CLEANUP_EXPIRE_POSTINGS,
        "schedule": crontab(hour=DAILY_MAINTENANCE_HOUR_UTC, minute=0),
        "kwargs": {"apply": True},
        "options": {"queue": QUEUE_MAINTENANCE},
    },
    # Gauges are process-local and start at zero, so after any restart
    # `applicantos_review_queue_size` and `applicantos_session_active` claim "nothing pending,
    # nothing running" until something recounts them. `cleanup.refresh_gauges` exists for
    # exactly that and sets both correctly -- it was simply never scheduled, so the gauges sat
    # at zero forever while the review queue filled up behind them. Five minutes matches the
    # watchdog: often enough that a dashboard is not lying for long, cheap enough that it is
    # two COUNT queries.
    TASK_CLEANUP_REFRESH_GAUGES: {
        "task": TASK_CLEANUP_REFRESH_GAUGES,
        "schedule": timedelta(minutes=5),
        "options": {"queue": QUEUE_MAINTENANCE},
    },
    TASK_SESSION_WATCHDOG: {
        "task": TASK_SESSION_WATCHDOG,
        "schedule": timedelta(minutes=5),
        "options": {"queue": QUEUE_MAINTENANCE},
    },
    # Status sync (§17.7). The mailbox sweep expires like the discovery poll: a run that has
    # been queued longer than its own interval has been superseded by the next tick, and
    # executing both would re-read the same window twice for nothing.
    TASK_SYNC_POLL_ALL: {
        "task": TASK_SYNC_POLL_ALL,
        "schedule": timedelta(minutes=SYNC_INTERVAL_MINUTES),
        "options": {
            "queue": QUEUE_MAINTENANCE,
            "expires": SYNC_INTERVAL_MINUTES * 60,
        },
    },
    TASK_SYNC_DETECT_GHOSTED: {
        "task": TASK_SYNC_DETECT_GHOSTED,
        "schedule": crontab(hour=DAILY_MAINTENANCE_HOUR_UTC, minute=30),
        "options": {"queue": QUEUE_MAINTENANCE},
    },
}


# ======================================================================================
# The application
# ======================================================================================


def _build_celery_app() -> Celery:
    """Construct and configure the Celery application from settings.

    Returns:
        The configured application. No connection is opened here — Celery connects lazily,
        which is what lets the API import :data:`TASK_ROUTES` on a machine with no broker.
    """
    settings = get_settings()
    broker = settings.celery_broker_url or settings.redis_url

    application = Celery(
        CELERY_APP_NAME,
        broker=broker,
        backend=settings.celery_result_backend or None,
        include=list(TASK_MODULES),
    )
    application.conf.update(
        # -- serialisation: JSON only, in and out -------------------------------------
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        result_accept_content=["json"],
        # -- time ----------------------------------------------------------------------
        timezone="UTC",
        enable_utc=True,
        # -- delivery semantics ---------------------------------------------------------
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_track_started=True,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=200,
        # -- limits -----------------------------------------------------------------------
        task_soft_time_limit=SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=HARD_TIME_LIMIT_SECONDS,
        broker_connection_retry_on_startup=True,
        result_expires=timedelta(days=1),
        # -- routing and scheduling ---------------------------------------------------------
        task_routes=dict(TASK_ROUTES),
        task_default_queue=QUEUE_MAINTENANCE,
        beat_schedule=dict(BEAT_SCHEDULE),
        # -- logging: ours, not Celery's ------------------------------------------------------
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
    )
    logger.debug(
        "workers.celery_configured",
        queues=sorted({QUEUE_DISCOVERY, QUEUE_AI, QUEUE_APPLY, QUEUE_KNOWLEDGE, QUEUE_MAINTENANCE}),
        tasks=len(TASK_ROUTES),
        beat_entries=len(BEAT_SCHEDULE),
    )
    return application


#: The process-wide Celery application. ``celery -A app.workers.celery_app worker`` and
#: ``celery -A app.workers.celery_app beat`` both resolve this name.
celery_app: Final[Celery] = _build_celery_app()


def enqueue(task: str, *args: Any, queue: str | None = None, **kwargs: Any) -> str | None:
    """Send one task from inside a worker, reporting rather than raising on failure.

    The worker-side counterpart to :func:`app.api.tasks.dispatch`. A fan-out task that could
    not enqueue one of its children must still finish and report the others, so a single
    unroutable message never costs a whole discovery cycle.

    Args:
        task: One of the frozen ``TASK_*`` names.
        *args: Positional arguments. JSON-serialisable only — ids as strings, never rows.
        queue: Override the queue :data:`TASK_ROUTES` sends this task to.
        **kwargs: Keyword arguments, with the same serialisability rule.

    Returns:
        The new task's id, or ``None`` when the broker refused it.
    """
    target = queue or TASK_QUEUES.get(task, QUEUE_MAINTENANCE)

    # A chain is only as good as its weakest hop. `jobs.poll_all` fans out to one
    # `jobs.poll_provider` per provider, each of which fans out to `jobs.score_posting`, and
    # so on to `apply.prepare`. If the entry point runs in-process but the children are
    # published to a broker nobody reads, the run stops one step in — discovery completes,
    # scoring never starts, and the result looks like a pipeline that quietly gave up. So
    # the same routing decision is made here, from inside the worker, as the API makes at
    # the front door.
    if _executing_inline():
        from app.workers.inline import submit_inline

        if submit_inline(task, tuple(args), dict(kwargs)):
            logger.debug("workers.enqueued_inline", task=task, queue=target)
            return None
        logger.warning("workers.inline_queue_full", task=task, queue=target)
        return None

    try:
        result = celery_app.send_task(task, args=list(args), kwargs=kwargs, queue=target)
    except Exception as exc:
        logger.warning(
            "workers.enqueue_failed",
            task=task,
            queue=target,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    identifier = getattr(result, "id", None)
    logger.debug("workers.enqueued", task=task, queue=target, task_id=str(identifier))
    return str(identifier) if identifier is not None else None


def _executing_inline() -> bool:
    """Whether this process runs its own background work rather than publishing it.

    True under ``task_execution="inline"``, and under ``"auto"`` when the current process is
    *not* a Celery worker. The worker check is what keeps a real deployment correct: inside
    ``celery worker`` the fan-out must go back to the broker so the pool shares it, and only
    an API process that has adopted the work should keep it.

    Returns:
        Whether children of the running task should execute in this process.
    """
    mode = get_settings().task_execution
    if mode == "worker":
        return False
    if mode == "inline":
        return True
    return not _IN_CELERY_WORKER.is_set()


# ======================================================================================
# Process lifecycle
# ======================================================================================


@setup_logging.connect
def _configure_worker_logging(**_kwargs: Any) -> None:
    """Install the structlog chain instead of letting Celery configure logging.

    Connecting to ``setup_logging`` at all is what suppresses Celery's own logging setup;
    the body then installs ours. Without this the root logger is reconfigured and the
    ``redact_secrets`` processor disappears from the chain, which would put provider tokens
    and API keys into worker logs (golden rule #4).
    """
    from app.config.logging import configure_logging

    configure_logging(get_settings())
    logger.debug("workers.logging_configured")


@worker_ready.connect
def _mark_celery_worker(**_kwargs: Any) -> None:
    """Record that this process is a Celery worker, not an API process.

    Set on ``worker_ready`` rather than at import, because importing this module is exactly
    what the API does to reach the task registry.
    """
    _IN_CELERY_WORKER.set()
    logger.debug("workers.identified_as_worker")


@worker_process_init.connect
def _reset_inherited_resources(**_kwargs: Any) -> None:
    """Drop every process-global resource inherited across the prefork boundary.

    A forked child inherits the parent's connection pool, cache client and vector store
    handle — all of them holding file descriptors the parent also holds. Sharing an async
    connection between two processes produces interleaved protocol traffic that surfaces as
    random "connection is closed" failures, so each child starts from an empty pool.

    ``dispose(close=False)`` replaces the pool **without** closing the underlying sockets:
    they still belong to the parent, and closing them here would break it.
    """
    from app.database.session import engine

    try:
        engine.sync_engine.dispose(close=False)
    except Exception as exc:
        logger.warning("workers.engine_reset_failed", error=str(exc), error_type=type(exc).__name__)

    try:
        from app.cache import reset_cache

        reset_cache()
    except ImportError:  # pragma: no cover - cache is always present in this tree
        logger.debug("workers.cache_reset_skipped")

    try:
        from app.knowledge.vector import reset_vector_store

        reset_vector_store()
    except ImportError:  # pragma: no cover - vector package is always present
        logger.debug("workers.vector_store_reset_skipped")

    logger.debug("workers.process_initialized")


@worker_process_shutdown.connect
def _close_child_loop(**_kwargs: Any) -> None:
    """Close the child's event loop and helper pool when a prefork child exits."""
    shutdown_loop()


@worker_shutdown.connect
def _close_worker_loop(**_kwargs: Any) -> None:
    """Close the loop for pools that have no separate child process (``solo``, ``threads``)."""
    shutdown_loop()
