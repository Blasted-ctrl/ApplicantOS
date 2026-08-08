"""Persisted log entries — the queryable tail of the structlog stream.

The structlog pipeline in :mod:`app.config.logging` writes to stdout, which is right for
operators and useless for the desktop app: a user debugging *why did this application stop?*
cannot grep a container's stdout. ``log_entries`` is the subset of that stream the product
itself needs to display and filter, backing ``GET /api/v1/logs`` and the ``log.entry``
WebSocket event.

**These columns are deliberately not foreign keys.** ``session_id``, ``application_id`` and
``posting_id`` are correlation identifiers copied from the structlog context, not
references, for three reasons that all point the same way:

1. A log entry must be writable for an entity that was never committed — the row that a
   failed transaction rolled back is exactly the one worth logging about.
2. Cascading deletes would silently destroy the audit trail of whatever was deleted, which
   inverts the purpose of keeping it.
3. This is the highest-write table in the schema. Three referential checks per insert is a
   cost paid on every log line for a guarantee nothing here needs.

Retention is a maintenance concern, not a modelling one: entries are pruned by age.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import GUID, utcnow
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["LogEntry"]


# --------------------------------------------------------------------------------------
# Column sizes
# --------------------------------------------------------------------------------------

#: Width of a log level name (``INFO``, ``WARNING``, ``CRITICAL``). Stored as the plain
#: string structlog emits rather than an enum: levels come from the standard library and
#: third-party libraries alike, and ``docs/CONTRACTS.md`` §3 declares no level enum.
LOG_LEVEL_MAX_LENGTH: Final[int] = 16

#: Width of the dotted event name (``application.submitted``, ``jobs.poll_finished``).
LOG_EVENT_MAX_LENGTH: Final[int] = 255

#: Width of the emitting logger's name, which is the producing module's ``__name__``.
LOGGER_NAME_MAX_LENGTH: Final[int] = 255

#: Width of a correlation identifier. Generous enough for a hyphenated UUID plus a prefix.
CORRELATION_ID_MAX_LENGTH: Final[int] = 64


class LogEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One structured log event, retained for the product's own log viewer.

    Note:
        :attr:`payload` holds the event's remaining key/value context and has already been
        through :func:`app.config.logging.redact_secrets` before it reaches this table —
        golden rule #4 applies to the database sink exactly as it does to stdout. It is a
        plain JSON column and is not change tracked, which is correct: log entries are
        immutable.
    """

    __tablename__ = "log_entries"
    __table_args__ = (
        # The application detail view's timeline: one application's events, in order.
        Index("ix_log_entries_application_id_at", "application_id", "at"),
    )

    level: Mapped[str] = mapped_column(
        String(LOG_LEVEL_MAX_LENGTH),
        nullable=False,
        index=True,
        doc="Log level name as emitted, e.g. 'info', 'warning', 'error'.",
    )
    event: Mapped[str] = mapped_column(
        String(LOG_EVENT_MAX_LENGTH),
        nullable=False,
        doc="Short dotted event name — the stable identifier of what happened.",
    )
    logger: Mapped[str | None] = mapped_column(
        String(LOGGER_NAME_MAX_LENGTH),
        nullable=True,
        doc="Name of the emitting logger, conventionally the producing module.",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(CORRELATION_ID_MAX_LENGTH),
        nullable=True,
        index=True,
        doc="Request/task correlation identifier bound via app.config.logging.bind_context.",
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        nullable=True,
        doc="RunSession this line belongs to. A correlation identifier, not a foreign key.",
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        nullable=True,
        doc="Application this line belongs to. A correlation identifier, not a foreign key.",
    )
    posting_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        nullable=True,
        doc="JobPosting this line belongs to. A correlation identifier, not a foreign key.",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Remaining structured context for the event, already secret-redacted.",
    )
    at: Mapped[datetime] = mapped_column(
        default=utcnow,
        nullable=False,
        index=True,
        doc="When the event was emitted, which may precede when the row was written.",
    )

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"LogEntry(id={self.id!r}, level={self.level!r}, event={self.event!r}, at={self.at!r})"
        )
