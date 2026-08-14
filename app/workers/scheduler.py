"""The periodic scheduler, for an install that has no ``celery beat``.

"I turn it on, it finds jobs, it applies for me" is the product. Everything needed for that
existed except the clock. :data:`~app.workers.celery_app.BEAT_SCHEDULE` defines eight
recurring jobs — re-poll every provider every 30 minutes, reap stranded run sessions,
re-index stale knowledge, sweep temporary documents — and every one of them was reachable
*only* from ``celery beat``, a separate process that needs a broker. A desktop install has
neither, so nothing recurred: discovery ran when the user pressed a button and never again,
and a session that died stayed "running" forever because its reaper was itself scheduled.

This is that clock, in the API process. It ticks against the same
:data:`~app.workers.celery_app.BEAT_SCHEDULE` — one source of truth, so a schedule change
cannot apply to a deployed install and silently not to a desktop one — and hands each due
task to :func:`app.api.tasks.dispatch`, which makes the same routing decision as any other
caller.

**It refuses to run when a real beat might exist.** Two schedulers on one queue means every
recurring job runs twice: two discovery passes an hour, two watchdogs, two cleanup sweeps.
So it starts only when :func:`app.api.tasks.dispatch` would run work in-process anyway —
that is, when nothing else is consuming the queues — and it re-checks on every tick, so a
worker started later takes over and this steps aside without a restart.

**Nothing fires on startup.** Every entry's first run is one full interval after boot rather
than immediately. A desktop app that is opened and closed four times in a morning would
otherwise run four full discovery passes, and the 30-minute cadence exists precisely to be
polite to the job boards.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = ["PeriodicScheduler", "start_scheduler", "stop_scheduler"]

logger = structlog.get_logger(__name__)

#: How often the loop wakes to look for due work. Well below the shortest interval in the
#: schedule (five minutes), so a job fires within a few seconds of becoming due, and long
#: enough that an idle desktop app is not spinning.
TICK_SECONDS: Final[float] = 20.0

#: Seconds to wait for the loop to finish its current tick during shutdown.
STOP_TIMEOUT_SECONDS: Final[float] = 5.0


@dataclass(slots=True)
class _Entry:
    """One scheduled task and when it is next allowed to run.

    Attributes:
        task: The registered task name.
        interval: How long between runs.
        due_at: The next moment this may fire.
    """

    task: str
    interval: timedelta
    due_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _entries(now: datetime) -> list[_Entry]:
    """Build the schedule from Celery's own definition.

    Crontab entries are deliberately skipped. They exist for daily maintenance at 03:00 UTC
    — pruning artefacts, detecting ghosted applications — and a desktop machine is usually
    asleep then, so honouring a wall-clock time would mean either never running them or
    running them at a surprising moment on the next launch. They stay the responsibility of
    a real deployment, where a beat process is actually up at three in the morning.

    Args:
        now: The moment the scheduler started, used to seed the first due time.

    Returns:
        One entry per fixed-interval task, each first due a full interval from now.
    """
    from app.workers.celery_app import BEAT_SCHEDULE

    entries: list[_Entry] = []
    for name, spec in BEAT_SCHEDULE.items():
        schedule: Any = spec.get("schedule")
        if not isinstance(schedule, timedelta):
            logger.debug("scheduler.skipped_crontab", task=name)
            continue
        entries.append(_Entry(task=name, interval=schedule, due_at=now + schedule))
    return entries


class PeriodicScheduler:
    """Runs :data:`~app.workers.celery_app.BEAT_SCHEDULE` from inside the API process.

    Args:
        settings: Runtime configuration, consulted on every tick so that starting a Celery
            worker mid-session hands the schedule back to it.
    """

    def __init__(self, settings: Settings) -> None:
        """Build the scheduler. Nothing runs until :meth:`start`."""
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        """Whether the loop is live."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin ticking, if not already."""
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="applicantos-scheduler")
        logger.info("scheduler.started", tick_seconds=TICK_SECONDS)

    async def stop(self) -> None:
        """Stop ticking and wait briefly for the current tick to finish."""
        task, self._task = self._task, None
        if task is None:
            return
        self._stopping.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_SECONDS)
        logger.info("scheduler.stopped")

    async def _run(self) -> None:
        """Tick until stopped, dispatching whatever has come due."""
        schedule = _entries(datetime.now(UTC))
        logger.info("scheduler.schedule_built", entries=[e.task for e in schedule])

        while not self._stopping.is_set():
            try:
                await asyncio.sleep(TICK_SECONDS)
                await self._tick(schedule)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bad tick must not end the clock
                logger.error(
                    "scheduler.tick_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    async def _tick(self, schedule: list[_Entry]) -> None:
        """Dispatch every entry whose time has come.

        Args:
            schedule: The mutable schedule; due times are advanced in place.
        """
        from app.api.tasks import QUEUE_MAINTENANCE, TASK_QUEUES, dispatch, worker_serves

        # Re-checked every tick rather than once at startup: a user who starts a Celery
        # worker halfway through a session should not end up with two schedulers.
        if self._settings.task_execution == "worker":
            return
        if await worker_serves(QUEUE_MAINTENANCE):
            logger.debug("scheduler.standing_down", reason="a worker is consuming the queues")
            return

        now = datetime.now(UTC)
        for entry in schedule:
            if entry.due_at > now:
                continue
            entry.due_at = now + entry.interval
            outcome = await dispatch(entry.task, queue=TASK_QUEUES.get(entry.task))
            logger.info(
                "scheduler.fired",
                task=entry.task,
                mode=outcome.mode,
                next_due=entry.due_at.isoformat(),
            )


#: The process-wide scheduler, created by :func:`start_scheduler`.
_scheduler: PeriodicScheduler | None = None


def start_scheduler(settings: Settings) -> PeriodicScheduler | None:
    """Start the in-process scheduler unless a real beat is expected to exist.

    Args:
        settings: Runtime configuration.

    Returns:
        The started scheduler, or ``None`` when this install defers to ``celery beat``.
    """
    global _scheduler

    if settings.task_execution == "worker":
        logger.info("scheduler.disabled", reason="task_execution='worker' expects celery beat")
        return None
    if _scheduler is None:
        _scheduler = PeriodicScheduler(settings)
    _scheduler.start()
    return _scheduler


async def stop_scheduler() -> None:
    """Stop the process-wide scheduler, if one was started."""
    global _scheduler

    scheduler, _scheduler = _scheduler, None
    if scheduler is not None:
        await scheduler.stop()
