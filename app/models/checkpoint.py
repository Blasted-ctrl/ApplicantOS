"""Checkpoints — the rows that make golden rule #8 ("everything is resumable") true.

Long operations in ApplicantOS are not atomic: discovering, scoring, tailoring a resume,
driving a browser and verifying a submission span minutes, several processes and at least
one external website. A crash halfway through must resume, not restart — restarting risks
re-submitting, which violates golden rule #1.

A :class:`Checkpoint` is one step of one such operation, addressed by a caller-chosen
:attr:`Checkpoint.key` that is **unique**. That uniqueness is the whole mechanism: two
workers racing to start the same step collide on the index, and the loser reads the
winner's row instead of duplicating the work. ``CheckpointService.save()`` is an upsert on
that key.

:attr:`Checkpoint.owner` groups steps belonging to the same operation (``"apply:<uuid>"``,
``"index:<source-id>"``) so ``resume_all(owner)`` can re-drive an interrupted run, which is
what the composite ``(owner, status)`` index serves.
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import GUID, utcnow
from app.models.enums import CheckpointStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.session import RunSession

__all__ = ["CHECKPOINT_STATUS_COLUMN", "Checkpoint"]

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------------------
# Column types and sizes
# --------------------------------------------------------------------------------------

# Enum columns persist the lowercase string *value*, never the Python member name.
_ENUM_VALUES: Final = operator.methodcaller("values")

#: Storage type for :class:`~app.models.enums.CheckpointStatus`.
CHECKPOINT_STATUS_COLUMN: Final[SAEnum] = SAEnum(
    CheckpointStatus,
    name="checkpoint_status",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Width of the idempotency key. Keys are structured strings such as
#: ``"apply:<application-uuid>:submit"``, not digests, so they need room.
CHECKPOINT_KEY_MAX_LENGTH: Final[int] = 255

#: Width of the owner group label.
CHECKPOINT_OWNER_MAX_LENGTH: Final[int] = 128

#: Width of the step name within an owner.
CHECKPOINT_STEP_MAX_LENGTH: Final[int] = 128

#: Attempt counter for a checkpoint that has never been driven.
INITIAL_ATTEMPT: Final[int] = 0


class Checkpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One resumable step of a long-running operation.

    Note:
        :attr:`state` is a plain JSON column and is not change tracked. Reassign it when
        saving progress — that is also what ``CheckpointService.save()`` does.
    """

    __tablename__ = "checkpoints"
    __table_args__ = (
        # Crash recovery scans one operation's steps by status: "what is still pending?"
        Index("ix_checkpoints_owner_status", "owner", "status"),
    )

    session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("run_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "Run this checkpoint belongs to, when there is one. Optional: maintenance "
            "pruning sessions must not delete state a recovery pass still needs."
        ),
    )
    key: Mapped[str] = mapped_column(
        String(CHECKPOINT_KEY_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
        doc="Caller-chosen idempotency key. Uniqueness is what serialises racing workers.",
    )
    owner: Mapped[str] = mapped_column(
        String(CHECKPOINT_OWNER_MAX_LENGTH),
        nullable=False,
        doc="Group label tying this step to the operation it belongs to.",
    )
    step: Mapped[str] = mapped_column(
        String(CHECKPOINT_STEP_MAX_LENGTH),
        nullable=False,
        doc="Name of the step within the owning operation.",
    )
    status: Mapped[CheckpointStatus] = mapped_column(
        CHECKPOINT_STATUS_COLUMN,
        default=CheckpointStatus.PENDING,
        nullable=False,
        doc="Step state; 'compensated' distinguishes a deliberate rollback from a failure.",
    )
    state: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Everything the step needs to continue from where it stopped.",
    )
    attempt: Mapped[int] = mapped_column(
        Integer,
        default=INITIAL_ATTEMPT,
        server_default="0",
        nullable=False,
        doc="How many times this step has been driven; bounds the retry policy.",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Message from the most recent failure of this step.",
    )
    resumable: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
        doc=(
            "Whether recovery may re-drive this step. False for terminal outcomes such as "
            "NEEDS_REVIEW and policy blocks, which must never be retried automatically."
        ),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        index=True,
        doc="When this state stops being trustworthy; maintenance prunes past it.",
    )

    # -- relationships ---------------------------------------------------------------
    session: Mapped[RunSession | None] = relationship(
        "RunSession",
        back_populates="checkpoints",
    )

    # -- behaviour --------------------------------------------------------------------

    def is_expired(self, now: datetime | None = None) -> bool:
        """Whether this checkpoint's saved state has aged out.

        Args:
            now: Instant to compare against. Defaults to the current UTC time; passing it
                explicitly makes sweep runs deterministic and testable.

        Returns:
            ``False`` when :attr:`expires_at` is unset — no expiry means never expires.
        """
        if self.expires_at is None:
            return False
        return self.expires_at <= (now if now is not None else utcnow())

    def can_resume(self) -> bool:
        """Whether a recovery pass should re-drive this step.

        Combines all three conditions that must hold: the step is flagged resumable, its
        status is one recovery acts on, and its saved state has not expired.
        """
        if not self.resumable or self.is_expired():
            return False
        status = self.status if self.status is not None else CheckpointStatus.PENDING
        return status.is_resumable()

    def mark_running(self) -> None:
        """Claim the step for execution, incrementing :attr:`attempt`."""
        self.status = CheckpointStatus.RUNNING
        self.attempt = (self.attempt or INITIAL_ATTEMPT) + 1

    def mark_succeeded(self, state: dict[str, Any] | None = None) -> None:
        """Record a successful step, optionally replacing the saved state.

        Args:
            state: Final state to persist. Copied defensively; omitted leaves the existing
                state untouched.
        """
        self.status = CheckpointStatus.SUCCEEDED
        self.last_error = None
        if state is not None:
            self.state = dict(state)

    def mark_failed(self, error: str) -> None:
        """Record a failed attempt and retain the error for the next recovery pass.

        Args:
            error: Human-readable failure description.
        """
        self.status = CheckpointStatus.FAILED
        self.last_error = error
        logger.warning(
            "checkpoint.failed",
            checkpoint_key=self.key,
            owner=self.owner,
            step=self.step,
            attempt=self.attempt,
            error=error,
        )

    def mark_compensated(self) -> None:
        """Record that a previously successful step was deliberately rolled back."""
        self.status = CheckpointStatus.COMPENSATED
        self.resumable = False

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"Checkpoint(id={self.id!r}, key={self.key!r}, owner={self.owner!r}, "
            f"step={self.step!r}, status={self.status!r}, attempt={self.attempt!r})"
        )
