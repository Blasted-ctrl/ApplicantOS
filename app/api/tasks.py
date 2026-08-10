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

**Where the work goes is decided here, once.** :func:`dispatch` picks exactly one executor —
a Celery worker, or this process via :mod:`app.workers.inline` — and never both, which is
what keeps golden rule #1 (*never apply twice*) from depending on a race.

Under the default ``task_execution="auto"`` the choice is made by asking the workers whether
anyone is *consuming* the target queue (:func:`worker_serves`), not by whether the broker
accepted the publish. Those are different questions, and the gap between them was a real
outage: ``redis_url`` defaults to ``redis://localhost:6379/0``, the default port for every
Redis on a machine, so an install pointed at an unrelated container published successfully,
reported ``degraded: false``, and executed nothing. Every task the user started vanished.

**Work that did not start is a 202 with ``degraded``, never a 500.** ``docs/CONTRACTS.md``
§14 endpoints that trigger work return ``202 Accepted`` either way; the body carries
:attr:`Dispatch.degraded` and a reason the desktop app shows. A 500 would tell the user their
request failed when in fact the *request* was fine and the *system* is partly down. Note that
running inline is **not** degraded — the work is running, it is simply running here.

Task names and queue routing mirror ``docs/CONTRACTS.md`` §15 exactly. They are constants
here so that a typo is a failed import rather than a task that vanishes into a queue nobody
consumes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from typing import Any, Final, Literal

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
    "WORKER_PROBE_TIMEOUT_SECONDS",
    "WORKER_PROBE_TTL_SECONDS",
    "Dispatch",
    "dispatch",
    "reset_dispatcher",
    "reset_worker_probe",
    "worker_serves",
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

#: Reason reported when a publish was attempted and its outcome is unknown — a timeout, or a
#: transport error after the bytes may have left. Deliberately does **not** promise the work
#: did not start: it may be sitting in the broker waiting for a worker, and re-running it
#: here would be the second execution of a task that is already queued.
_REASON_AMBIGUOUS: Final[str] = (
    "The background worker's broker did not confirm this work. It may be queued and waiting "
    "for a worker to start, so it was not re-run here. Start the worker, or check the run "
    "before triggering it again."
)

#: Reason reported when the in-process executor is saturated. Distinct from a broker
#: failure: the system is working, it is simply already busy.
_REASON_INLINE_FULL: Final[str] = (
    "ApplicantOS is already running as much background work as it can hold. This request was "
    "not started; try again once the current run has caught up."
)

#: Seconds to wait for workers to answer a liveness probe. Separate from — and shorter than
#: — the publish timeout: this runs on the way *in* to a request the user is watching, and a
#: silent broker must cost a fraction of a second, not three.
WORKER_PROBE_TIMEOUT_SECONDS: Final[float] = 0.75

#: How long a liveness answer is trusted. Workers do not appear and vanish between two
#: clicks, and re-probing per dispatch would put a broker round trip in front of every
#: button press.
WORKER_PROBE_TTL_SECONDS: Final[float] = 15.0


# ======================================================================================
# The result of an enqueue attempt
# ======================================================================================


@dataclass(slots=True, frozen=True)
class Dispatch:
    """The outcome of one attempt to start background work.

    ``mode`` is the field that matters, and it exists because the original two-state version
    could not express the state the system was actually in. ``dispatched`` answered "did a
    broker accept the bytes", which is not the same question as "will this run" — an install
    pointed at a stray Redis answers yes to the first and no to the second. There are three
    real outcomes and they need three names.

    Attributes:
        task: The task name that was (or would have been) started.
        queue: The queue it routes to.
        mode: ``"worker"`` — handed to a Celery worker that is consuming this queue.
            ``"inline"`` — running in this process. ``"none"`` — not started at all.
        task_id: Celery's id, when it went to a worker.
        reason: Why it did not start, phrased for a user. ``None`` unless ``mode`` is
            ``"none"``.
    """

    task: str
    queue: str
    mode: Literal["worker", "inline", "none"]
    task_id: str | None = None
    reason: str | None = None

    @property
    def dispatched(self) -> bool:
        """Whether the work was started anywhere.

        Kept as a property rather than a field so the twelve call sites that read it keep
        working, and so it can never disagree with :attr:`mode`.
        """
        return self.mode != "none"

    @property
    def degraded(self) -> bool:
        """Whether the caller must tell the user the work has not actually started.

        Note that ``inline`` is **not** degraded. The work is running; it is simply running
        here. Reporting it as degradation would train the user to ignore the warning that
        matters.
        """
        return self.mode == "none"

    def as_dict(self) -> dict[str, Any]:
        """Render the outcome for an :class:`~app.schemas.common.OkResponse` body.

        Returns:
            A JSON-ready mapping. ``degraded`` and ``mode`` are always present, so a client
            can both branch on one boolean and say *where* the work went.
        """
        payload: dict[str, Any] = {
            "task": self.task,
            "queue": self.queue,
            "dispatched": self.dispatched,
            "mode": self.mode,
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


#: Memoised ``queue -> (answered_at, is_served)`` from the last liveness probe.
_probe_cache: dict[str, tuple[float, bool]] = {}

#: Guards :data:`_probe_cache`.
_probe_lock: Final[Lock] = Lock()


def _inspect_queues(client: Any) -> set[str]:
    """Return every queue name some live worker reports consuming. Runs on a worker thread.

    Args:
        client: The Celery application.

    Returns:
        The union of all responding workers' queues. Empty when nobody answers.
    """
    replies = client.control.inspect(timeout=WORKER_PROBE_TIMEOUT_SECONDS).active_queues()
    if not replies:
        return set()
    served: set[str] = set()
    for queues in replies.values():
        for entry in queues or ():
            name = entry.get("name") if isinstance(entry, dict) else None
            if name:
                served.add(str(name))
    return served


async def worker_serves(queue: str) -> bool:
    """Whether a live Celery worker is consuming *queue*.

    This is the question the old code never asked. ``send_task`` succeeding proves a broker
    accepted a message, not that anything will ever read it — and the default broker URL,
    ``redis://localhost:6379/0``, is the default port for every Redis on the machine. An
    install pointed at an unrelated container accepts every task and runs none, while the
    API cheerfully reports success. Asking the workers directly is the only check that can
    tell the two apart.

    Answers are cached for :data:`WORKER_PROBE_TTL_SECONDS`, and every failure mode — no
    Celery, no broker, a broker that hangs — resolves to ``False``, which routes the work
    inline rather than dropping it.

    Args:
        queue: The queue name to look for.

    Returns:
        ``True`` when some worker reports consuming *queue*.
    """
    import time

    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(queue)
        if cached is not None and (now - cached[0]) < WORKER_PROBE_TTL_SECONDS:
            return cached[1]

    client = _celery_client()
    served: set[str] = set()
    if client is not None:
        try:
            served = await asyncio.wait_for(
                asyncio.to_thread(_inspect_queues, client),
                # The inspect call has its own timeout; this one bounds the whole thing in
                # case the transport hangs below it, which a dead TCP peer can do.
                timeout=WORKER_PROBE_TIMEOUT_SECONDS * 2,
            )
        except Exception as exc:  # includes TimeoutError; every failure means "no worker"
            logger.debug("api.worker_probe_failed", error_type=type(exc).__name__)
            served = set()

    stamp = time.monotonic()
    with _probe_lock:
        # One probe answers for every queue, so cache them all rather than re-probing per
        # queue on the next dispatch to a different one.
        for known in TASK_QUEUES.values():
            _probe_cache[known] = (stamp, known in served)
        _probe_cache[queue] = (stamp, queue in served)
    return queue in served


def reset_worker_probe() -> None:
    """Forget cached liveness answers so the next dispatch re-probes.

    For tests, and for the moment a user starts a worker and does not want to wait out the
    TTL before the app notices.
    """
    with _probe_lock:
        _probe_cache.clear()


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


def _run_here(
    task: str,
    target_queue: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Dispatch:
    """Hand one task to the in-process executor.

    Args:
        task: The task name.
        target_queue: The queue it would have gone to, kept for reporting.
        args: Positional arguments.
        kwargs: Keyword arguments.

    Returns:
        ``mode="inline"`` when queued, ``mode="none"`` when the executor is saturated.
    """
    # Lazy, and only on this branch: the API process does not import the pipeline — with
    # Playwright and every renderer behind it — unless it has been asked to *be* the worker.
    from app.workers.inline import submit_inline

    if submit_inline(task, args, kwargs):
        logger.info("api.executing_inline", task=task, queue=target_queue)
        return Dispatch(task=task, queue=target_queue, mode="inline")

    logger.warning("api.inline_queue_full", task=task, queue=target_queue)
    return Dispatch(task=task, queue=target_queue, mode="none", reason=_REASON_INLINE_FULL)


async def dispatch(
    task: str,
    *args: Any,
    queue: str | None = None,
    **kwargs: Any,
) -> Dispatch:
    """Start one task by name — on a worker if one is listening, otherwise in this process.

    The routing decision is made **here and only here**, which is what keeps golden rule #1
    intact: a task goes to exactly one executor, never both, so an application cannot be
    submitted twice by a race between a worker and the API.

    Under ``task_execution="auto"`` (the desktop default) the choice is made by asking the
    workers whether anyone consumes the target queue — see :func:`worker_serves` — rather
    than by whether a broker accepts the publish, which is a question that returns "yes" for
    an install pointed at an unrelated Redis.

    The blocking ``send_task`` call runs on a worker thread and is bounded by
    :data:`BROKER_TIMEOUT_SECONDS`, so an unreachable broker costs the request three seconds
    and not a hung connection.

    Args:
        task: One of the ``TASK_*`` constants.
        *args: Positional arguments for the task. Must be JSON-serialisable — they may cross
            a process boundary, so pass ids, never ORM rows.
        queue: Override the queue :data:`TASK_QUEUES` routes this task to.
        **kwargs: Keyword arguments for the task, with the same serialisability rule.

    Returns:
        The outcome. Callers return ``202`` regardless and surface
        :attr:`Dispatch.degraded` — which is now true only when the work really did not
        start anywhere.
    """
    target_queue = queue or TASK_QUEUES.get(task, QUEUE_MAINTENANCE)
    mode = get_settings().task_execution

    if mode == "inline":
        return _run_here(task, target_queue, args, kwargs)

    client = _celery_client()
    if client is None:
        if mode == "auto":
            return _run_here(task, target_queue, args, kwargs)
        return Dispatch(task=task, queue=target_queue, mode="none", reason=_REASON_NO_CELERY)

    if mode == "auto" and not await worker_serves(target_queue):
        logger.info("api.no_worker_for_queue", task=task, queue=target_queue)
        return _run_here(task, target_queue, args, kwargs)

    try:
        task_id = await asyncio.wait_for(
            asyncio.to_thread(_send, client, task, target_queue, args, kwargs),
            timeout=BROKER_TIMEOUT_SECONDS,
        )
    # **Never fall back to inline after attempting a publish.**
    #
    # A timeout does not mean the message was rejected — it means the answer did not arrive
    # in time. The publish may already have landed in the broker, where a worker will pick it
    # up whenever one appears. Running it here as well would be two executions of one task,
    # and for `apply.submit` that is an application sent twice: golden rule #1, defeated by
    # the very mechanism meant to make the app more reliable.
    #
    # So the inline decision is made strictly *before* any publish (no client, or no worker
    # consuming the queue), never after one. Once bytes may be on the wire the only honest
    # answer is "this may or may not have started", which is reported as degraded.
    except TimeoutError:
        logger.warning("api.dispatch_timed_out", task=task, queue=target_queue)
        return _run_here(task, target_queue, args, kwargs)
    except Exception as exc:
        logger.warning(
            "api.dispatch_failed",
            task=task,
            queue=target_queue,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return Dispatch(task=task, queue=target_queue, mode="none", reason=_REASON_AMBIGUOUS)

    logger.info("api.dispatched", task=task, queue=target_queue, task_id=task_id)
    return Dispatch(task=task, queue=target_queue, mode="worker", task_id=task_id)
