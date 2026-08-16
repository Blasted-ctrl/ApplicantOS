"""Run sessions — one automation run, and the counters that describe it.

A :class:`RunSession` is what the desktop app's live dashboard is bound to and what the
watchdog reaps after a crash. It is deliberately a *rollup*: the individual applications
carry the detail, while the session carries the six counters that answer "how is this run
going?" in a single row, so the dashboard never has to aggregate over a growing table to
paint a progress bar.

Counters are mutated only through :meth:`RunSession.record`, which validates the field name
against :data:`SESSION_COUNTER_FIELDS`. A typo'd keyword in a caller would otherwise create
an attribute on the instance and silently lose the count.

:attr:`RunSession.duration_seconds` is a hybrid: in Python it measures against *now* while
the session is still running, and in SQL it compiles to a portable elapsed-seconds
expression (``EXTRACT(EPOCH FROM ...)`` on PostgreSQL, ``julianday`` arithmetic on SQLite),
so ``ORDER BY duration_seconds`` and ``WHERE duration_seconds > n`` work on both backends
without the application loading rows to sort them.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import ColumnElement, Float, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement

from app.database.base import Base
from app.database.types import utcnow
from app.models.enums import (
    STOP_REASON_SENTENCES,
    SessionStatus,
    StopReason,
)
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.checkpoint import Checkpoint
    from app.models.user import User

__all__ = [
    "DEFAULT_SESSION_TRIGGER",
    "SESSION_COUNTER_FIELDS",
    "SESSION_STATUS_COLUMN",
    "STOP_REASON_COLUMN",
    "RunSession",
]

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------------------
# Column types, sizes and vocabulary
# --------------------------------------------------------------------------------------

# Enum columns persist the lowercase string *value*, never the Python member name.
_ENUM_VALUES: Final = operator.methodcaller("values")

#: Storage type for :class:`~app.models.enums.SessionStatus`.
SESSION_STATUS_COLUMN: Final[SAEnum] = SAEnum(
    SessionStatus,
    name="session_status",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Storage type for :class:`~app.models.enums.StopReason`.
STOP_REASON_COLUMN: Final[SAEnum] = SAEnum(
    StopReason,
    name="stop_reason",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Every column :meth:`RunSession.record` is allowed to increment. Anything else is a
#: caller bug and raises rather than being silently absorbed as an instance attribute.
SESSION_COUNTER_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "applications_completed",
        "applications_skipped",
        "applications_started",
        "failures",
        "jobs_found",
        "jobs_qualified",
        "jobs_scored",
        "manual_review",
        "resumes_generated",
    }
)

#: Width of the trigger label ("manual", "schedule", "api", ...).
SESSION_TRIGGER_MAX_LENGTH: Final[int] = 32

#: Trigger recorded when the caller does not name one.
DEFAULT_SESSION_TRIGGER: Final[str] = "manual"

#: Counters never go below this. A negative delta that would underflow is clamped and
#: logged rather than persisted, because a negative "jobs found" is always a bug.
COUNTER_FLOOR: Final[int] = 0

#: Seconds in a day, used to convert SQLite's Julian day difference to seconds.
SECONDS_PER_DAY: Final[int] = 86_400


# --------------------------------------------------------------------------------------
# Portable elapsed-time SQL expression
# --------------------------------------------------------------------------------------


class _ElapsedSeconds(FunctionElement[float]):
    """SQL expression yielding the seconds between two timestamp expressions.

    Timestamp subtraction has no portable spelling: PostgreSQL produces an ``interval``
    that must be converted with ``EXTRACT(EPOCH FROM ...)``, while SQLite has no interval
    type at all and needs ``julianday`` arithmetic. Rather than push that difference into
    every query, it is compiled per dialect exactly once here.

    Construct with ``_ElapsedSeconds(start, end)``.
    """

    name = "elapsed_seconds"
    type = Float()
    inherit_cache = True


@compiles(_ElapsedSeconds)
def _compile_elapsed_seconds(
    element: _ElapsedSeconds,
    compiler: SQLCompiler,
    **kwargs: Any,
) -> str:
    """Compile :class:`_ElapsedSeconds` for PostgreSQL and any other ANSI-ish backend."""
    start, end = tuple(element.clauses)
    return (
        f"EXTRACT(EPOCH FROM ({compiler.process(end, **kwargs)} "
        f"- {compiler.process(start, **kwargs)}))"
    )


@compiles(_ElapsedSeconds, "sqlite")
def _compile_elapsed_seconds_sqlite(
    element: _ElapsedSeconds,
    compiler: SQLCompiler,
    **kwargs: Any,
) -> str:
    """Compile :class:`_ElapsedSeconds` for SQLite, which has no interval arithmetic."""
    start, end = tuple(element.clauses)
    return (
        f"((julianday({compiler.process(end, **kwargs)}) "
        f"- julianday({compiler.process(start, **kwargs)})) * {SECONDS_PER_DAY})"
    )


class RunSession(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """One automation run: its status, its timing, and its rollup counters.

    Note:
        :attr:`token_usage` and :attr:`config_snapshot` are plain JSON columns and are not
        change tracked. Use :meth:`add_token_usage`, which reassigns, rather than mutating
        the dictionary in place.
    """

    __tablename__ = "run_sessions"

    status: Mapped[SessionStatus] = mapped_column(
        SESSION_STATUS_COLUMN,
        default=SessionStatus.RUNNING,
        nullable=False,
        index=True,
        doc="Whether the run is executing, and if not, how it ended.",
    )
    started_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        nullable=False,
        index=True,
        doc="When the run began. Never NULL — a session exists because it started.",
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        doc="When the run stopped. NULL while running; the watchdog reaps stale NULLs.",
    )

    # -- stopping -----------------------------------------------------------------------
    # `status` says how a run ended. `stop_reason` says why, and why is the only half a user
    # can act on: "reached your limit of 50" and "ran out of postings" are both `completed`.
    stop_reason: Mapped[StopReason | None] = mapped_column(
        STOP_REASON_COLUMN,
        nullable=True,
        doc="Why the run stopped. NULL while running, and on rows written before 0003.",
    )
    stop_requested_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        index=True,
        doc=(
            "When a stop was asked for. This is the *cooperative* signal every long step "
            "polls; `status` is only flipped once the run has actually wound down."
        ),
    )

    # -- what the user asked for ---------------------------------------------------------
    # Both are per-run narrowings of a standing setting, and both may only ever narrow:
    # `max_applications` is intersected with `max_applications_per_session` and the daily
    # cap, `match_threshold` is raised to at least `auto_apply_min_score`. A request cannot
    # widen what a run is permitted to do — see `RunSession.application_cap`.
    max_applications: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="Cap for this run alone. NULL means the configured session cap governs.",
    )
    match_threshold: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="Minimum normalised score (0-100) for this run. NULL means use the setting.",
    )

    # -- rollup counters ----------------------------------------------------------------
    jobs_found: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Postings discovered during this run, before deduplication.",
    )
    jobs_scored: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Postings this run put a score on, whether or not they qualified.",
    )
    jobs_qualified: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Postings that scored at or above the configured threshold.",
    )
    resumes_generated: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Tailored resume versions produced during this run.",
    )
    applications_started: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Applications this run opened a browser for. Started minus completed is the loss.",
    )
    applications_completed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Applications that reached a submitted state. Also the duration sample count.",
    )
    applications_skipped: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Postings passed over: already applied to, below the floor, or provider-refused.",
    )
    manual_review: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Applications routed to a human rather than guessed at (golden rule #2).",
    )
    failures: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Applications that failed outright during this run.",
    )
    avg_application_seconds: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        doc="Running mean of completed application durations; NULL until the first sample.",
    )

    # -- provenance ---------------------------------------------------------------------
    token_usage: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Cumulative LLM token counts for the run, keyed by model or usage kind.",
    )
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Effective settings and preferences at start, so a run stays reproducible.",
    )
    trigger: Mapped[str] = mapped_column(
        String(SESSION_TRIGGER_MAX_LENGTH),
        default=DEFAULT_SESSION_TRIGGER,
        server_default=DEFAULT_SESSION_TRIGGER,
        nullable=False,
        doc="What started this run: 'manual', 'schedule', 'api', ...",
    )

    # -- relationships -------------------------------------------------------------------
    # Both children reference the session optionally (ON DELETE SET NULL). Pruning old
    # sessions is routine maintenance and must never destroy applications or the
    # checkpoint state a crash recovery might still need.
    applications: Mapped[list[Application]] = relationship(
        "Application",
        back_populates="session",
        passive_deletes=True,
    )
    checkpoints: Mapped[list[Checkpoint]] = relationship(
        "Checkpoint",
        back_populates="session",
        passive_deletes=True,
    )
    user: Mapped[User] = relationship("User", back_populates="sessions")

    # -- behaviour -------------------------------------------------------------------------

    @hybrid_property
    def duration_seconds(self) -> float | None:
        """Elapsed seconds for this run, measured against *now* while it is still running.

        Returns:
            Seconds between :attr:`started_at` and :attr:`ended_at`, or between
            :attr:`started_at` and the current instant when the run has not ended. ``None``
            on an unflushed row whose ``started_at`` default has not been applied.
        """
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None else utcnow()
        return (end - self.started_at).total_seconds()

    @duration_seconds.inplace.expression
    @classmethod
    def _duration_seconds_expression(cls) -> ColumnElement[float]:
        """SQL form of :attr:`duration_seconds`, portable across PostgreSQL and SQLite."""
        return _ElapsedSeconds(cls.started_at, func.coalesce(cls.ended_at, func.now()))

    @property
    def is_running(self) -> bool:
        """Whether this run is still executing."""
        return self.status is SessionStatus.RUNNING

    @property
    def is_halting(self) -> bool:
        """Whether no *new* work may start under this run.

        True once the run has stopped **or** once a stop has been asked for and not yet
        acted on. The two are deliberately one predicate: a caller about to open a browser
        does not care which of them is true, only whether it is allowed to proceed. Keeping
        them separate at the call site is how a stop ends up honoured on one path and
        ignored on another.
        """
        return not self.is_running or self.stop_requested_at is not None

    def application_cap(self, *, configured_cap: int, daily_remaining: int) -> int:
        """Return how many more applications this run may submit.

        Three limits are in play and the smallest always wins, because every one of them is
        a *narrowing*. The request's ``max_applications`` cannot raise the configured session
        cap, and neither can raise what the user has left in the day. A cap that could be
        widened from the request body would make ``max_applications_per_session`` advisory,
        which is not what a setting called a maximum means.

        Args:
            configured_cap: ``settings.max_applications_per_session``.
            daily_remaining: ``max_applications_per_day`` minus what the user has already
                submitted today, across every run.

        Returns:
            The number of further submissions permitted, never negative.
        """
        limits = [int(configured_cap), int(daily_remaining)]
        if self.max_applications is not None:
            limits.append(int(self.max_applications))
        return max(COUNTER_FLOOR, min(limits))

    def score_floor(self, *, configured_floor: int) -> int:
        """Return the normalised score a posting must reach for this run to apply.

        Narrowing only, for the same reason as :meth:`application_cap`: a run may ask to be
        *pickier* than the standing setting, never less picky. A request that could lower
        the floor would be a way to route around ``auto_apply_min_score`` by starting a run.

        Args:
            configured_floor: ``settings.auto_apply_min_score``.

        Returns:
            The effective floor for this run.
        """
        if self.match_threshold is None:
            return int(configured_floor)
        return max(int(configured_floor), int(self.match_threshold))

    def request_stop(self, reason: StopReason) -> bool:
        """Ask this run to wind down, without closing it.

        Separating the request from the close is what makes a stop *observable* by work that
        is already in flight. Flipping ``status`` straight to ``cancelled`` would end the run
        on the dashboard while a browser was still filling a form, and the counters that
        browser went on to report would land on a session the user had been told was over.

        Idempotent: the first reason wins, so a user stop is not overwritten by a limit the
        run happens to hit while winding down.

        Args:
            reason: Why the run is stopping.

        Returns:
            ``True`` if this call set the signal, ``False`` if one was already set.
        """
        if self.stop_requested_at is not None:
            return False
        self.stop_requested_at = utcnow()
        self.stop_reason = StopReason(reason)
        return True

    @property
    def stop_sentence(self) -> str | None:
        """The stop reason as a sentence, with its number filled in.

        Returns:
            One sentence naming why the run stopped, or ``None`` while it is still running
            and no stop has been asked for. Never the word "Done."
        """
        if self.stop_reason is None:
            return None
        template = STOP_REASON_SENTENCES.get(self.stop_reason)
        if template is None:  # pragma: no cover - guarded by test_stop_reasons_all_have_copy
            return f"Stopped: {self.stop_reason.value.replace('_', ' ')}."
        limit = self.max_applications if self.max_applications is not None else ""
        return template.format(limit=limit)

    def record(self, **deltas: int) -> None:
        """Increment one or more rollup counters.

        Every keyword must name a member of :data:`SESSION_COUNTER_FIELDS`. Unset counters
        (an unflushed row where the column default has not been applied) are treated as
        zero, and a delta that would drive a counter below :data:`COUNTER_FLOOR` is clamped
        and logged — a negative count is always a bug in the caller, never data.

        Args:
            **deltas: Counter name to signed increment, for example
                ``session.record(jobs_found=12, jobs_qualified=3)``.

        Raises:
            ValueError: If a keyword does not name a known counter. Failing loudly is
                deliberate: a silent no-op would corrupt the dashboard invisibly.
            TypeError: If a delta is not an integer.
        """
        for field, delta in deltas.items():
            if field not in SESSION_COUNTER_FIELDS:
                raise ValueError(
                    f"{field!r} is not a RunSession counter; "
                    f"expected one of {sorted(SESSION_COUNTER_FIELDS)}"
                )
            if isinstance(delta, bool) or not isinstance(delta, int):
                raise TypeError(
                    f"RunSession.record({field}=...) requires an int, got {type(delta).__name__!r}"
                )
            current = getattr(self, field)
            updated = (0 if current is None else int(current)) + delta
            if updated < COUNTER_FLOOR:
                logger.warning(
                    "run_session.counter_underflow",
                    field=field,
                    delta=delta,
                    current=current,
                    clamped_to=COUNTER_FLOOR,
                )
                updated = COUNTER_FLOOR
            setattr(self, field, updated)

    def observe_application_duration(self, seconds: float) -> None:
        """Fold one completed application's duration into the running mean.

        :attr:`applications_completed` is the sample count, so this must be called *after*
        ``record(applications_completed=1)`` for the same application. Keeping a running
        mean rather than a sum avoids a second column and stays correct across a crash,
        because both operands are persisted.

        Args:
            seconds: Wall-clock duration of the application that just completed.
        """
        samples = max(self.applications_completed or 0, 1)
        previous = self.avg_application_seconds or 0.0
        self.avg_application_seconds = ((previous * (samples - 1)) + float(seconds)) / samples

    def add_token_usage(self, **counts: int) -> None:
        """Accumulate LLM token counts into :attr:`token_usage`.

        Reassigns the whole dictionary rather than mutating it, because JSON columns are
        not change tracked and an in-place update would not be flushed.

        Args:
            **counts: Usage bucket to token count, for example
                ``add_token_usage(input_tokens=812, output_tokens=1_204)``.
        """
        merged = dict(self.token_usage or {})
        for bucket, count in counts.items():
            merged[bucket] = int(merged.get(bucket, 0)) + int(count)
        self.token_usage = merged

    def finish(
        self,
        status: SessionStatus = SessionStatus.COMPLETED,
        reason: StopReason | None = None,
    ) -> None:
        """Close the run, stamping :attr:`ended_at` if it is not already stamped.

        Idempotent: re-finishing a session updates the status but preserves the original
        end time, so a watchdog sweep cannot rewrite a clean shutdown's timing. The stop
        reason follows the same rule for the same reason — the first explanation of why a
        run ended is the true one, and a later sweep must not relabel a run the user
        cancelled as one that stalled.

        Args:
            status: Terminal status to record. Defaults to ``completed``.
            reason: Why it stopped. Ignored when a reason is already recorded.
        """
        self.status = status
        if self.ended_at is None:
            self.ended_at = utcnow()
        if reason is not None and self.stop_reason is None:
            self.stop_reason = StopReason(reason)

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"RunSession(id={self.id!r}, user_id={self.user_id!r}, "
            f"status={self.status!r}, trigger={self.trigger!r}, "
            f"applications_completed={self.applications_completed!r})"
        )
