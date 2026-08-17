"""session resume

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-17

Lets a run name the résumé variant it tailors from.

``docs/CONTRACTS.md`` §11 requires every application to record the résumé used, and the
product is meant to support several — a software variant, a robotics variant, an electrical
variant — with a run working through one of them. Neither was possible: nothing anywhere
resolved a variant for an apply, and ``Pipeline._resume_container`` picked strictly by
``is_default``, so a user with four variants had three that could never be applied with.

``ON DELETE SET NULL``: a run outlives the variant it used. Deleting a variant is a
deliberate act that takes its generation history with it, and it must not take the run's
counters, its applications or its stop reason as well. A run whose variant is gone reads as
one that named none, which is the honest degradation.

Nullable, with no back-fill. Existing runs did not choose a variant — the column did not
exist — and NULL is exactly that statement.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa

from alembic import op
from app.database.types import GUID

#: Identifier of this revision.
revision: str = "0006"

#: Revision this one builds on.
down_revision: str | None = "0005"

#: Branch labels, unused: this project keeps one linear history.
branch_labels: str | Sequence[str] | None = None

#: Dependencies on other version directories, unused.
depends_on: str | Sequence[str] | None = None

#: The table this revision touches.
TABLE: Final[str] = "run_sessions"

#: The column it adds.
COLUMN: Final[str] = "resume_id"

#: Foreign key name, following the convention in ``0001_initial_schema.py``.
FK_NAME: Final[str] = "fk_run_sessions_resume_id_resumes"

#: Supporting index name.
INDEX_NAME: Final[str] = "ix_run_sessions_resume_id"


def upgrade() -> None:
    """Add the run's résumé reference."""
    op.add_column(TABLE, sa.Column(COLUMN, GUID(), nullable=True))
    op.create_index(INDEX_NAME, TABLE, [COLUMN], unique=False)
    with op.batch_alter_table(TABLE) as batch:
        batch.create_foreign_key(FK_NAME, "resumes", [COLUMN], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    """Drop it. Runs revert to the default-variant selection they had before."""
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(FK_NAME, type_="foreignkey")
    op.drop_index(INDEX_NAME, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
