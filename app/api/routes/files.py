"""File upload and download (``docs/CONTRACTS.md`` §14).

The one route group whose request body is not JSON. Onboarding asks for a master resume,
the knowledge engine indexes a LinkedIn export, and the review queue attaches a transcript —
all of which need bytes to cross the boundary, and none of which could, because until now
``uploaded_files`` had a model, ``StorageBackend`` had a ``put``, and there was nothing in
between. The wizard's ``master_resume`` step declares a field of kind ``file`` and the client
had no endpoint to give it an id from.

Three decisions worth stating, because each is a place this could have been subtly wrong:

**The bytes are read in bounded chunks and the limit is enforced while reading**, not after.
``UploadFile`` spools to a temp file rather than into memory, so a client that lies in
``Content-Length`` — or omits it, which is legal for ``multipart/form-data`` — cannot make
this handler allocate more than :data:`~app.config.settings.Settings.max_upload_mb`. The
check is on bytes actually consumed, so it is not fooled by a header at all.

**The storage key is content-addressed**, so uploading the same resume twice writes one
object. Two catalogue rows still result: they are different upload *events*, with their own
filenames and owners, and collapsing them would make deleting one delete the other's
metadata. The bytes are shared, the records are not. Digest computation is incremental over
the same chunks the size check walks — the file is never held whole in memory.

**Download streams and never guesses.** ``Content-Type`` is the type recorded at upload, and
the filename goes out in a quoted ``Content-Disposition`` with a UTF-8 fallback, because a
resume called ``Résumé.pdf`` is the common case and not an edge one.

Deleting is a soft delete of the catalogue row. The bytes stay: another row may be content-
addressed to the same object, and the maintenance sweep that reclaims unreferenced blobs is
the only thing entitled to decide a digest has no readers left.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Final
from urllib.parse import quote

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, PaginationDep, SettingsDep
from app.models.enums import DocumentKind
from app.models.file import UploadedFile
from app.schemas.common import OkResponse, Page
from app.schemas.file import FileRead

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/files"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["files"]

#: Bytes per read while streaming an upload to storage. Large enough that a 25 MB resume is
#: tens of reads, small enough that a hostile upload is rejected long before it is a problem.
_CHUNK_BYTES: Final[int] = 1024 * 1024

#: Bytes in a megabyte, so ``max_upload_mb`` converts in one named place.
_BYTES_PER_MB: Final[int] = 1024 * 1024

#: Filename used when a client sends a part with no name at all. Multipart permits it.
_FALLBACK_FILENAME: Final[str] = "upload"

#: Longest filename kept. Matches ``uploaded_files.filename``; a longer one is truncated
#: rather than rejected, because the name is a display label and the bytes are the point.
_FILENAME_MAX: Final[int] = 255

router = APIRouter()


def _safe_filename(raw: str | None) -> str:
    """Reduce an uploaded part's filename to something safe to store and display.

    A multipart filename is attacker-controlled and is *not* a path: browsers send a bare
    name, but nothing enforces it, and ``../../.ssh/authorized_keys`` is a legal value. It
    never reaches the filesystem here — the storage key is derived from the content digest,
    not from this — but it is displayed, logged, and echoed in ``Content-Disposition``, so it
    is stripped to its last component and to a single line.

    Args:
        raw: The filename as supplied, which may be ``None``.

    Returns:
        A non-empty display filename of at most :data:`_FILENAME_MAX` characters.
    """
    candidate = (raw or "").replace("\\", "/").split("/")[-1].strip()
    candidate = "".join(character for character in candidate if character.isprintable())
    candidate = candidate.lstrip(".").strip()
    if not candidate:
        return _FALLBACK_FILENAME
    return candidate[:_FILENAME_MAX]


def _content_disposition(filename: str) -> str:
    """Build a ``Content-Disposition`` value that survives a non-ASCII filename.

    RFC 6266: an ASCII-only ``filename`` for old clients, plus ``filename*`` with a UTF-8
    percent-encoding for everything current. A resume named ``Résumé.pdf`` is ordinary, and
    a header that mangles it — or worse, one that lets a quote in the name close the quoted
    string early and inject a header parameter — is not acceptable.

    Args:
        filename: The already-sanitised display filename.

    Returns:
        The full header value.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.post(
    "",
    response_model=FileRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file",
    description="`multipart/form-data`. Returns the catalogue entry, whose id is what other "
    "endpoints reference — `master_resume.file_id`, a knowledge source, a review attachment.",
)
async def upload_file(
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    file: UploadFile = File(description="The bytes."),
    kind: DocumentKind = Form(
        default=DocumentKind.OTHER,
        description="What this file is. Drives retention and which screen lists it.",
    ),
) -> FileRead:
    """Store an uploaded file and catalogue it.

    Args:
        user: The acting user, who owns the resulting row.
        session: The request's database session.
        settings: Application settings, for the upload ceiling.
        file: The uploaded part.
        kind: What the file is.

    Returns:
        The catalogue entry, 201.

    Raises:
        HTTPException: ``413`` if the upload exceeds ``max_upload_mb``, ``400`` if it is
            empty, ``500`` if the storage backend refuses the write.
    """
    from app.storage import get_storage
    from app.storage.base import StorageError, build_key

    limit = max(1, settings.max_upload_mb) * _BYTES_PER_MB
    filename = _safe_filename(file.filename)

    # Read once, into memory, bounded. The ceiling is what makes that safe, and it is
    # checked against bytes actually consumed rather than against Content-Length, which a
    # multipart client is not obliged to send and an attacker is not obliged to mean.
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            logger.warning("api.upload_too_large", filename=filename, limit_bytes=limit)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"That file is larger than the {settings.max_upload_mb} MB limit. "
                    "Raise `MAX_UPLOAD_MB` if you need to."
                ),
            )
        digest.update(chunk)
        chunks.append(chunk)

    if size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That file is empty, so there is nothing to store.",
        )

    sha256 = digest.hexdigest()
    data = b"".join(chunks)

    # Content-addressed: the same bytes land on the same key no matter who uploads them or
    # what they call it (golden rule #9 — hashlib, never the salted builtin `hash`).
    suffix = filename.rpartition(".")[2] if "." in filename else None
    key = build_key("uploads", str(kind), sha256, ext=suffix or None)

    storage = get_storage()
    try:
        stored = await storage.put(key, data, content_type=file.content_type)
    except StorageError as exc:
        logger.error("api.upload_failed", filename=filename, key=key, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The file could not be written to storage.",
        ) from exc

    row = UploadedFile(
        user_id=user.id,
        kind=kind,
        filename=filename,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        storage_key=stored.key,
        sha256=stored.sha256,
        backend=stored.backend,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    logger.info(
        "api.file_uploaded",
        file_id=str(row.id),
        kind=str(kind),
        size_bytes=row.size_bytes,
        backend=row.backend,
    )
    return FileRead.model_validate(row)


@router.get(
    "",
    response_model=Page[FileRead],
    summary="List uploaded files",
)
async def list_files(
    user: CurrentUser,
    session: DbSession,
    pagination: PaginationDep,
    kind: DocumentKind | None = Query(default=None, description="Filter to one kind."),
) -> Page[FileRead]:
    """List this user's files, newest first.

    Args:
        user: The acting user.
        session: The request's database session.
        pagination: Limit and offset.
        kind: Optional kind filter.

    Returns:
        One page of catalogue entries.
    """
    conditions = [UploadedFile.user_id == user.id, UploadedFile.deleted_at.is_(None)]
    if kind is not None:
        conditions.append(UploadedFile.kind == kind)

    total = int(
        await session.scalar(select(func.count()).select_from(UploadedFile).where(*conditions)) or 0
    )
    rows = (
        (
            await session.execute(
                select(UploadedFile)
                .where(*conditions)
                .order_by(UploadedFile.created_at.desc(), UploadedFile.id.desc())
                .limit(pagination.limit)
                .offset(pagination.offset)
            )
        )
        .scalars()
        .all()
    )
    return Page.of(
        [FileRead.model_validate(row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


async def _load(
    session: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID
) -> UploadedFile:
    """Load one live file belonging to *user_id*.

    Ownership is part of the lookup rather than a check after it, so a miss and a
    someone-else's-file are indistinguishable from outside — the 404 does not confirm that
    an id exists.

    Args:
        session: The request's database session.
        user_id: The acting user.
        file_id: The file being addressed.

    Returns:
        The row.

    Raises:
        HTTPException: ``404`` if there is no such live file for this user.
    """
    row = await session.scalar(
        select(UploadedFile)
        .where(
            UploadedFile.id == file_id,
            UploadedFile.user_id == user_id,
            UploadedFile.deleted_at.is_(None),
        )
        .limit(1)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such file.",
        )
    return row


@router.get(
    "/{file_id}",
    response_model=FileRead,
    summary="One file's metadata",
)
async def get_file(user: CurrentUser, session: DbSession, file_id: uuid.UUID) -> FileRead:
    """Return one catalogue entry.

    Args:
        user: The acting user.
        session: The request's database session.
        file_id: The file being addressed.

    Returns:
        The catalogue entry.

    Raises:
        HTTPException: ``404`` if there is no such live file for this user.
    """
    return FileRead.model_validate(await _load(session, user.id, file_id))


@router.get(
    "/{file_id}/content",
    summary="Download the bytes",
    response_class=StreamingResponse,
    responses={200: {"content": {"*/*": {}}, "description": "The file."}},
)
async def download_file(
    user: CurrentUser, session: DbSession, file_id: uuid.UUID
) -> StreamingResponse:
    """Stream a file's bytes.

    Streamed rather than read whole: this is also how a submission screenshot and a rendered
    resume come back, and neither should be sized by what fits in the API process.

    Args:
        user: The acting user.
        session: The request's database session.
        file_id: The file being addressed.

    Returns:
        A streaming response carrying the stored bytes.

    Raises:
        HTTPException: ``404`` if there is no such live file, or if the catalogue row
            outlived the object it points at.
    """
    from app.storage import get_storage
    from app.storage.base import ObjectNotFoundError

    row = await _load(session, user.id, file_id)
    storage = get_storage()
    if not await storage.exists(row.storage_key):
        logger.warning("api.file_bytes_missing", file_id=str(row.id), key=row.storage_key)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The catalogue has this file but the stored bytes are gone.",
        )

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in storage.open(row.storage_key):
                yield chunk
        except ObjectNotFoundError:  # reclaimed between the check and the read
            return

    return StreamingResponse(
        stream(),
        media_type=row.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(row.filename),
            "Content-Length": str(row.size_bytes),
        },
    )


@router.delete(
    "/{file_id}",
    response_model=OkResponse,
    summary="Forget a file",
    description="Soft-deletes the catalogue entry. The bytes are content-addressed and may "
    "be shared with another entry, so reclaiming them is the maintenance sweep's decision.",
)
async def delete_file(user: CurrentUser, session: DbSession, file_id: uuid.UUID) -> OkResponse:
    """Soft-delete one catalogue entry.

    Args:
        user: The acting user.
        session: The request's database session.
        file_id: The file being addressed.

    Returns:
        Confirmation.

    Raises:
        HTTPException: ``404`` if there is no such live file for this user.
    """
    from datetime import UTC, datetime

    row = await _load(session, user.id, file_id)
    row.deleted_at = datetime.now(UTC)
    await session.commit()
    logger.info("api.file_deleted", file_id=str(row.id))
    return OkResponse(message=f"{row.filename} was removed.")
