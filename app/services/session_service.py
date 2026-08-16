"""Run sessions — starting one, counting it, finishing it, and reaping it (§13).

A :class:`~app.models.session.RunSession` is the row the live dashboard binds to. It is
deliberately a *rollup*: the applications carry the detail, the session carries six counters,
and the dashboard paints a progress bar from one row instead of aggregating a growing table
on every WebSocket tick. This service owns that row's whole life.

Three decisions are worth stating up front, because each of them is the reason a method looks
the way it does.

**One running session per user.** :meth:`SessionService.start` refuses to open a second
concurrent run and hands back the one already in flight. Two runs racing over the same
postings would fight for the daily cap, double-count every counter, and make "how is this run
going?" unanswerable. Returning the existing session rather than raising keeps the caller's
code straight: "give me the current run" and "start a run" are the same request from the
desktop app's point of view.

**Counters increment in SQL, not in Python.** :meth:`SessionService.record` compiles to
``UPDATE run_sessions SET jobs_found = jobs_found + :delta``. Read-modify-write on a loaded
ORM instance loses counts the moment two workers report progress on the same run, which is
the normal case: discovery, scoring and applying run on three different queues. For the same
reason ``avg_application_seconds`` is **recomputed** from the applications themselves rather
than folded in from a caller-supplied sample — a running mean maintained by whoever happens
to call first is a running mean that drifts.

**A crashed run must not stay "running" forever.** :meth:`SessionService.watchdog` closes
sessions whose ``updated_at`` has gone stale, which is what the ``session.watchdog`` beat task
(``docs/CONTRACTS.md`` §15, every five minutes) exists to drive. Without it, a killed worker
leaves a session that blocks every subsequent :meth:`start` for that user — the one-running-
session rule turning from a safeguard into a deadlock.

Methods return ORM rows rather than schemas, matching
:class:`app.services.review_service.ReviewService`: the API layer validates them into
:class:`~app.schemas.session.SessionRead`, while the workers that call the same methods need
the row itself and would otherwise have to re-select what they were just handed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import func, select, update

from app.database.types import utcnow
from app.models.application import Application
from app.models.checkpoint import Checkpoint
from app.models.enums import (
    APPLICATION_POST_SUBMIT_STATES,
    STOP_REASONS_COMPLETING,
    ApplicationStatus,
    CheckpointStatus,
    SessionStatus,
    StopReason,
)
from app.models.session import (
    COUNTER_FLOOR,
    DEFAULT_SESSION_TRIGGER,
    SESSION_COUNTER_FIELDS,
    RunSession,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DEFAULT_STALE_AFTER_MINUTES",
    "MAX_SCORE",
    "MAX_STALE_AFTER_MINUTES",
    "MIN_STALE_AFTER_MINUTES",
    "STATS_STATUS_PREFIX",
    "SessionService",
    "status_for",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: How long a run may go without a counter update before the watchdog declares it dead.
#: Comfortably longer than the ``session.watchdog`` beat interval (five minutes) and longer
#: than the slowest single step — a browser flow with uploads — so a slow run is never reaped
#: out from under itself.
DEFAULT_STALE_AFTER_MINUTES: Final[int] = 15

#: Floor on the watchdog window. Anything shorter would reap runs that are merely busy.
MIN_STALE_AFTER_MINUTES: Final[int] = 1

#: Ceiling on the watchdog window. A week-old "running" session is not a session anybody is
#: still waiting on, and allowing an unbounded window would let one bad call disable reaping.
MAX_STALE_AFTER_MINUTES: Final[int] = 7 * 24 * 60

#: Prefix under which :meth:`SessionService.stats` reports per-status application counts, so
#: the flat ``dict[str, int]`` the schema declares stays unambiguous.
STATS_STATUS_PREFIX: Final[str] = "status_"

#: Top of the normalised score band, mirroring :attr:`app.models.score.JobScore.normalized`.
#: A run cannot ask for a threshold above it: nothing could ever clear one.
MAX_SCORE: Final[int] = 100

#: Checkpoint statuses that still represent outstanding work for a run.
_OUTSTANDING_CHECKPOINT_STATES: Final[tuple[CheckpointStatus, ...]] = (
    CheckpointStatus.PENDING,
    CheckpointStatus.RUNNING,
    CheckpointStatus.FAILED,
)

#: Deterministic ordering of the post-submit statuses for the ``IN`` clause in
#: :meth:`SessionService.stats`. Frozenset iteration order varies between processes, which
#: would defeat SQLAlchemy's compiled-statement cache.
_POST_SUBMIT_ORDERED: Final[tuple[ApplicationStatus, ...]] = tuple(
    sorted(APPLICATION_POST_SUBMIT_STATES, key=lambda status: status.value)
)


def status_for(reason: StopReason) -> SessionStatus:
    """Return the session status a given stop reason implies.

    The one mapping from "why it stopped" to "how it ended", so the two can never contradict
    each other on the same row. A run that hit its cap or ran out of postings did what was
    asked of it and is ``completed``; a run the user stopped is ``cancelled``; a run that
    stalled or broke underneath is ``failed``.

    Args:
        reason: Why the run stopped.

    Returns:
        The terminal status to record.
    """
    resolved = StopReason(reason)
    if resolved in STOP_REASONS_COMPLETING:
        return SessionStatus.COMPLETED
    if resolved is StopReason.USER_STOPPED:
        return SessionStatus.CANCELLED
    return SessionStatus.FAILED


class SessionService:
    """Owns the :class:`~app.models.session.RunSession` row for one unit of work.

    Args:
        session: The database session. Every mutating method commits, because a run's
            counters are read by other processes — the desktop app polling ``GET /sessions``
            and the watchdog deciding what is stale — and an uncommitted counter is invisible
            to both.

    Usage::

        sessions = SessionService(db)
        run = await sessions.start(user.id, "manual")
        await sessions.record(run.id, jobs_found=12, jobs_qualified=3)
        await sessions.finish(run.id, SessionStatus.COMPLETED)
    """

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to one database session."""
        self._session = session

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------

    async def start(
        self,
        user_id: uuid.UUID | str,
        trigger: str = DEFAULT_SESSION_TRIGGER,
        *,
        config_snapshot: Mapping[str, Any] | None = None,
        max_applications: int | None = None,
        match_threshold: int | None = None,
    ) -> RunSession:
        """Open a run for *user_id*, or return the one already running.

        A second concurrent run is refused rather than created: two runs would compete for
        the same daily cap and double-count every counter, and the dashboard has one progress
        bar. The existing run is returned instead of raising, because from the caller's side
        "start a run" and "give me the current run" are the same intent.

        Args:
            user_id: The user the run belongs to.
            trigger: What started it — ``"manual"``, ``"schedule"``, ``"api"``.
            config_snapshot: Effective configuration at start, stored verbatim so a run stays
                reproducible. **The caller is responsible for excluding secrets**: this is a
                JSON column, and the structlog redaction chain governs logs, not the database.
            max_applications: Cap for this run alone, or ``None`` to be governed by the
                configured session cap. Stored on the row rather than only in the snapshot
                because the submit ladder reads it on every attempt, and a limit that lives
                only in a JSON blob nobody queries is a limit that does not exist.
            match_threshold: Minimum normalised score for this run, or ``None`` for the
                configured floor. Narrowing only — see
                :meth:`~app.models.session.RunSession.score_floor`.

        Returns:
            The running session, committed.

        Raises:
            LookupError: If *user_id* is malformed.
            ValueError: If either narrowing is negative, or *match_threshold* exceeds 100.
        """
        identifier = _as_uuid(user_id, "user id")
        cap = _validated_narrowing(max_applications, "max_applications")
        threshold = _validated_narrowing(match_threshold, "match_threshold", ceiling=MAX_SCORE)

        existing = await self.current(identifier)
        if existing is not None:
            logger.info(
                "run_session.already_running",
                session_id=str(existing.id),
                user_id=str(identifier),
                trigger=existing.trigger,
            )
            return existing

        run = RunSession(
            user_id=identifier,
            status=SessionStatus.RUNNING,
            started_at=utcnow(),
            trigger=(trigger or DEFAULT_SESSION_TRIGGER).strip() or DEFAULT_SESSION_TRIGGER,
            config_snapshot=dict(config_snapshot or {}),
            max_applications=cap,
            match_threshold=threshold,
        )
        self._session.add(run)
        await self._session.flush()
        await self._session.commit()

        logger.info(
            "run_session.started",
            session_id=str(run.id),
            user_id=str(identifier),
            trigger=run.trigger,
            max_applications=cap,
            match_threshold=threshold,
        )
        return run

    async def finish(
        self,
        session_id: uuid.UUID | str,
        status: SessionStatus = SessionStatus.COMPLETED,
        reason: StopReason | None = None,
    ) -> RunSession:
        """Close a run, stamping ``ended_at`` if it is not already stamped.

        Idempotent: re-finishing updates the status but preserves the original end time, so a
        watchdog sweep arriving after a clean shutdown cannot rewrite that run's timing. The
        stop reason is preserved for the same reason — the first explanation is the true one.

        Prefer :meth:`conclude` in new code: it derives the status from the reason, so the
        two can never disagree about the same run.

        Args:
            session_id: The run to close.
            status: How it ended. Defaults to ``completed``.
            reason: Why it ended. Ignored when the run already records one.

        Returns:
            The closed session, committed.

        Raises:
            LookupError: If *session_id* is malformed or names no run.
            ValueError: If *status* is not a valid
                :class:`~app.models.enums.SessionStatus`, or is ``running`` — finishing a run
                into "still running" is a caller bug, not a state.
        """
        run = await self.get(session_id)
        resolved = SessionStatus(status)
        if resolved is SessionStatus.RUNNING:
            raise ValueError("finish() requires a terminal status, not 'running'")

        run.finish(resolved, reason)
        await self._session.flush()
        await self._session.commit()

        logger.info(
            "run_session.finished",
            session_id=str(run.id),
            user_id=str(run.user_id),
            status=resolved.value,
            stop_reason=run.stop_reason.value if run.stop_reason is not None else None,
            applications_completed=run.applications_completed,
            failures=run.failures,
        )
        return run

    async def conclude(
        self,
        session_id: uuid.UUID | str,
        reason: StopReason,
    ) -> RunSession:
        """Close a run, deriving its status from *why* it stopped.

        One mapping, in one place, so a run can never be ``completed`` with a reason that
        means it broke. :data:`~app.models.enums.STOP_REASONS_COMPLETING` names the reasons
        that mean the run did everything asked of it; a user stop is ``cancelled``; anything
        else is ``failed``.

        Args:
            session_id: The run to close.
            reason: Why it stopped.

        Returns:
            The closed session, committed.

        Raises:
            LookupError: If *session_id* is malformed or names no run.
        """
        return await self.finish(session_id, status_for(reason), reason)

    async def request_stop(
        self,
        session_id: uuid.UUID | str,
        reason: StopReason = StopReason.USER_STOPPED,
    ) -> RunSession:
        """Ask a run to wind down, without closing it.

        This is the signal every long step polls through :meth:`halt_reason`. It is separate
        from :meth:`conclude` because work already in flight has to be able to *observe* the
        stop: flipping the status straight to ``cancelled`` would end the run on the
        dashboard while a browser was still filling a form, and the counters that browser
        went on to report would land on a run the user had been told was over.

        Idempotent, and the first reason wins — a user's stop is not relabelled by a limit
        the run happens to hit while winding down.

        Args:
            session_id: The run to stop.
            reason: Why it is stopping.

        Returns:
            The run, committed, with its stop signal set.

        Raises:
            LookupError: If *session_id* is malformed or names no run.
        """
        run = await self.get(session_id)
        if run.request_stop(reason):
            await self._session.flush()
            await self._session.commit()
            logger.info(
                "run_session.stop_requested",
                session_id=str(run.id),
                user_id=str(run.user_id),
                reason=reason.value,
            )
        return run

    async def halt_reason(self, session_id: uuid.UUID | str | None) -> StopReason | None:
        """Return why *session_id* may not start new work, or ``None`` if it may.

        The single predicate every caller about to begin an application consults. It answers
        for two conditions at once — a stop was asked for, or the run is already over —
        because a caller does not care which is true, only whether it is allowed to proceed.
        Asking those questions separately at each call site is how a stop ends up honoured
        on one path and ignored on another.

        Args:
            session_id: The run to check, or ``None`` for work that belongs to no run.
                Work outside a run is never halted by this, which is correct: there is no
                run whose stop it could be violating.

        Returns:
            The stop reason, or ``None`` when the run is live (or there is no run).
        """
        if session_id is None:
            return None
        try:
            run = await self.get(session_id)
        except LookupError:
            # A run pruned or never created. Refusing here would strand work that is
            # otherwise legitimate, and there is no stop being violated.
            return None
        if not run.is_halting:
            return None
        return run.stop_reason or StopReason.USER_STOPPED

    async def submitted_count(self, session_id: uuid.UUID | str) -> int:
        """Return how many applications this run has actually sent.

        Counted from the applications themselves rather than read off
        ``applications_completed``, because the cap this feeds is a safety limit and a
        rollup counter is a report. A counter increment lost to a crash between the
        submission and the ``UPDATE`` would silently raise the cap; a ``COUNT`` cannot.

        Args:
            session_id: The run to measure.

        Returns:
            Applications attributed to this run that have reached a post-submit state.

        Raises:
            LookupError: If *session_id* is malformed.
        """
        identifier = _as_uuid(session_id, "session id")
        total = await self._session.scalar(
            select(func.count(Application.id)).where(
                Application.session_id == identifier,
                Application.status.in_(_POST_SUBMIT_ORDERED),
            )
        )
        return int(total or 0)

    # ----------------------------------------------------------------------------------
    # Counters
    # ----------------------------------------------------------------------------------

    async def record(self, session_id: uuid.UUID | str, **deltas: int) -> RunSession:
        """Increment one or more of the run's counters, atomically.

        Compiles to ``SET <counter> = <counter> + :delta``, so three workers on three queues
        reporting progress on the same run cannot lose each other's counts the way a
        read-modify-write on a loaded instance would.

        When ``applications_completed`` is among the deltas, ``avg_application_seconds`` is
        **recomputed** by one aggregate over this run's applications rather than folded in
        from a caller-supplied sample. That makes the mean a function of the data instead of
        a function of who called first, and it stays correct across a crash. It therefore
        requires that the application's own ``duration_seconds`` has already been committed —
        record the duration, then record the completion.

        Args:
            session_id: The run to update.
            **deltas: Counter name to increment. Every name must be a member of
                :data:`~app.models.session.SESSION_COUNTER_FIELDS`.

        Returns:
            The refreshed session, committed.

        Raises:
            LookupError: If *session_id* is malformed or names no run.
            ValueError: If a keyword is not a known counter, or a delta is negative. A
                negative count is always a caller bug; refusing it here keeps the SQL-side
                increment and the model's :data:`~app.models.session.COUNTER_FLOOR` from
                needing a non-portable ``GREATEST``.
            TypeError: If a delta is not an integer.
        """
        identifier = _as_uuid(session_id, "session id")
        changes = _validated_deltas(deltas)
        if not changes:
            return await self.get(identifier)

        result = await self._session.execute(
            update(RunSession)
            .where(RunSession.id == identifier)
            .values(**{name: getattr(RunSession, name) + delta for name, delta in changes.items()})
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise LookupError(f"run session {identifier} not found")

        if "applications_completed" in changes:
            await self._session.execute(
                update(RunSession)
                .where(RunSession.id == identifier)
                .values(avg_application_seconds=await self._mean_duration(identifier))
                .execution_options(synchronize_session=False)
            )

        await self._session.commit()
        run = await self._reload(identifier)
        logger.debug(
            "run_session.recorded",
            session_id=str(identifier),
            **{name: delta for name, delta in changes.items()},
        )
        return run

    async def _mean_duration(self, session_id: uuid.UUID) -> float | None:
        """Return the mean wall-clock duration of this run's completed applications.

        Args:
            session_id: The run to measure.

        Returns:
            The mean in seconds, or ``None`` when no application in the run has recorded a
            duration yet — which is different from zero and must stay so.
        """
        mean = await self._session.scalar(
            select(func.avg(Application.duration_seconds)).where(
                Application.session_id == session_id,
                Application.duration_seconds.is_not(None),
            )
        )
        return float(mean) if mean is not None else None

    # ----------------------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------------------

    async def get(self, session_id: uuid.UUID | str) -> RunSession:
        """Load one run by id.

        Args:
            session_id: The run's identifier, as a UUID or its string form.

        Returns:
            The row.

        Raises:
            LookupError: If the identifier is malformed or names no run.
        """
        identifier = _as_uuid(session_id, "session id")
        run = await self._session.scalar(
            select(RunSession).where(RunSession.id == identifier).limit(1)
        )
        if run is None:
            raise LookupError(f"run session {identifier} not found")
        return run

    async def current(self, user_id: uuid.UUID | str) -> RunSession | None:
        """Return the user's running session, if there is one.

        Args:
            user_id: The user to look at.

        Returns:
            The running session, or ``None``. The newest is returned if several exist, which
            :meth:`start` makes impossible but a direct database edit does not.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        return await self._session.scalar(
            select(RunSession)
            .where(
                RunSession.user_id == identifier,
                RunSession.status == SessionStatus.RUNNING,
            )
            .order_by(RunSession.started_at.desc(), RunSession.id.desc())
            .limit(1)
        )

    async def stats(self, session_id: uuid.UUID | str) -> dict[str, int]:
        """Return the derived aggregates behind :attr:`~app.schemas.session.SessionDetail.stats`.

        The six rollup counters come off the row itself — that is what they are for — and
        everything else is two aggregate queries: applications grouped by status, and the
        run's outstanding checkpoints. Nothing here loops over rows.

        Args:
            session_id: The run to describe.

        Returns:
            A flat mapping: the six counters, ``applications`` (rows attributed to this run),
            ``submitted``, ``checkpoints_total``, ``checkpoints_pending``,
            ``duration_seconds`` (rounded, because the schema declares ``dict[str, int]``),
            and one ``status_<value>`` entry per application status present.

        Raises:
            LookupError: If *session_id* is malformed or names no run.
        """
        run = await self.get(session_id)
        identifier = run.id

        rows = (
            await self._session.execute(
                select(Application.status, func.count(Application.id))
                .where(Application.session_id == identifier)
                .group_by(Application.status)
            )
        ).all()

        by_status: dict[ApplicationStatus, int] = {
            ApplicationStatus(status): int(count or 0) for status, count in rows
        }

        checkpoint_rows = (
            await self._session.execute(
                select(Checkpoint.status, func.count(Checkpoint.id))
                .where(Checkpoint.session_id == identifier)
                .group_by(Checkpoint.status)
            )
        ).all()
        checkpoints: dict[CheckpointStatus, int] = {
            CheckpointStatus(status): int(count or 0) for status, count in checkpoint_rows
        }

        duration = run.duration_seconds
        stats: dict[str, int] = {
            "jobs_found": int(run.jobs_found or 0),
            "jobs_scored": int(run.jobs_scored or 0),
            "jobs_qualified": int(run.jobs_qualified or 0),
            "resumes_generated": int(run.resumes_generated or 0),
            "applications_started": int(run.applications_started or 0),
            "applications_completed": int(run.applications_completed or 0),
            "applications_skipped": int(run.applications_skipped or 0),
            "manual_review": int(run.manual_review or 0),
            "failures": int(run.failures or 0),
            "applications": sum(by_status.values()),
            "submitted": sum(by_status.get(status, 0) for status in _POST_SUBMIT_ORDERED),
            "checkpoints_total": sum(checkpoints.values()),
            "checkpoints_pending": sum(
                checkpoints.get(status, 0) for status in _OUTSTANDING_CHECKPOINT_STATES
            ),
            "duration_seconds": round(duration) if duration is not None else 0,
        }
        for status, count in by_status.items():
            stats[f"{STATS_STATUS_PREFIX}{status.value}"] = count
        return stats

    # ----------------------------------------------------------------------------------
    # Maintenance
    # ----------------------------------------------------------------------------------

    async def watchdog(self, stale_after_minutes: int = DEFAULT_STALE_AFTER_MINUTES) -> int:
        """Close running sessions that have stopped reporting progress.

        A worker killed mid-run leaves its session in ``running`` forever. That is not merely
        untidy: the one-running-session rule in :meth:`start` turns it into a deadlock, since
        every later run for that user is folded into a session nothing is driving. This sweep
        is what makes the rule safe, and it is what the ``session.watchdog`` beat task drives
        every five minutes.

        Staleness is measured on ``updated_at``, which every :meth:`record` bumps, so a busy
        run is never reaped and a silent one always is.

        Args:
            stale_after_minutes: Silence tolerated before a run is declared dead, clamped to
                ``[MIN_STALE_AFTER_MINUTES, MAX_STALE_AFTER_MINUTES]``.

        Returns:
            How many sessions were closed.
        """
        window = max(
            MIN_STALE_AFTER_MINUTES,
            min(int(stale_after_minutes or DEFAULT_STALE_AFTER_MINUTES), MAX_STALE_AFTER_MINUTES),
        )
        now = utcnow()
        cutoff = now - timedelta(minutes=window)

        stale = list(
            (
                await self._session.execute(
                    select(RunSession.id).where(
                        RunSession.status == SessionStatus.RUNNING,
                        RunSession.updated_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not stale:
            return 0

        await self._session.execute(
            update(RunSession)
            .where(RunSession.id.in_(stale))
            .values(
                status=SessionStatus.FAILED,
                ended_at=func.coalesce(RunSession.ended_at, now),
                # `coalesce`, not a plain assignment: a run that was already winding down
                # for a reason of its own — the user stopped it, it hit its cap — must keep
                # that reason. Only a run with no explanation at all is labelled stalled.
                stop_reason=func.coalesce(RunSession.stop_reason, StopReason.STALLED.value),
            )
            # `"fetch"`, unlike the `False` used by the hot-path counter increments: this
            # sweep runs every five minutes over a handful of rows, and its whole job is to
            # correct instances other code is still holding. `expire_on_commit` is off
            # project-wide, so without this a caller that had the session loaded would go on
            # reading `running` from a row this method just failed.
            .execution_options(synchronize_session="fetch")
        )
        await self._session.commit()

        logger.warning(
            "run_session.watchdog_reaped",
            reaped=len(stale),
            stale_after_minutes=window,
            session_ids=[str(identifier) for identifier in stale],
        )
        return len(stale)

    # ----------------------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------------------

    async def _reload(self, session_id: uuid.UUID) -> RunSession:
        """Re-read a run after a Core ``UPDATE`` bypassed the identity map.

        ``synchronize_session=False`` is what keeps the bulk increment portable and cheap; the
        price is that an instance already loaded in this session holds stale counters.
        ``populate_existing`` overwrites it rather than returning the stale copy.

        Args:
            session_id: The run to re-read.

        Returns:
            The refreshed row.

        Raises:
            LookupError: If the row vanished between the update and this read.
        """
        run = await self._session.scalar(
            select(RunSession)
            .where(RunSession.id == session_id)
            .execution_options(populate_existing=True)
            .limit(1)
        )
        if run is None:
            raise LookupError(f"run session {session_id} not found")
        return run


# ======================================================================================
# Helpers
# ======================================================================================


def _validated_narrowing(
    value: int | None,
    label: str,
    *,
    ceiling: int | None = None,
) -> int | None:
    """Check one per-run narrowing, letting ``None`` through untouched.

    ``None`` and zero are deliberately different: ``None`` means "no per-run narrowing, use
    the setting", zero means "apply to nothing". Coercing one into the other would turn a
    request that said nothing about a limit into one that forbids all work.

    Args:
        value: The requested narrowing, or ``None``.
        label: Field name, used in the error message.
        ceiling: Largest permitted value, when the field has one.

    Returns:
        The validated value, or ``None``.

    Raises:
        ValueError: If the value is negative, or above *ceiling*.
        TypeError: If the value is not an integer.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} requires an int, got {type(value).__name__!r}")
    if value < COUNTER_FLOOR:
        raise ValueError(f"{label}={value} is negative")
    if ceiling is not None and value > ceiling:
        raise ValueError(f"{label}={value} is above the maximum of {ceiling}")
    return value


def _validated_deltas(deltas: Mapping[str, Any]) -> dict[str, int]:
    """Check every counter name and increment, dropping the no-ops.

    Args:
        deltas: The caller's keyword arguments.

    Returns:
        The deltas worth writing, with zeros removed so a call that changes nothing does not
        emit an ``UPDATE``.

    Raises:
        ValueError: If a name is not a known counter, or a delta is negative.
        TypeError: If a delta is not an integer.
    """
    changes: dict[str, int] = {}
    for name, value in deltas.items():
        if name not in SESSION_COUNTER_FIELDS:
            raise ValueError(
                f"{name!r} is not a RunSession counter; "
                f"expected one of {sorted(SESSION_COUNTER_FIELDS)}"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"record({name}=...) requires an int, got {type(value).__name__!r}")
        if value < COUNTER_FLOOR:
            raise ValueError(f"record({name}={value}) is negative; counters only ever go up")
        if value:
            changes[name] = value
    return changes


def _as_uuid(value: uuid.UUID | str, label: str) -> uuid.UUID:
    """Coerce an identifier to a :class:`~uuid.UUID`.

    Args:
        value: The identifier, already a UUID or its string form.
        label: What the identifier names, used in the error message.

    Returns:
        The parsed UUID.

    Raises:
        LookupError: If the value is not a well-formed UUID. Malformed and missing are the
            same outcome for a caller — ``404`` — so they raise the same class.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise LookupError(f"{value!r} is not a valid {label}") from exc
