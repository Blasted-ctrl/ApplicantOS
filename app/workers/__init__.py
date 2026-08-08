"""Celery workers — everything ApplicantOS does outside an HTTP request.

``docs/CONTRACTS.md`` §15 freezes the task names and the five queues. This package holds the
Celery application, the retry policy, and one module per task family. Every task in it obeys
the same three rules:

* **Tasks are thin.** Validate the arguments, open a :func:`~app.database.session.session_scope`,
  call one service method, map exceptions, return a small JSON dictionary. A task that
  contains business logic is a service method that lost its home — the API and the worker
  would then disagree about what the operation means.
* **Everything crossing the queue is JSON.** Ids as strings, never ORM rows: the argument
  list is serialised, and a row would be a stale snapshot by the time it was consumed.
* **``needs_review`` is terminal.** A retried escalation would re-drive a browser against a
  form a human is already looking at. :mod:`app.workers.retry` makes that an explicit check
  rather than an omission.

Why this module owns the event loop
-----------------------------------

Celery tasks are synchronous functions; every service in this codebase is a coroutine. The
bridge is :func:`run_async`, and its implementation matters more than it looks:

``asyncio.run`` would create **and destroy** an event loop per task. That is not merely
wasteful — SQLAlchemy's async engine caches connections whose lifetime is bound to the loop
that opened them, so a per-task loop leaves the pool holding connections belonging to a loop
that no longer exists, and the next task inherits a pool of corpses. :func:`run_async`
therefore keeps **one loop per worker thread**, in a :class:`threading.local`, reused for the
life of the process and disposed once at shutdown by :func:`shutdown_loop`.

The other half of the rule is that ``asyncio.run`` (and ``loop.run_until_complete``) raise
when a loop is already running on the calling thread. That happens whenever a task is invoked
inline from async code — ``task.apply()`` inside a test, or an eager/``task_always_eager``
configuration driven from an async harness. Rather than raise, :func:`run_async` offloads to
a dedicated helper thread with its own loop, so calling it is always safe and never touches a
loop that is already spinning.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Final

import structlog

__all__ = [
    "HELPER_THREAD_NAME_PREFIX",
    "LOOP_SHUTDOWN_TIMEOUT_SECONDS",
    "current_loop",
    "run_async",
    "shutdown_loop",
    "task_span",
]

logger = structlog.get_logger(__name__)

#: Seconds allowed for in-flight tasks to unwind when :func:`shutdown_loop` cancels them.
#: Long enough for a browser context to close politely, short enough that a wedged task
#: cannot hold a worker shutdown open indefinitely.
LOOP_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 10.0

#: Thread-name prefix for the fallback executor, so a stack dump names it unambiguously.
HELPER_THREAD_NAME_PREFIX: Final[str] = "applicantos-async"

#: Per-thread event loop. A prefork worker runs tasks on the main thread of each child; a
#: ``--pool=threads`` worker runs them on several. Both get exactly one loop per thread.
_LOCAL: Final[threading.local] = threading.local()

#: Lazily created single-thread executor used only when :func:`run_async` is called from a
#: thread that already has a *running* loop. ``None`` until that actually happens.
_helper: ThreadPoolExecutor | None = None

#: Guards :data:`_helper`.
_helper_lock: Final[threading.Lock] = threading.Lock()


# ======================================================================================
# The worker event loop
# ======================================================================================


def current_loop() -> asyncio.AbstractEventLoop:
    """Return this thread's worker event loop, creating it on first use.

    The loop is stored in a :class:`threading.local` and is **never** closed between tasks.
    Reuse is what keeps the SQLAlchemy connection pool valid: a pooled connection belongs to
    the loop that opened it, and a fresh loop per task would hand the next task connections
    bound to a loop that has already been closed.

    Returns:
        The loop dedicated to the calling thread. Also installed as the thread's current
        event loop, so libraries that call :func:`asyncio.get_event_loop` internally find it.
    """
    existing: asyncio.AbstractEventLoop | None = getattr(_LOCAL, "loop", None)
    if existing is not None and not existing.is_closed():
        return existing

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _LOCAL.loop = loop
    logger.debug("workers.loop_created", thread=threading.current_thread().name)
    return loop


def _helper_pool() -> ThreadPoolExecutor:
    """Return the single-thread executor used for the already-running-loop fallback.

    Returns:
        The process-wide executor, created on first use.
    """
    global _helper
    with _helper_lock:
        if _helper is None:
            _helper = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=HELPER_THREAD_NAME_PREFIX
            )
            logger.debug("workers.helper_pool_created")
        return _helper


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* to completion from synchronous code and return its result.

    The single bridge between Celery's synchronous task functions and this codebase's async
    services. It reuses the calling thread's long-lived loop (:func:`current_loop`) rather
    than creating one per call.

    When the calling thread already has a **running** loop — an inline ``task.apply()`` from
    async code, or ``task_always_eager`` driven from a test coroutine — driving that loop
    would raise. The coroutine is offloaded to a dedicated helper thread with its own loop
    instead, so this function is safe to call from anywhere and never re-enters a live loop.

    Args:
        coro: The coroutine to execute. It is always awaited exactly once, whichever path
            is taken.

    Returns:
        Whatever *coro* returns.

    Raises:
        BaseException: Anything *coro* raises, re-raised unchanged so the task's own error
            mapping sees the real exception rather than a wrapper.

    Example:
        >>> from app.workers import run_async  # doctest: +SKIP
        >>> run_async(pipeline.submit(application_id))  # doctest: +SKIP
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return current_loop().run_until_complete(coro)

    logger.debug("workers.loop_offloaded", thread=threading.current_thread().name)
    return _helper_pool().submit(lambda: current_loop().run_until_complete(coro)).result()


def shutdown_loop() -> None:
    """Cancel outstanding work and close this thread's loop, plus the helper pool.

    Called from the Celery ``worker_process_shutdown`` and ``worker_shutdown`` signals.
    Idempotent, and safe on a thread that never created a loop.

    Skipping it leaves async generators unfinalised and sockets open until the OS reaps
    them, which surfaces as connection-limit errors during rapid worker restarts.
    """
    global _helper
    with _helper_lock:
        pool, _helper = _helper, None
    if pool is not None:
        pool.shutdown(wait=True)
        logger.debug("workers.helper_pool_closed")

    loop: asyncio.AbstractEventLoop | None = getattr(_LOCAL, "loop", None)
    _LOCAL.loop = None
    if loop is None or loop.is_closed():
        return

    try:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=LOOP_SHUTDOWN_TIMEOUT_SECONDS))
        loop.run_until_complete(loop.shutdown_asyncgens())
    except RuntimeError as exc:
        logger.warning("workers.loop_shutdown_failed", error=str(exc))
    finally:
        loop.close()
        logger.debug("workers.loop_closed", thread=threading.current_thread().name)


# ======================================================================================
# Task instrumentation
# ======================================================================================


@contextmanager
def task_span(task: str, **bound: Any) -> Iterator[structlog.BoundLogger]:
    """Time one task body, bind its context, and record the outcome metric.

    Emits ``applicantos_task_duration_seconds{task,outcome}`` (``docs/CONTRACTS.md`` §16) and
    yields a logger with *bound* already attached, so every line a task writes carries the
    same correlation keys.

    Observability is never load-bearing: if the metrics module cannot be imported — a slim
    install without ``prometheus_client`` and without the observability package — the span
    still logs and still re-raises, it just does not measure.

    Args:
        task: The frozen task name, used as the metric label.
        **bound: structlog keys to bind for the duration (``user_id``, ``posting_id``, …).

    Yields:
        A bound logger for the task body.

    Raises:
        BaseException: Anything the body raises, after logging it.
    """
    log = logger.bind(task=task, **bound)
    try:
        from app.observability.metrics import track_task
    except ImportError:  # pragma: no cover - observability is always present in this tree
        log.debug("workers.metrics_unavailable")
        try:
            yield log
        except Exception as exc:
            log.error("workers.task_failed", error=str(exc), error_type=type(exc).__name__)
            raise
        return

    with track_task(task):
        try:
            yield log
        except Exception as exc:
            log.error("workers.task_failed", error=str(exc), error_type=type(exc).__name__)
            raise


def __getattr__(name: str) -> Any:
    """Resolve :data:`celery_app` lazily so importing this package does not need Celery.

    ``run_async`` is useful to any synchronous entry point — a script, a test — and none of
    those should have to install Celery to get it. The Celery application is therefore
    materialised only when somebody actually asks for it.

    Args:
        name: The attribute being looked up.

    Returns:
        The Celery application when *name* is ``"celery_app"``.

    Raises:
        AttributeError: For every other name.
    """
    if name == "celery_app":
        from app.workers.celery_app import celery_app

        return celery_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return the package's public names, including the lazy Celery application."""
    return sorted({*__all__, "celery_app", *globals()})
