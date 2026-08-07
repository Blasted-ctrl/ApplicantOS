"""Per-user fit scores for a posting.

A score is the decision record that stands between discovery and action: it is what
``auto_apply_min_score`` is compared against, what the desktop feed sorts by, and what an
audit has to look at to answer "why did it apply to *that*?".

Scoring is per *user*, not per posting: two users with different preferences, skills and
sponsorship needs get genuinely different numbers for the same advertisement. Hence
``UNIQUE(posting_id, user_id)`` — exactly one current score per (posting, user) pair, so
re-scoring is an upsert and the feed can never show two contradictory verdicts.

Explainability is a first-class requirement of ``docs/CONTRACTS.md`` §10, which is why the
row keeps three separate things:

* :attr:`total` / :attr:`normalized` — the numbers, from the deterministic rule engine.
* :attr:`breakdown` — every component that contributed, so ``explain()`` can render the
  arithmetic without re-running the rules.
* :attr:`rationale` / :attr:`model_used` — the optional LLM commentary, kept apart from
  the deterministic result so it is never mistaken for it. The model may nudge the score
  by ±10 but may never flip a hard negative into "apply".
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import GUID
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.posting import JobPosting
    from app.models.user import User

__all__ = ["MAX_NORMALIZED_SCORE", "MIN_NORMALIZED_SCORE", "JobScore"]

#: Lower bound of :attr:`JobScore.normalized`. ``total`` is unbounded (rules may sum to
#: anything); ``normalized`` is the clamped 0-100 figure the whole product compares
#: against, including ``settings.auto_apply_min_score`` and ``UserPreferences.min_score``.
MIN_NORMALIZED_SCORE: Final[int] = 0

#: Upper bound of :attr:`JobScore.normalized`.
MAX_NORMALIZED_SCORE: Final[int] = 100

#: Width of a scoring verdict label (``"apply"`` / ``"review"`` / ``"skip"``). Stored as a
#: string rather than an enum because ``docs/CONTRACTS.md`` §3 declares no verdict enum;
#: see ``docs/OPEN_QUESTIONS.md``.
VERDICT_MAX_LENGTH: Final[int] = 32

#: Width of the identifier of the model that produced :attr:`JobScore.rationale`.
MODEL_NAME_MAX_LENGTH: Final[int] = 128


class JobScore(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """How well one posting fits one user, with the reasoning attached.

    Note:
        :attr:`breakdown` is a plain JSON column and is not change tracked. Reassign it
        wholesale when re-scoring rather than mutating the nested structure.
    """

    __tablename__ = "job_scores"
    __table_args__ = (
        # One current score per (posting, user): re-scoring upserts, never appends.
        UniqueConstraint(
            "posting_id",
            "user_id",
            name="uq_job_scores_posting_id_user_id",
        ),
    )

    posting_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Scored posting. A score outlives nothing — it dies with its posting.",
    )

    total: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        doc="Raw sum of every fired rule's points; may be negative or exceed 100.",
    )
    normalized: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        index=True,
        doc="`total` clamped to 0-100. This is the figure every threshold compares against.",
    )
    breakdown: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Serialised ScoreComponent list: which rules fired, with what points and why.",
    )
    verdict: Mapped[str | None] = mapped_column(
        String(VERDICT_MAX_LENGTH),
        nullable=True,
        index=True,
        doc="Routing decision derived from `normalized` and the hard-negative rules.",
    )
    model_used: Mapped[str | None] = mapped_column(
        String(MODEL_NAME_MAX_LENGTH),
        nullable=True,
        doc="Model that wrote `rationale`; NULL when scoring ran purely on rules.",
    )
    rationale: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Human-readable explanation. Advisory only — never the source of the number.",
    )

    # -- relationships ---------------------------------------------------------------
    # The reverse (`JobPosting.scores`) is eager; this side is not, to avoid loading a
    # posting graph when a score is fetched on its own. Use selectinload() when needed.
    posting: Mapped[JobPosting] = relationship(
        "JobPosting",
        back_populates="scores",
    )
    # One-directional: `User` (app/models/user.py) declares no reverse collection for
    # scores, and naming one here that does not exist would hard-fail mapper
    # configuration. Nothing else writes `job_scores.user_id`, so there is no conflict.
    user: Mapped[User] = relationship("User")

    @property
    def is_clamped(self) -> bool:
        """Whether :attr:`normalized` had to be clipped to fit the 0-100 band.

        Useful for spotting a rule pack that has drifted far out of calibration.

        Returns:
            ``True`` when the raw :attr:`total` falls outside the normalised band.
        """
        return not (MIN_NORMALIZED_SCORE <= self.total <= MAX_NORMALIZED_SCORE)

    def meets(self, threshold: int) -> bool:
        """Whether this score clears *threshold*.

        Args:
            threshold: A 0-100 minimum, typically ``settings.auto_apply_min_score`` or
                ``UserPreferences.min_score``.

        Returns:
            ``True`` when :attr:`normalized` is greater than or equal to *threshold*.
        """
        return self.normalized >= threshold

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"JobScore(id={self.id!r}, posting_id={self.posting_id!r}, "
            f"user_id={self.user_id!r}, normalized={self.normalized!r}, "
            f"verdict={self.verdict!r})"
        )
