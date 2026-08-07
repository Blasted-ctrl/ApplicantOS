"""Uploaded and generated files — the metadata index over the blob store.

Bytes live in :mod:`app.storage` (a local directory or S3); this table is the *catalogue*.
Separating the two is what lets the same row describe a file whether it sits on disk or in
a bucket, and what lets cleanup reclaim bytes without losing the record that they existed.

Three columns carry most of the weight:

``sha256``
    Content address. Indexed, because the storage layer deduplicates on it: uploading the
    same transcript twice must not cost two objects, and a rendered resume whose digest is
    unchanged does not need re-rendering.

``storage_key``
    Backend-relative location. Indexed because the reverse lookup (given an object, which
    row describes it?) runs during orphan sweeps.

``deleted_at``
    Soft deletion. A file is retired by stamping this, not by deleting the row: golden
    rule #6 keeps the *record* of a submitted document even when
    ``delete_temp_resume_after_submit`` has reclaimed the bytes. The composite
    ``(kind, deleted_at)`` index serves the maintenance queries that drive exactly that
    sweep ("every live screenshot", "every retired tailored resume").
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import Enum as SAEnum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import GUID, utcnow
from app.models.enums import DocumentKind
from app.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.cover_letter import CoverLetter
    from app.models.resume import ResumeVersion
    from app.models.user import User

__all__ = ["DEFAULT_STORAGE_BACKEND", "DOCUMENT_KIND_COLUMN", "UploadedFile"]


# --------------------------------------------------------------------------------------
# Column types and sizes
# --------------------------------------------------------------------------------------

# Enum columns persist the lowercase string *value*, never the Python member name.
_ENUM_VALUES: Final = operator.methodcaller("values")

#: Storage type for :class:`~app.models.enums.DocumentKind`.
DOCUMENT_KIND_COLUMN: Final[SAEnum] = SAEnum(
    DocumentKind,
    name="document_kind",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Width of a hex-encoded SHA-256 digest.
SHA256_HEX_LENGTH: Final[int] = 64

#: Width of the original filename as supplied or generated.
FILENAME_MAX_LENGTH: Final[int] = 255

#: Width of a MIME type, generous enough for parameterised types.
CONTENT_TYPE_MAX_LENGTH: Final[int] = 255

#: Width of a backend-relative object key (S3 keys may be long and deeply nested).
STORAGE_KEY_MAX_LENGTH: Final[int] = 1024

#: Width of the storage backend name (``local`` / ``s3``).
STORAGE_BACKEND_MAX_LENGTH: Final[int] = 32

#: Backend recorded when the caller does not name one. Mirrors ``settings.storage_backend``.
DEFAULT_STORAGE_BACKEND: Final[str] = "local"


class UploadedFile(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Catalogue entry for one blob in the storage backend.

    Covers everything with bytes: user uploads (a source resume, a transcript), generated
    documents (tailored resumes, cover letters), and browser artifacts (proof-of-submission
    screenshots). :attr:`user_id` is nullable because some artifacts belong to the system
    rather than to a person.
    """

    __tablename__ = "uploaded_files"
    __table_args__ = (
        # Maintenance sweeps: "every live screenshot", "every retired tailored resume".
        Index("ix_uploaded_files_kind_deleted_at", "kind", "deleted_at"),
    )

    # Declared explicitly rather than via UserOwnedMixin, which is NOT NULL: the contract
    # makes this column nullable so system-owned artifacts have a home. CASCADE, because
    # deleting a user must take their documents with them.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        doc="Owning user, or NULL for system-owned artifacts.",
    )
    kind: Mapped[DocumentKind] = mapped_column(
        DOCUMENT_KIND_COLUMN,
        default=DocumentKind.OTHER,
        nullable=False,
        doc="What this file is; drives retention policy and the documents UI.",
    )
    filename: Mapped[str] = mapped_column(
        String(FILENAME_MAX_LENGTH),
        nullable=False,
        doc="Display filename, as uploaded or as generated for download.",
    )
    content_type: Mapped[str | None] = mapped_column(
        String(CONTENT_TYPE_MAX_LENGTH),
        nullable=True,
        doc="MIME type, used for the download response and for parser dispatch.",
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="Object size in bytes; drives storage accounting and upload limits.",
    )
    storage_key: Mapped[str] = mapped_column(
        String(STORAGE_KEY_MAX_LENGTH),
        nullable=False,
        index=True,
        doc="Backend-relative location of the bytes. Meaningless without `backend`.",
    )
    sha256: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH),
        nullable=True,
        index=True,
        doc="Content address; the storage layer deduplicates and cache-keys on it.",
    )
    backend: Mapped[str] = mapped_column(
        String(STORAGE_BACKEND_MAX_LENGTH),
        default=DEFAULT_STORAGE_BACKEND,
        server_default=DEFAULT_STORAGE_BACKEND,
        nullable=False,
        doc="Which storage backend holds the bytes ('local' or 's3').",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        index=True,
        doc="When the bytes may be reclaimed. NULL means keep indefinitely.",
    )

    # -- relationships ---------------------------------------------------------------
    # Every reference to a file is an optional pointer with ON DELETE SET NULL: the
    # artifact is disposable, the record that referenced it is not.
    resume_versions: Mapped[list[ResumeVersion]] = relationship(
        "ResumeVersion",
        back_populates="file",
        foreign_keys="ResumeVersion.file_id",
        passive_deletes=True,
    )
    cover_letters: Mapped[list[CoverLetter]] = relationship(
        "CoverLetter",
        back_populates="file",
        foreign_keys="CoverLetter.file_id",
        passive_deletes=True,
    )
    confirmation_for_applications: Mapped[list[Application]] = relationship(
        "Application",
        back_populates="confirmation_screenshot",
        foreign_keys="Application.confirmation_screenshot_id",
        passive_deletes=True,
    )
    # One-directional: `User` declares no reverse collection for files. Nothing else
    # writes `uploaded_files.user_id`, so there is no conflicting sync target.
    user: Mapped[User | None] = relationship("User")

    # -- behaviour --------------------------------------------------------------------

    def is_expired(self, now: datetime | None = None) -> bool:
        """Whether the bytes behind this row may be reclaimed.

        Args:
            now: Instant to compare against. Defaults to the current UTC time; passing it
                explicitly makes cleanup sweeps deterministic and testable.

        Returns:
            ``False`` when :attr:`expires_at` is unset — no expiry means keep forever.
        """
        if self.expires_at is None:
            return False
        return self.expires_at <= (now if now is not None else utcnow())

    def is_reclaimable(self, now: datetime | None = None) -> bool:
        """Whether ``cleanup.prune_artifacts`` should delete this file's bytes.

        Args:
            now: Instant to compare against, forwarded to :meth:`is_expired`.

        Returns:
            ``True`` when the row has been soft-deleted or has passed its expiry. The row
            itself always survives — only the bytes go.
        """
        return bool(self.deleted_at is not None or self.is_expired(now))

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"UploadedFile(id={self.id!r}, kind={self.kind!r}, "
            f"filename={self.filename!r}, backend={self.backend!r}, "
            f"size_bytes={self.size_bytes!r})"
        )
