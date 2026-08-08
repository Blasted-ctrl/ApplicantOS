"""Blob storage — where every byte this system produces or ingests actually lives.

Rendered resumes and cover letters, uploaded source documents, and the proof-of-submission
screenshots that make "did this really get sent?" answerable: all of them are objects in a
backend, catalogued by :class:`~app.models.file.UploadedFile`. This package owns the bytes;
that table owns the record.

Use the package, not the modules::

    from app.storage import build_key, get_storage

    storage = get_storage()
    key = build_key("users", user_id, "applications", application_id, "resume", ext="pdf")
    stored = await storage.put_file(key, rendered_pdf)     # -> StoredObject
    link = await storage.url(stored.key, filename="resume.pdf")

**Backend selection** follows ``settings.storage_backend``:

===========  ==========================================================================
``local``    :class:`~app.storage.local.LocalStorage` under ``settings.storage_root`` —
             nothing to install, nothing leaves the machine. The default, and what the
             safety envelope means by "the user's data stays local".
``s3``       :class:`~app.storage.s3.S3Storage` against ``settings.s3_bucket``, honouring
             ``settings.s3_endpoint_url`` for MinIO. Requires ``aioboto3`` or ``boto3``,
             both imported lazily.
===========  ==========================================================================

Both backends implement the same :class:`~app.storage.base.StorageBackend` protocol and
speak the same POSIX-style keys, so a caller never learns which one is configured — and an
install can move from a directory to a bucket by copying a tree and changing one setting.

Keys are built by :func:`~app.storage.base.build_key`, which slugifies its components and
**rejects** ``..``, absolute paths, drive letters and NUL bytes rather than repairing them.
That matters because key components are user-influenced — an uploaded filename, a company
name — and the local backend turns them into filesystem paths.
"""

from __future__ import annotations

from functools import lru_cache

import structlog

from app.config.settings import Settings, get_settings
from app.storage.base import (
    CONTENT_TYPES,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONTENT_TYPE,
    DEFAULT_URL_TTL_SECONDS,
    KEY_MAX_LENGTH,
    KEY_SEPARATOR,
    MAX_SEGMENT_LENGTH,
    ObjectNotFoundError,
    StorageBackend,
    StorageError,
    StorageKeyError,
    StoredObject,
    build_key,
    guess_content_type,
    key_segments,
    normalize_key,
    resolve_content_type,
    sha256_bytes,
    sha256_file,
)
from app.storage.local import DOWNLOAD_URL_PREFIX, LocalStorage
from app.storage.s3 import S3Storage

__all__ = [
    "CONTENT_TYPES",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_CONTENT_TYPE",
    "DEFAULT_URL_TTL_SECONDS",
    "DOWNLOAD_URL_PREFIX",
    "KEY_MAX_LENGTH",
    "KEY_SEPARATOR",
    "MAX_SEGMENT_LENGTH",
    "LocalStorage",
    "ObjectNotFoundError",
    "S3Storage",
    "StorageBackend",
    "StorageError",
    "StorageKeyError",
    "StoredObject",
    "build_key",
    "build_storage",
    "get_storage",
    "guess_content_type",
    "key_segments",
    "normalize_key",
    "reset_storage",
    "resolve_content_type",
    "sha256_bytes",
    "sha256_file",
]

logger = structlog.get_logger(__name__)


def build_storage(settings: Settings) -> StorageBackend:
    """Construct the storage backend described by *settings*, without memoising it.

    Args:
        settings: Configuration supplying ``storage_backend``, ``storage_root`` and the
            ``s3_*`` / ``aws_*`` fields.

    Returns:
        A ready-to-use backend. Construction is cheap and offline: the local backend only
        resolves its root, and the S3 backend defers both its SDK import and its first
        network call.

    Raises:
        ValueError: If ``storage_backend`` is neither ``local`` nor ``s3``. Settings
            validation normally catches this first.
        StorageError: If ``storage_backend="s3"`` and no bucket is configured.
    """
    backend = settings.storage_backend

    if backend == "local":
        storage: StorageBackend = LocalStorage(settings.storage_root)
    elif backend == "s3":
        storage = S3Storage(
            settings.s3_bucket,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.aws_access_key_id,
            secret_access_key=settings.aws_secret_access_key,
        )
    else:  # pragma: no cover - unreachable while Settings validates the literal
        raise ValueError(f"unknown storage backend: {backend!r}")

    logger.debug("storage.configured", backend=backend)
    return storage


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    """Return the process-wide storage backend singleton.

    Memoised, so every caller shares one client and one resolved root. Construction is lazy
    and never connects eagerly: importing this module touches neither the filesystem nor a
    bucket.

    Returns:
        The configured :class:`~app.storage.base.StorageBackend`.
    """
    return build_storage(get_settings())


def reset_storage() -> None:
    """Discard the memoised backend so the next call rebuilds it.

    Intended for tests that change ``storage_backend`` and for a clean shutdown path. Any
    backend holding a client should be closed by the caller — ``await storage.close()`` on
    :class:`~app.storage.s3.S3Storage` — because this function only drops the reference.
    """
    get_storage.cache_clear()
