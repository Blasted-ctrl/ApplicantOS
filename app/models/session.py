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
from sqlalchemy import ColumnElement, Enum as SAEnum, Float, Integer, String, func
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement

from app.database.base import Base
from app.database.types import utcnow
from app.models.enums import SessionStatus
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.checkpoint import Checkpoint
    from app.models.user import User

__all__ = [
    "DEFAULT_SESSION_TRIGGER",
    "SESSION_COUNTER_FIELDS",
    "SESSION_STATUS_COLUMN",
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

#: Every column :meth:`RunSession.record` is allowed to increment. Anything else is a
#: caller bug and raises rather than being silently absorbed as an instance attribute.
SESSION_COUNTER_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "applications_completed",
        "failures",
        "jobs_found",
        "jobs_qualified",
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

    # -- rollup counters ----------------------------------------------------------------
    jobs_found: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Postings discovered during this run, before deduplication.",
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
    applications_completed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Applications that reached a submitted state. Also the duration sample count.",
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
                    f"RunSession.record({field}=...) requires an int, "
                    f"got {type(delta).__name__!r}"
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

    def finish(self, status: SessionStatus = SessionStatus.COMPLETED) -> None:
        """Close the run, stamping :attr:`ended_at` if it is not already stamped.

        Idempotent: re-finishing a session updates the status but preserves the original
        end time, so a watchdog sweep cannot rewrite a clean shutdown's timing.

        Args:
            status: Terminal status to record. Defaults to ``completed``.
        """
        self.status = status
        if self.ended_at is None:
            self.ended_at = utcnow()

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"RunSession(id={self.id!r}, user_id={self.user_id!r}, "
            f"status={self.status!r}, trigger={self.trigger!r}, "
            f"applications_completed={self.applications_completed!r})"
        )
