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

__all__ = [
    "CRONTAB_FIRST_DELAY",
    "CRONTAB_INTERVAL",
    "PeriodicScheduler",
    "start_scheduler",
    "stop_scheduler",
]

logger = structlog.get_logger(__name__)

#: How often the loop wakes to look for due work. Well below the shortest interval in the
#: schedule (five minutes), so a job fires within a few seconds of becoming due, and long
#: enough that an idle desktop app is not spinning.
TICK_SECONDS: Final[float] = 20.0

#: Seconds to wait for the loop to finish its current tick during shutdown.
STOP_TIMEOUT_SECONDS: Final[float] = 5.0

#: How long after launch a translated crontab entry first fires. Long enough that startup —
#: migrations, plugin registration, the first discovery poll — is over, short enough that it
#: still happens in a session somebody only keeps open for an hour.
CRONTAB_FIRST_DELAY: Final[timedelta] = timedelta(minutes=5)

#: How often a translated crontab entry repeats. Six hours rather than twenty-four because a
#: desktop app is not up for twenty-four: a daily interval measured from launch would, on a
#: machine used for a few hours an evening, never elapse. Both translated tasks are
#: idempotent, so firing more often than the server schedule does costs a query.
CRONTAB_INTERVAL: Final[timedelta] = timedelta(hours=6)


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

    Crontab entries are **translated, not skipped**. They name a wall-clock time — 03:00 and
    03:30 UTC — because that is when a server should do table-wide maintenance. A desktop
    machine is asleep then, so honouring the clock literally meant those two tasks never ran
    at all on a desktop install: ``sync.detect_ghosted`` is what turns a silent application
    into a ``ghosted`` one, and without it an application the employer never answered stayed
    "submitted" forever.

    The translation is deliberately not a clever cron emulation. Each crontab entry becomes
    "shortly after launch, then every :data:`CRONTAB_INTERVAL`", which fires at least once in
    any real session. That is safe because both tasks are idempotent and window-based rather
    than incremental: ``expire_postings`` expires anything past its age threshold and
    ``detect_ghosted`` marks anything silent past ``settings.ghosted_after_days``. Running
    either twice in a day costs one extra query and changes nothing.

    A real deployment with ``celery beat`` still gets the exact 03:00 schedule — this
    scheduler stands down the moment a worker appears.

    Args:
        now: The moment the scheduler started, used to seed the first due time.

    Returns:
        One entry per task, fixed-interval and translated-crontab alike.
    """
    from app.workers.celery_app import BEAT_SCHEDULE

    entries: list[_Entry] = []
    for name, spec in BEAT_SCHEDULE.items():
        schedule: Any = spec.get("schedule")
        if isinstance(schedule, timedelta):
            entries.append(_Entry(task=name, interval=schedule, due_at=now + schedule))
            continue
        # A crontab, or anything else Celery understands and this scheduler does not.
        logger.debug(
            "scheduler.crontab_translated",
            task=name,
            interval_seconds=CRONTAB_INTERVAL.total_seconds(),
            first_delay_seconds=CRONTAB_FIRST_DELAY.total_seconds(),
        )
        entries.append(
            _Entry(task=name, interval=CRONTAB_INTERVAL, due_at=now + CRONTAB_FIRST_DELAY)
        )
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
