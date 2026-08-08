"""Helpers shared by more than one route module.

The leading underscore is load-bearing: :func:`app.api.routes.iter_route_modules` skips
modules whose name starts with ``_``, so this file is a library for the route modules rather
than a thirteenth route group.

Three concerns live here, each because two or more groups need it and duplicating it would
let the copies drift:

**Ownership checks.** ApplicantOS is single-tenant but not single-*profile*: ``X-User-Id``
selects which profile a request acts as. Every handler that loads a row by id therefore has
to confirm the row belongs to the acting user, and the answer to "it does not" is **404, not
403** — a 403 confirms that the id exists, which is exactly the fact a probing client wants.
:func:`require_owner` is that check, spelled once.

**Blob access.** ``app/storage/`` does not exist in this tree, so the local filesystem is the
backend and keys resolve beneath ``settings.storage_root`` — the same rule
:meth:`app.services.pipeline.Pipeline._storage_path` applies when it writes them. The key is
decomposed with :mod:`pathlib` and ``..`` segments are dropped, so a crafted
``storage_key`` cannot escape the root. A row whose ``backend`` is not ``local`` is reported
as a 409 rather than a 500: nothing is broken, this process simply cannot serve those bytes.

**Download URLs.** ``ArtifactRead.url``, ``ResumeVersionRead.download_url`` and
``CoverLetterRead.download_url`` are all documented as "set by the API layer". They are built
here from :data:`API_V1_PREFIX` so that the prefix is stated in exactly one place — this
module — and :mod:`app.main` mounts the router under the same constant.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Final

import structlog
from fastapi import HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.models.file import UploadedFile
from app.schemas.application import ArtifactRead

__all__ = [
    "API_V1_PREFIX",
    "APPLICATION_KEY_TEMPLATE",
    "LIKE_ESCAPE",
    "LOCAL_BACKEND",
    "application_key_prefix",
    "artifact_download_url",
    "artifact_read",
    "as_artifact_response",
    "csv_list",
    "escape_like",
    "like_pattern",
    "load_uploaded_file",
    "require_owner",
    "resume_download_url",
    "storage_path",
]

logger = structlog.get_logger(__name__)

#: Where :func:`app.main.create_app` mounts the aggregated router. Download links are built
#: against it, so changing the mount point is a one-line change here.
API_V1_PREFIX: Final[str] = "/api/v1"

#: The only storage backend this process can stream bytes from.
LOCAL_BACKEND: Final[str] = "local"

#: Prefix every artifact of one application is stored under. Mirrors
#: :meth:`app.services.pipeline.Pipeline._storage_key`, which is what writes them; it is the
#: only link between an ``uploaded_files`` row and the application that produced it, because
#: the table deliberately carries no ``application_id`` column.
APPLICATION_KEY_TEMPLATE: Final[str] = "users/{user_id}/applications/{application_id}/"

#: Detail returned when a catalogued file's bytes are gone.
_MISSING_BYTES_DETAIL: Final[str] = (
    "This file's record still exists but its bytes have been reclaimed. Generated documents "
    "are disposable; the structured version they were rendered from is retained."
)

#: Detail returned when the row names a backend this process cannot read.
_FOREIGN_BACKEND_DETAIL: Final[str] = (
    "This file lives in a storage backend this process cannot stream from."
)

#: Characters that mean something to SQL ``LIKE`` and must be neutralised in user input.
_LIKE_SPECIALS: Final[tuple[str, ...]] = ("\\", "%", "_")

#: Escape character used with every ``LIKE`` built by :func:`like_pattern`.
LIKE_ESCAPE: Final[str] = "\\"


# ======================================================================================
# Ownership
# ======================================================================================


def require_owner(owner_id: uuid.UUID | None, user_id: uuid.UUID, label: str) -> None:
    """Raise 404 when a loaded row does not belong to the acting user.

    404 rather than 403 on purpose: a 403 confirms the identifier names a real row, which
    turns an id-guessing loop into an enumeration oracle. From the caller's side there is no
    difference between "does not exist" and "is not yours", and there should not be.

    Args:
        owner_id: The row's ``user_id``.
        user_id: The acting user.
        label: Noun used in the message, e.g. ``"application"``.

    Raises:
        HTTPException: ``404`` when the row belongs to somebody else.
    """
    if owner_id is not None and owner_id == user_id:
        return
    logger.info("api.cross_user_access_refused", resource=label, user_id=str(user_id))
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No {label} exists with that id.",
    )


# ======================================================================================
# Query-string helpers
# ======================================================================================


def csv_list(value: str | None) -> list[str]:
    """Split a comma-separated query parameter into trimmed, non-empty items.

    Repeated query parameters (``?kind=a&kind=b``) and comma-separated ones
    (``?kind=a,b``) are both idiomatic; accepting the comma form costs one function and
    saves every client from having to know which style this API prefers.

    Args:
        value: The raw parameter value, or ``None``.

    Returns:
        The items, in the order given, with duplicates preserved.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def escape_like(term: str) -> str:
    """Neutralise ``LIKE`` wildcards in a user-supplied search term.

    Without this, a search for ``100%`` matches every row: ``%`` is a wildcard, not a
    literal, and a user typing one is never asking for that.

    Args:
        term: The raw search term.

    Returns:
        The term with ``\\``, ``%`` and ``_`` escaped.
    """
    escaped = term
    for special in _LIKE_SPECIALS:
        escaped = escaped.replace(special, f"{LIKE_ESCAPE}{special}")
    return escaped


def like_pattern(term: str) -> str:
    """Build the containment pattern for a free-text filter.

    Args:
        term: The raw search term.

    Returns:
        ``%term%`` with the term's own wildcards escaped.
    """
    return f"%{escape_like(term.strip())}%"


# ======================================================================================
# Blobs
# ======================================================================================


def storage_path(settings: Settings, storage_key: str) -> Path:
    """Resolve a backend-relative key to an absolute path under the local blob root.

    Mirrors :meth:`app.services.pipeline.Pipeline._storage_path`, which is what wrote the
    key. Empty, ``.`` and ``..`` segments are discarded, so a key crafted to traverse
    upwards resolves to a path inside the root instead of outside it.

    Args:
        settings: Runtime configuration, supplying ``storage_root``.
        storage_key: The POSIX-style key stored on the row.

    Returns:
        The absolute path the bytes live at.
    """
    segments = [part for part in storage_key.split("/") if part not in ("", ".", "..")]
    return settings.storage_root.joinpath(*segments)


async def load_uploaded_file(session: AsyncSession, file_id: uuid.UUID) -> UploadedFile:
    """Load one catalogued file, or raise 404.

    Args:
        session: The request's database session.
        file_id: The file's identifier.

    Returns:
        The row.

    Raises:
        HTTPException: ``404`` when no live row has that id. A soft-deleted row is treated
            as absent — the record survives for the audit trail, the bytes do not.
    """
    record = await session.scalar(select(UploadedFile).where(UploadedFile.id == file_id).limit(1))
    if record is None or record.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No file exists with that id.",
        )
    return record


def as_artifact_response(record: UploadedFile, settings: Settings) -> FileResponse:
    """Stream a catalogued file back to the client.

    Args:
        record: The catalogue row.
        settings: Runtime configuration, supplying the blob root.

    Returns:
        A :class:`~fastapi.responses.FileResponse` carrying the original filename and MIME
        type, streamed rather than buffered — a rendered resume is small but a browser
        artifact bundle need not be.

    Raises:
        HTTPException: ``409`` when the row names a backend this process cannot read, and
            ``404`` when the bytes have been reclaimed.
    """
    backend = (record.backend or LOCAL_BACKEND).strip().lower()
    if backend != LOCAL_BACKEND:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_FOREIGN_BACKEND_DETAIL,
        )

    path = storage_path(settings, record.storage_key)
    if not path.is_file():
        logger.info("api.artifact_bytes_missing", file_id=str(record.id))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MISSING_BYTES_DETAIL,
        )

    return FileResponse(
        path=path,
        media_type=record.content_type or "application/octet-stream",
        filename=record.filename,
    )


# ======================================================================================
# Download links
# ======================================================================================


def artifact_download_url(application_id: uuid.UUID, file_id: uuid.UUID) -> str:
    """Return the link that streams one application artifact.

    Args:
        application_id: The owning application.
        file_id: The artifact.

    Returns:
        An absolute path on this server, never a storage-backend URL — a local install and
        an S3 install must expose the same shape.
    """
    return f"{API_V1_PREFIX}/applications/{application_id}/artifacts/{file_id}"


def resume_download_url(version_id: uuid.UUID) -> str:
    """Return the link that streams one rendered resume version.

    Args:
        version_id: The :class:`~app.models.resume.ResumeVersion` identifier.

    Returns:
        The path of ``GET /resumes/versions/{id}/download``.
    """
    return f"{API_V1_PREFIX}/resumes/versions/{version_id}/download"


def artifact_read(record: UploadedFile, application_id: uuid.UUID) -> ArtifactRead:
    """Project a catalogue row into its API shape, with a working download link.

    Args:
        record: The catalogue row.
        application_id: The application the artifact belongs to.

    Returns:
        The populated :class:`~app.schemas.application.ArtifactRead`.
    """
    item = ArtifactRead.model_validate(record)
    item.url = artifact_download_url(application_id, record.id)
    return item


def application_key_prefix(user_id: uuid.UUID, application_id: uuid.UUID) -> str:
    """Return the storage-key prefix every artifact of one application shares.

    Args:
        user_id: The owning user.
        application_id: The application.

    Returns:
        The prefix, ending in ``/`` so a ``LIKE`` cannot match a sibling id that merely
        starts with the same characters.
    """
    return APPLICATION_KEY_TEMPLATE.format(user_id=user_id, application_id=application_id)
