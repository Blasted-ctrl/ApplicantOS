"""session run loop

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-16

Gives ``run_sessions`` the four columns a run needs to be able to *stop*, and the three
counters the live session panel needs to be able to describe itself.

Before this revision a run had exactly two ways to end: the user pressed Stop, which wrote
``cancelled``, or the watchdog reaped it after fifteen minutes of silence, which wrote
``failed``. Nothing ever wrote ``completed``, because nothing could tell the difference
between a run that had finished its work and a run that had died — there was no column in
which that difference could be recorded. Every unattended run in the product's history
therefore ended as a failure.

**``stop_reason`` and ``stop_requested_at`` are two columns, not one.** ``stop_requested_at``
is the cooperative signal a step polls before starting new work; ``stop_reason`` is the
explanation the desktop app renders. Collapsing them would mean either closing a run while a
browser was still filling a form, or having no way to say why a closed run closed.

**``max_applications`` and ``match_threshold`` are nullable, and NULL is not zero.** NULL
means "no per-run narrowing, use the configured setting". Zero would mean "apply to nothing",
which is a legitimate and different request.

**No column is back-filled.** Rows written before this revision ran under a build that had
no stop reason to record, and inventing one for them would be fabricating history. They read
back as NULL, which is what "we do not know" looks like.

Hand-written in the style of ``0001_initial_schema.py`` and ``0002_status_sync.py``: every
width, default and nullability below is a decision rather than whatever a diff emitted. The
enum column is ``VARCHAR``, not a native type, matching ``sa.Enum(native_enum=False)`` in
:mod:`app.models.session`, and its width is the longest member *value* — which is why that
width is a named constant here. Adding a longer :class:`~app.models.enums.StopReason` member
is a schema change and needs its own migration, rather than silently truncating on
PostgreSQL.

The counters carry ``server_default="0"`` because they are ``NOT NULL`` additions to a table
that already has rows; without it the ``ALTER`` cannot succeed on either backend.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa

from alembic import op
from app.database.types import UTCDateTime

#: Identifier of this revision.
revision: str = "0003"

#: Revision this one builds on.
down_revision: str | None = "0002"

#: Branch labels, unused: this project keeps one linear history.
branch_labels: str | Sequence[str] | None = None

#: Dependencies on other version directories, unused.
depends_on: str | Sequence[str] | None = None


# ======================================================================================
# Column widths
# ======================================================================================

#: ``StopReason`` — longest value ``"infrastructure_failure"``.
ENUM_STOP_REASON: Final[int] = 22

#: The table every column below belongs to.
TABLE: Final[str] = "run_sessions"

#: Counters added by this revision, all ``NOT NULL DEFAULT 0``.
COUNTERS: Final[tuple[str, ...]] = (
    "jobs_scored",
    "applications_started",
    "applications_skipped",
)


def upgrade() -> None:
    """Add the stop signal, the per-run narrowings, and the three counters."""
    op.add_column(
        TABLE,
        sa.Column(
            "stop_reason",
            sa.Enum(
                "user_stopped",
                "limit_reached",
                "daily_limit_reached",
                "no_eligible_jobs",
                "submission_disabled",
                "stalled",
                "infrastructure_failure",
                name="stop_reason",
                native_enum=False,
                length=ENUM_STOP_REASON,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        TABLE,
        sa.Column("stop_requested_at", UTCDateTime(), nullable=True),
    )
    op.create_index(
        op.f("ix_run_sessions_stop_requested_at"),
        TABLE,
        ["stop_requested_at"],
        unique=False,
    )
    op.add_column(TABLE, sa.Column("max_applications", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("match_threshold", sa.Integer(), nullable=True))

    for counter in COUNTERS:
        op.add_column(
            TABLE,
            sa.Column(counter, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    """Drop everything :func:`upgrade` added, newest first."""
    for counter in reversed(COUNTERS):
        op.drop_column(TABLE, counter)

    op.drop_column(TABLE, "match_threshold")
    op.drop_column(TABLE, "max_applications")
    op.drop_index(op.f("ix_run_sessions_stop_requested_at"), table_name=TABLE)
    op.drop_column(TABLE, "stop_requested_at")
    op.drop_column(TABLE, "stop_reason")
