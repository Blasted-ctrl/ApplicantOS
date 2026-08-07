"""Generated cover letters.

A cover letter is written for a specific posting, so :attr:`CoverLetter.posting_id` is
mandatory and cascades: a letter addressed to a job that no longer exists is dead weight.
The link to an :class:`~app.models.application.Application` is *optional* and set later,
because ``CoverLetterWriter.should_write()`` may decide to draft one during preparation,
before the application row is finalised — and because a user can ask for a letter without
ever applying.

Like resumes (``docs/CONTRACTS.md`` §18, golden rule #6), the rendered PDF is disposable
and the text is not: :attr:`CoverLetter.body` is the canonical artifact and
:attr:`CoverLetter.file_id` is ``ON DELETE SET NULL``, so cleanup can reclaim the file
without losing what was actually said to the employer.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import GUID
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.file import UploadedFile
    from app.models.posting import JobPosting
    from app.models.user import User

__all__ = ["DEFAULT_COVER_LETTER_TONE", "CoverLetter"]

#: Width of the tone label ("professional", "enthusiastic", "concise", ...).
COVER_LETTER_TONE_MAX_LENGTH: Final[int] = 32

#: Tone applied when the caller expresses no preference.
DEFAULT_COVER_LETTER_TONE: Final[str] = "professional"


class CoverLetter(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """A cover letter drafted for one posting, optionally attached to one application.

    Note:
        :attr:`token_usage` is a plain JSON column and is not change tracked; reassign it
        rather than mutating it in place.
    """

    __tablename__ = "cover_letters"

    posting_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Posting this letter addresses. A letter has no meaning without it.",
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "Application the letter was submitted with, once one exists. SET NULL keeps "
            "the text as evidence even if the application row is removed."
        ),
    )
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="The letter itself, in plain text. Canonical and permanent.",
    )
    tone: Mapped[str] = mapped_column(
        String(COVER_LETTER_TONE_MAX_LENGTH),
        default=DEFAULT_COVER_LETTER_TONE,
        server_default=DEFAULT_COVER_LETTER_TONE,
        nullable=False,
        doc="Register the letter was written in; feeds regeneration prompts.",
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Rendered artifact. Disposable — the body above is the source of truth.",
    )
    token_usage: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Input/output token counts for the generation that produced this letter.",
    )

    # -- relationships ---------------------------------------------------------------
    posting: Mapped[JobPosting] = relationship(
        "JobPosting",
        back_populates="cover_letters",
        lazy="selectin",
    )
    # `foreign_keys` is mandatory: `applications.cover_letter_id` points back here, so
    # there are two candidate join paths between these tables.
    application: Mapped[Application | None] = relationship(
        "Application",
        back_populates="cover_letters",
        foreign_keys=[application_id],
    )
    file: Mapped[UploadedFile | None] = relationship(
        "UploadedFile",
        back_populates="cover_letters",
        foreign_keys=[file_id],
        lazy="selectin",
    )
    # One-directional: `User` declares no reverse collection for cover letters. Nothing
    # else writes `cover_letters.user_id`, so there is no conflicting sync target.
    user: Mapped[User] = relationship("User")

    @property
    def word_count(self) -> int:
        """Approximate length of the letter, in whitespace-delimited words.

        Used by the review UI and by the renderer's page-fit heuristics.

        Returns:
            The number of whitespace-separated tokens in :attr:`body`, or ``0``.
        """
        if not self.body:
            return 0
        return len(self.body.split())

    @property
    def has_artifact(self) -> bool:
        """Whether a rendered file is currently attached to this letter."""
        return self.file_id is not None

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"CoverLetter(id={self.id!r}, user_id={self.user_id!r}, "
            f"posting_id={self.posting_id!r}, tone={self.tone!r}, "
            f"words={self.word_count!r})"
        )
