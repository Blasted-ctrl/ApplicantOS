"""submitted answers

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-16

Separates *what was submitted* from *what a human decided*.

``applications.answers`` looks like a record and is not one. It is an **input**:
:meth:`app.services.pipeline.Pipeline.submit` hands it to the field answerer, where
``FieldAnswerer._explicit`` returns any match at confidence ``1.0`` — ahead of the user's
profile, ahead of the EEO branch that honours "decline to self-identify", and ahead of the
model. That precedence is correct precisely because the only writer was
``ReviewService.resolve``, which stores values a person typed.

Recording the browser's own output into that same column therefore did something much worse
than duplicate a field. It froze machine-resolved values as though a human had chosen them:
a demographic disclosure the user later retracted would still be submitted on a retry, a
corrected phone number would be ignored, and a model-written essay would come back at
confidence ``1.0``, above any ``min_answer_confidence`` the user could set.

So the record gets a column of its own. ``answers`` keeps its meaning and its precedence;
``submitted_answers`` is written by the pipeline, read by the UI, and read back by nothing.

``NOT NULL DEFAULT '{}'`` because this is an ``ALTER TABLE ... ADD COLUMN`` against a table
that already has rows. Existing applications get an empty record rather than a back-filled
one: nothing knows what those forms said, and inventing it would be fabrication.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa

from alembic import op
from app.database.types import JSONType

#: Identifier of this revision.
revision: str = "0004"

#: Revision this one builds on.
down_revision: str | None = "0003"

#: Branch labels, unused: this project keeps one linear history.
branch_labels: str | Sequence[str] | None = None

#: Dependencies on other version directories, unused.
depends_on: str | Sequence[str] | None = None

#: The table this revision touches.
TABLE: Final[str] = "applications"

#: The column it adds.
COLUMN: Final[str] = "submitted_answers"


def upgrade() -> None:
    """Add the submitted-answer record."""
    op.add_column(
        TABLE,
        sa.Column(COLUMN, JSONType(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    """Drop it. The answers a human settled are in ``answers`` and are untouched."""
    op.drop_column(TABLE, COLUMN)
