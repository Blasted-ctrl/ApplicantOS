"""Resumes as *generated views* of the knowledge graph, and their immutable versions.

Golden rule #6 of ``docs/CONTRACTS.md`` §18: knowledge is the source of truth and a resume
is a view over it. Nothing here stores an uploaded PDF as the master copy. Instead:

* :class:`Resume` is a **named variant** — "SWE new-grad", "embedded systems" — carrying a
  template choice and generation config. It holds no content at all.
* :class:`ResumeVersion` is one **immutable rendering decision**: the structured
  :class:`~app.documents.models.ResumeDocument` that was produced, the
  :class:`~app.models.knowledge.KnowledgeFact` ids every bullet traces back to, the token
  cost, and the model's reasoning. ``UNIQUE(resume_id, version_number)`` makes the version
  sequence gapless and race-free.

The retention posture follows from that split. ``content_json`` is retained forever: it is
the audit trail proving golden rule #7 (nothing is fabricated), and regenerating a PDF from
it is free. The rendered file is disposable — ``file_id`` is ``ON DELETE SET NULL``, and
``delete_temp_resume_after_submit`` routinely removes the artifact while the version row
survives untouched.

:class:`ResumeVersion` is soft-deleted rather than deleted, for the same reason.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import GUID
from app.models.mixins import (
    SoftDeleteMixin,
    TimestampMixin,
    UserOwnedMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.file import UploadedFile
    from app.models.user import User

__all__ = ["FIRST_RESUME_VERSION_NUMBER", "Resume", "ResumeVersion"]

#: Version numbers are 1-based and contiguous per resume; ``UNIQUE(resume_id,
#: version_number)`` is what keeps two concurrent generations from claiming the same slot.
FIRST_RESUME_VERSION_NUMBER: Final[int] = 1

#: Width of a resume's display name.
RESUME_NAME_MAX_LENGTH: Final[int] = 120

#: Width of the variant label ("new-grad", "embedded", ...).
RESUME_VARIANT_LABEL_MAX_LENGTH: Final[int] = 120

#: Width of a template plugin name (``modern``, ``classic``, ``ats_plain``, ...).
RESUME_TEMPLATE_MAX_LENGTH: Final[int] = 64

#: Width of a render format token (``pdf``, ``docx``, ``md``, ``html``).
RENDER_FORMAT_MAX_LENGTH: Final[int] = 16

#: Render format used when a caller does not specify one.
DEFAULT_RENDER_FORMAT: Final[str] = "pdf"

#: Template used when a caller does not specify one. Mirrors ``settings.resume_template``.
DEFAULT_RESUME_TEMPLATE: Final[str] = "modern"


class Resume(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """A named resume variant owned by a user.

    Contains no resume *content*: it is the configuration under which versions are
    generated. Exactly one variant per user should carry ``is_default=True``; the
    application layer enforces that, since a partial unique index is not portable to
    SQLite.
    """

    __tablename__ = "resumes"

    name: Mapped[str] = mapped_column(
        String(RESUME_NAME_MAX_LENGTH),
        nullable=False,
        doc="Display name shown in the desktop resume picker.",
    )
    variant_label: Mapped[str | None] = mapped_column(
        String(RESUME_VARIANT_LABEL_MAX_LENGTH),
        nullable=True,
        doc="Short targeting label passed to ResumeEngine as TailorRequest.variant_label.",
    )
    template: Mapped[str] = mapped_column(
        String(RESUME_TEMPLATE_MAX_LENGTH),
        default=DEFAULT_RESUME_TEMPLATE,
        server_default=DEFAULT_RESUME_TEMPLATE,
        nullable=False,
        doc="Name of the TemplatePlugin used to render versions of this variant.",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
        index=True,
        doc="Whether this variant is chosen when a request names none.",
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Generation overrides (max_bullets, section order, tone) for this variant.",
    )

    # -- relationships ---------------------------------------------------------------
    # A variant genuinely owns its versions: deleting the variant is a deliberate act that
    # takes its generation history with it. The FK carries ON DELETE CASCADE, so
    # `passive_deletes` lets the database do the work in one statement.
    versions: Mapped[list[ResumeVersion]] = relationship(
        "ResumeVersion",
        back_populates="resume",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ResumeVersion.version_number",
    )
    user: Mapped[User] = relationship("User", back_populates="resumes")

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"Resume(id={self.id!r}, user_id={self.user_id!r}, name={self.name!r}, "
            f"template={self.template!r}, is_default={self.is_default!r})"
        )


class ResumeVersion(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """One immutable generation of a resume, with full provenance.

    Every bullet in :attr:`content_json` corresponds to an id in :attr:`fact_ids`, which is
    what makes golden rule #7 auditable: the resume can be replayed against the knowledge
    graph and any bullet that does not trace to a fact is a bug, not a judgement call.

    Note:
        :attr:`content_json`, :attr:`fact_ids` and :attr:`token_usage` are plain JSON
        columns and are not change tracked. A version is immutable by intent, so this is
        the right trade — but if one must be corrected, reassign the whole value.
    """

    __tablename__ = "resume_versions"
    __table_args__ = (
        # Gapless, race-free version sequence per variant.
        UniqueConstraint(
            "resume_id",
            "version_number",
            name="uq_resume_versions_resume_id_version_number",
        ),
    )

    resume_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("resumes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Owning variant. A version cannot exist without one.",
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "Application this version was tailored for. SET NULL rather than CASCADE: "
            "golden rule #6 keeps content_json forever, even past the application."
        ),
    )
    version_number: Mapped[int] = mapped_column(
        Integer,
        default=FIRST_RESUME_VERSION_NUMBER,
        nullable=False,
        doc="1-based sequence number within the owning variant.",
    )
    content_json: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Serialised ResumeDocument. The canonical, permanent form of this resume.",
    )
    render_format: Mapped[str] = mapped_column(
        String(RENDER_FORMAT_MAX_LENGTH),
        default=DEFAULT_RENDER_FORMAT,
        server_default=DEFAULT_RENDER_FORMAT,
        nullable=False,
        doc="Format the artifact in `file_id` was rendered to.",
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Rendered artifact. Disposable: cleanup nulls this without touching the row.",
    )
    fact_ids: Mapped[list[str]] = mapped_column(
        default=list,
        nullable=False,
        doc="KnowledgeFact ids every bullet traces to — the anti-hallucination audit trail.",
    )
    token_usage: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Input/output token counts for the generation that produced this version.",
    )
    reasoning: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Model's explanation of which facts it selected and why.",
    )

    # -- relationships ---------------------------------------------------------------
    resume: Mapped[Resume] = relationship(
        "Resume",
        back_populates="versions",
        lazy="selectin",
    )
    # `foreign_keys` is mandatory here: `applications` and `resume_versions` reference each
    # other (applications.resume_version_id points back), so the join condition is
    # ambiguous without naming the column explicitly.
    application: Mapped[Application | None] = relationship(
        "Application",
        back_populates="resume_versions",
        foreign_keys=[application_id],
    )
    file: Mapped[UploadedFile | None] = relationship(
        "UploadedFile",
        back_populates="resume_versions",
        foreign_keys=[file_id],
        lazy="selectin",
    )

    @property
    def bullet_count(self) -> int:
        """Number of knowledge facts this version drew on.

        Returns:
            The length of :attr:`fact_ids`, or ``0`` when the column is unset.
        """
        return len(self.fact_ids or ())

    @property
    def has_artifact(self) -> bool:
        """Whether a rendered file is currently attached to this version.

        A ``False`` here is normal, not an error: artifacts are deleted after submission
        when ``settings.delete_temp_resume_after_submit`` is on, and the version can always
        be re-rendered from :attr:`content_json`.
        """
        return self.file_id is not None

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"ResumeVersion(id={self.id!r}, resume_id={self.resume_id!r}, "
            f"version_number={self.version_number!r}, "
            f"render_format={self.render_format!r})"
        )
