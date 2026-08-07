"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Revision identifiers in this project are sequential zero-padded integers, assigned
explicitly so the version directory reads in migration order:

    alembic revision --rev-id 0002 -m "${message}"

Write the ``downgrade`` body. An empty downgrade turns a bad deploy into a restore-from-
backup, which is the difference between a five-minute rollback and an outage. When an
operation genuinely cannot be reversed (a dropped column's data is gone), say so in a
comment and reverse the *structure* anyway.

SQLite cannot ``ALTER`` a column: wrap any column alteration in
``with op.batch_alter_table("<table>") as batch_op:`` so it runs on both backends.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
#: Identifier of this revision.
revision: str = ${repr(up_revision)}

#: Revision this one builds on; ``None`` only for the initial migration.
down_revision: str | None = ${repr(down_revision)}

#: Branch labels, unused: this project keeps one linear history.
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}

#: Dependencies on other version directories, unused.
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert this revision."""
    ${downgrades if downgrades else "pass"}
