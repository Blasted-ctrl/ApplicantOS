"""submitted resume file

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-16

Records *which résumé was actually uploaded* when the applicant's own document was sent.

``settings.resume_source = "master"`` submits the résumé the user uploaded rather than the
generated view, and it exists for a good reason: generation quality and submission capability
are independent problems, and a user whose knowledge graph is still being cleaned up should
still be able to apply today with the document they already trust.

The recording was wrong, though. ``Pipeline._materialize_documents`` returned the uploaded
file's path and returned early, while ``applications.resume_version_id`` went on pointing at a
generated ``ResumeVersion`` that no employer ever received — so the application detail screen
named, offered for download, and attributed to the submission a document that was not sent.
``docs/CONTRACTS.md`` §11 requires the résumé *used*.

``submitted_resume_file_id`` names the upload when one was sent; NULL keeps the existing
meaning, that ``resume_version_id`` describes what went out.

``ON DELETE SET NULL``, matching ``confirmation_screenshot_id``: the retention sweep prunes
old uploads, and losing the file must not take the application row with it.

No back-fill. Rows written before this revision cannot say which upload they used — the
column did not exist when they were written — and inventing one would be fabricating
evidence about a real submission.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa

from alembic import op
from app.database.types import GUID

#: Identifier of this revision.
revision: str = "0005"

#: Revision this one builds on.
down_revision: str | None = "0004"

#: Branch labels, unused: this project keeps one linear history.
branch_labels: str | Sequence[str] | None = None

#: Dependencies on other version directories, unused.
depends_on: str | Sequence[str] | None = None

#: The table this revision touches.
TABLE: Final[str] = "applications"

#: The column it adds.
COLUMN: Final[str] = "submitted_resume_file_id"

#: Name of the foreign key, matching the convention in ``0001_initial_schema.py``.
FK_NAME: Final[str] = "fk_applications_submitted_resume_file_id_uploaded_files"

#: Name of the supporting index.
INDEX_NAME: Final[str] = "ix_applications_submitted_resume_file_id"


def upgrade() -> None:
    """Add the submitted-résumé reference."""
    op.add_column(TABLE, sa.Column(COLUMN, GUID(), nullable=True))
    op.create_index(INDEX_NAME, TABLE, [COLUMN], unique=False)
    # Named explicitly so the constraint can be dropped by name on PostgreSQL; SQLite
    # rebuilds the table under batch mode and ignores the name.
    with op.batch_alter_table(TABLE) as batch:
        batch.create_foreign_key(
            FK_NAME,
            "uploaded_files",
            [COLUMN],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Drop it. `resume_version_id` is untouched and keeps its own meaning."""
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(FK_NAME, type_="foreignkey")
    op.drop_index(INDEX_NAME, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
