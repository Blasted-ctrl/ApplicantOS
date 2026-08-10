"""Running background work inside the API process, when no Celery worker will.

ApplicantOS is a desktop application. `CLAUDE.md` promises the whole pipeline runs with no
API keys and no Postgres, and `app/api/tasks.py` states that "the desktop install runs with
no broker at all" — but until this module existed, an install with no broker simply did not
run background work. Pressing **Start a Run** wrote a ``run_sessions`` row, enqueued
``jobs.poll_all`` and returned 202. Nothing consumed it. The session sat at ``running``
forever with a spinner next to it, and the only thing that could have closed it was itself a
queued task.

The failure had a nastier form than "no broker", and it is the one this module is really
for. ``redis_url`` defaults to ``redis://localhost:6379/0``, which is the default port for
*every* Redis on the machine. Point an install at a stray container — another project's, a
leftover from a tutorial — and the publish **succeeds**. Celery reports a task id, the API
reports ``degraded: false``, and the work is never executed by anybody. A liveness check that
asks "did the broker take it" cannot see that; the check has to ask "is a worker consuming
this queue", which is what :func:`app.api.tasks.worker_serves` does.

**This is a worker, so it lives in** ``app/workers/``. The API imports it lazily, inside the
one branch that needs it, which preserves the property `app/api/tasks.py` is careful about:
a process that only serves JSON never pulls Playwright and the document renderers in. Once
you have asked the API process to *be* the worker, importing the pipeline is precisely what
you asked for.

Three properties this must have, each because of a specific way the naive version is wrong:

**Never both.** A task runs on a Celery worker or here — never both, or golden rule #1 is
gone and an application is submitted twice. The decision is made once, in
:func:`app.api.tasks.dispatch`, and this module is only ever reached when that decision came
out "inline".

**Never in the request.** Discovery takes twelve minutes. Running it inside the HTTP call
that started it would hang the button that started it. Work is queued here and drained by
daemon threads, so the endpoint still answers immediately — the same shape as Celery, minus
the broker.

**Bounded.** A fan-out that enqueues one child per posting is 244 tasks from one press. An
unbounded queue turns that into memory pressure; a bounded one refuses politely and says so,
which is a fact the caller can report rather than a silent drop.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = [
    "InlineExecutor",
    "get_executor",
    "reset_executor_state",
    "shutdown_executor",
    "submit_inline",
]

logger = structlog.get_logger(__name__)

#: Sentinel pushed once per thread to end a drain loop. A dedicated object rather than
#: ``None`` so that a task named ``None`` — impossible today, but free to guard — could not
#: be mistaken for a shutdown request.
_STOP: Final[object] = object()

#: Seconds a drain thread waits on the queue before looking at the stop flag again. Short
#: enough that shutdown is prompt, long enough that an idle executor is not a spin loop.
_POLL_SECONDS: Final[float] = 0.5

#: How often to say that a job is still running. Chosen against the pipeline's own longest
#: legitimate step: `APPLY_SOFT_TIME_LIMIT_SECONDS` gives a browser submission 45 minutes, and
#: a discovery pass across three providers measured just under 13. Five minutes is therefore
#: noisy for nothing on a normal job and early enough to be useful on a stuck one.
_OVERRUN_WARN_SECONDS: Final[float] = 300.0

#: Seconds to wait for in-flight work at shutdown. A submission in progress deserves the
#: chance to finish and record itself; one that overruns is abandoned rather than hanging
#: the process, because the pipeline checkpoints and will resume (golden rule #8).
_SHUTDOWN_GRACE_SECONDS: Final[float] = 10.0


@dataclass(slots=True, frozen=True)
class _Job:
    """One unit of queued work.

    Attributes:
        task: The registered Celery task name, e.g. ``"jobs.poll_all"``.
        args: Positional arguments, already JSON-serialisable.
        kwargs: Keyword arguments, already JSON-serialisable.
    """

    task: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class InlineExecutor:
    """A minimal in-process worker pool that runs Celery tasks by name.

    Threads rather than an asyncio task group, because the task functions are synchronous
    Celery entry points that bridge to async through :func:`app.workers.run_async`. That
    bridge already detects a running loop and offloads to a helper thread, so calling it
    from anywhere is safe — but calling it from *these* threads keeps the API's event loop
    free to serve requests while a twelve-minute discovery runs.

    Args:
        workers: Number of drain threads.
        queue_size: Maximum queued jobs before :meth:`submit` starts refusing.
    """

    def __init__(self, workers: int, queue_size: int) -> None:
        """Create the pool. Threads are not started until :meth:`start`."""
        self._queue: queue.Queue[_Job | object] = queue.Queue(maxsize=max(1, queue_size))
        self._threads: list[threading.Thread] = []
        self._worker_count = max(1, workers)
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the drain threads, if they are not already running.

        Idempotent and safe to call from concurrent requests: the first caller through the
        lock starts the pool and the rest observe it started.
        """
        with self._lock:
            if self._threads:
                return
            self._stopping.clear()
            for index in range(self._worker_count):
                thread = threading.Thread(
                    target=self._drain,
                    name=f"applicantos-inline-{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            logger.info("inline.started", workers=self._worker_count)

    def shutdown(self, timeout: float = _SHUTDOWN_GRACE_SECONDS) -> None:
        """Stop draining and wait briefly for in-flight work.

        Args:
            timeout: Total seconds to wait, shared across threads.
        """
        import time

        with self._lock:
            threads, self._threads = self._threads, []
        if not threads:
            return

        self._stopping.set()
        for _ in threads:
            # A full queue is fine: those threads reach the stop flag on their next poll.
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(_STOP)

        # The budget is shared and spent as it elapses, not divided up front. Dividing gave
        # each thread `timeout / len(threads)` whether or not the others needed any of it —
        # so the single thread that was mid-submission got a quarter of the grace period
        # while three idle threads exited instantly and wasted the rest of theirs.
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        pending = self._queue.qsize()
        logger.info(
            "inline.stopped",
            abandoned=pending,
            note="abandoned work resumes from its checkpoint on the next run" if pending else None,
        )

    # ----------------------------------------------------------------------------------
    # Submission
    # ----------------------------------------------------------------------------------

    def submit(self, task: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        """Queue one task for execution in this process.

        Args:
            task: The registered task name.
            args: Positional arguments.
            kwargs: Keyword arguments.

        Returns:
            ``True`` when queued. ``False`` when the queue is full — reported rather than
            blocked on, because the caller is usually inside an HTTP request and a full
            queue must not become a hung button.
        """
        self.start()
        try:
            self._queue.put_nowait(_Job(task=task, args=args, kwargs=kwargs))
        except queue.Full:
            logger.warning("inline.queue_full", task=task, depth=self._queue.qsize())
            return False
        logger.debug("inline.queued", task=task, depth=self._queue.qsize())
        return True

    @property
    def depth(self) -> int:
        """Number of jobs waiting to start. Surfaced by ``GET /ready``."""
        return self._queue.qsize()

    @property
    def running(self) -> bool:
        """Whether any drain thread is alive."""
        return any(thread.is_alive() for thread in self._threads)

    # ----------------------------------------------------------------------------------
    # Execution
    # ----------------------------------------------------------------------------------

    def _drain(self) -> None:
        """Pull jobs and run them until stopped. One per thread."""
        while not self._stopping.is_set():
            try:
                item = self._queue.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                if item is _STOP or not isinstance(item, _Job):
                    return
                self._execute(item)
            finally:
                self._queue.task_done()

    def _execute(self, job: _Job) -> None:
        """Run one task to completion, swallowing its failure.

        A task that raises must not take the drain thread with it — the next job in the
        queue is unrelated and a dead pool would strand every future run in exactly the
        state this module exists to prevent. Celery's own ``apply`` captures the exception
        into the result, so the traceback is logged rather than propagated.

        Args:
            job: The work to run.
        """
        registered = _resolve(job.task)
        if registered is None:
            logger.error("inline.unknown_task", task=job.task)
            return

        logger.info("inline.executing", task=job.task)
        with _overrun_watch(job.task, self.depth):
            try:
                result = registered.apply(args=list(job.args), kwargs=job.kwargs)
            except Exception as exc:  # a failure inside apply() itself, not inside the task
                logger.error(
                    "inline.execute_crashed",
                    task=job.task,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                return

        if result.successful():
            logger.info("inline.executed", task=job.task)
            return
        logger.error(
            "inline.task_failed",
            task=job.task,
            error=str(result.result),
            error_type=type(result.result).__name__,
        )


@contextlib.contextmanager
def _overrun_watch(task: str, depth: int) -> Iterator[None]:
    """Log loudly, on a timer, while a job runs longer than any job should.

    Celery's ``task_time_limit`` and ``task_soft_time_limit`` are enforced by the *worker*
    around a delivered message; they do not apply to ``task.apply()``, which is a plain
    function call. So an inline job has no timeout, and on SQLite — where the pool is one
    thread by necessity — a single wedged ``apply.submit`` stops every other job in the
    process indefinitely.

    A Python thread cannot be killed from outside, so this does not pretend to enforce a
    limit. What it removes is the *silence*: a run that has stopped making progress looks
    exactly like a run that is working, which is the failure this whole module exists to
    end. The timer keeps firing, so the log says how long it has been stuck and how much
    work is queued behind it.

    The real ceilings still apply underneath — Playwright's own ``playwright_timeout_ms``
    bounds each browser step, and the pipeline checkpoints, so an abandoned job resumes
    rather than restarts (golden rule #8).

    Args:
        task: The task name, for the log line.
        depth: Jobs waiting behind this one.

    Yields:
        Nothing; the timer is cancelled on exit.
    """
    state: dict[str, Any] = {"elapsed": _OVERRUN_WARN_SECONDS, "timer": None}

    def _warn() -> None:
        logger.warning(
            "inline.job_overrunning",
            task=task,
            seconds=round(float(state["elapsed"])),
            queued_behind=depth,
        )
        state["elapsed"] = float(state["elapsed"]) + _OVERRUN_WARN_SECONDS
        _arm()

    def _arm() -> None:
        timer = threading.Timer(_OVERRUN_WARN_SECONDS, _warn)
        timer.daemon = True
        state["timer"] = timer
        timer.start()

    _arm()
    try:
        yield
    finally:
        pending = state["timer"]
        if pending is not None:
            pending.cancel()


#: Set once the task modules have been imported into the registry.
_registry_ready: Final[threading.Event] = threading.Event()

#: Serialises the one-time task-module import.
_registry_lock: Final[threading.Lock] = threading.Lock()


def _resolve(task: str) -> Any | None:
    """Look up a registered Celery task by name, importing the task modules on first use.

    Constructing the Celery app does **not** populate its registry — the decorators only run
    when ``app.workers.poll_jobs`` and friends are imported, which a real worker does on
    startup from the ``imports`` setting. This process is not a worker and never went through
    that path, so the first inline execution has to do it. Skipping this step is not a subtle
    failure: every task resolves to ``None`` and the run is dropped with a log line, which is
    the exact silence this module was written to eliminate.

    Args:
        task: The registered task name.

    Returns:
        The task object, or ``None`` when no such task exists.
    """
    from app.workers.celery_app import celery_app

    if not _registry_ready.is_set():
        with _registry_lock:
            if not _registry_ready.is_set():
                try:
                    celery_app.loader.import_default_modules()
                except Exception as exc:
                    # Fall back to importing the declared modules directly: a single bad
                    # module must not cost every other task its registration.
                    logger.warning(
                        "inline.default_imports_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    import importlib

                    from app.workers.celery_app import TASK_MODULES

                    for module in TASK_MODULES:
                        try:
                            importlib.import_module(module)
                        except Exception as module_error:
                            logger.error(
                                "inline.task_module_failed",
                                module=module,
                                error=str(module_error),
                                error_type=type(module_error).__name__,
                            )
                _registry_ready.set()
                logger.info("inline.registry_loaded", tasks=len(celery_app.tasks))

    return celery_app.tasks.get(task)


#: The process-wide pool. Created on first use so that importing this module — which the
#: API does lazily — never starts threads on its own.
_executor: InlineExecutor | None = None

#: Guards :data:`_executor` against two concurrent requests each building a pool.
_executor_lock: Final[threading.Lock] = threading.Lock()

#: Latched by :func:`shutdown_executor` so a late submission cannot rebuild the pool while
#: the application is stopping.
_shutting_down: Final[threading.Event] = threading.Event()


def get_executor(settings: Settings | None = None) -> InlineExecutor:
    """Return the process-wide inline executor, creating it on first call.

    Args:
        settings: Settings to size the pool from. Resolved from
            :func:`~app.config.settings.get_settings` when omitted.

    Returns:
        The started executor.

    Raises:
        RuntimeError: If the application is shutting down. Callers treat this the same as a
            full queue — the work did not start, and saying so is better than starting
            threads that outlive the engine they need.
    """
    global _executor

    if _shutting_down.is_set():
        raise RuntimeError("the inline executor is shutting down")

    with _executor_lock:
        if _executor is None:
            from app.config.settings import get_settings

            resolved = settings if settings is not None else get_settings()
            _executor = InlineExecutor(
                workers=_concurrency_for(resolved),
                queue_size=resolved.inline_task_queue_size,
            )
        _executor.start()
        return _executor


def _concurrency_for(settings: Settings) -> int:
    """How many drain threads this database can support.

    **SQLite permits exactly one writer.** Not one per connection and not one per table — one
    per database, for the duration of each write transaction. A discovery pass upserts a few
    hundred postings inside a single transaction, so two threads doing that concurrently do
    not interleave: one holds the write lock for the whole pass and the other waits.

    ``PRAGMA busy_timeout`` makes the waiter wait instead of failing instantly, which is
    necessary but not sufficient — measured against this pipeline, a second writer exhausted
    a ten-second timeout and lost 97 postings to ``discovery.ingest_failed``. Raising the
    timeout only trades lost work for a stalled thread. The honest fix is to stop creating
    the second writer, because on SQLite there is no such thing as two.

    PostgreSQL has real row-level concurrency, so a deployment on it uses the configured
    width.

    Args:
        settings: Runtime configuration.

    Returns:
        The number of threads to run.
    """
    configured = max(1, settings.inline_task_workers)
    if settings.sqlite_mode or settings.database_url.startswith("sqlite"):
        if configured > 1:
            logger.info("inline.serialised_for_sqlite", configured=configured)
        return 1
    return configured


def submit_inline(task: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    """Queue one task on the process-wide executor.

    Args:
        task: The registered task name.
        args: Positional arguments.
        kwargs: Keyword arguments.

    Returns:
        Whether it was queued. ``False`` when the pool is saturated or the application is
        stopping — both mean "this did not start", which the caller reports rather than
        letting the work vanish.
    """
    try:
        return get_executor().submit(task, args, kwargs)
    except RuntimeError:
        logger.warning("inline.rejected_while_stopping", task=task)
        return False


def shutdown_executor() -> None:
    """Stop the process-wide executor, if one was ever started.

    Called from the application lifespan alongside
    :func:`~app.api.tasks.reset_dispatcher`, so a desktop app that is closing does not leave
    daemon threads mid-submission.

    :data:`_shutting_down` is latched *before* the (slow) join, and :func:`get_executor`
    refuses once it is set. Without that latch the ten-second join is a window in which a
    request still in flight calls :func:`submit_inline`, finds ``_executor is None``, and
    builds a fresh pool with fresh threads — resurrecting the executor moments after the
    application said it had stopped, and leaving those threads running against an engine the
    lifespan is about to dispose.
    """
    global _executor

    _shutting_down.set()
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown()


def reset_executor_state() -> None:
    """Allow a new executor after a shutdown. For tests, and for an app that restarts in-process."""
    _shutting_down.clear()
