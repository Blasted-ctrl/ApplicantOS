"""Blob storage on the local filesystem — the default, and the private one.

``settings.storage_backend="local"`` is the shipping posture: the user's resumes, cover
letters and proof-of-submission screenshots stay on their own disk, under
``settings.storage_root``, with no bucket, no credentials, and nothing leaving the machine.
The S3 backend exists for a shared install; this one exists because the safety envelope
says the user's data stays local by default.

Layout is the key itself::

    <storage_root>/users/<user_id>/applications/<application_id>/resume.pdf
    <storage_root>/users/<user_id>/applications/<application_id>/screenshots/after.png

**Containment is enforced by resolution, not by string matching.** A key is validated by
:func:`~app.storage.base.key_segments`, joined onto the root, and then run through
:func:`os.path.realpath`; the result must sit under the realpath of the root or the write
is refused. Checking the resolved path is what catches the cases a substring check misses —
a symlinked component pointing out of the tree, a junction on Windows, a key whose parent
directory was replaced between two operations.

**Writes are atomic.** Bytes go to a uniquely-named temporary file in the destination
directory and are then moved into place with :func:`os.replace`, which is atomic on POSIX
and on Windows. A reader therefore sees either the previous object or the new one, never a
half-written resume — and a crash mid-write leaves a ``.tmp`` file, not a corrupt PDF.

All filesystem work runs in a worker thread via :func:`asyncio.to_thread`, so a slow disk
never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO, ClassVar, Final
from urllib.parse import quote

import structlog

from app.config.settings import get_settings
from app.storage.base import (
    COPY_CHUNK_BYTES,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_URL_TTL_SECONDS,
    KEY_SEPARATOR,
    ObjectNotFoundError,
    StorageError,
    StorageKeyError,
    StoredObject,
    key_segments,
    resolve_content_type,
    sha256_bytes,
)

__all__ = ["BACKEND_NAME", "DOWNLOAD_URL_PREFIX", "TEMP_SUFFIX", "LocalStorage"]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Value recorded in ``uploaded_files.backend`` for objects held here.
BACKEND_NAME: Final[str] = "local"

#: Path prefix of the API endpoint that serves these bytes. The desktop client never reads
#: ``storage_root`` directly — it fetches the same URL shape whichever backend is
#: configured, which is what lets the two installs share one :class:`ArtifactRead` schema.
DOWNLOAD_URL_PREFIX: Final[str] = "/api/v1/files"

#: Extension of an in-progress write, before :func:`os.replace` moves it into place.
TEMP_SUFFIX: Final[str] = ".tmp"

#: Separator between a destination filename and the random token of its temp file.
TEMP_TOKEN_SEPARATOR: Final[str] = "."


class LocalStorage:
    """A :class:`~app.storage.base.StorageBackend` rooted at a directory on this machine.

    Args:
        root: Directory to store objects under. Defaults to ``settings.storage_root``.
        download_prefix: Path prefix :meth:`url` builds download links from.

    Construction touches the filesystem only to resolve the root: directories are created
    on first write, so instantiating this in a test or a script costs nothing.
    """

    name: ClassVar[str] = BACKEND_NAME

    def __init__(
        self, root: Path | None = None, *, download_prefix: str = DOWNLOAD_URL_PREFIX
    ) -> None:
        base = Path(root) if root is not None else get_settings().storage_root
        # realpath, not resolve(strict=True): the root may not exist yet, and every
        # containment check below compares realpath against realpath so that a symlinked
        # component cannot make an escaping path look contained.
        self._root: Path = Path(os.path.realpath(base))
        self._download_prefix: str = download_prefix.rstrip(KEY_SEPARATOR)

    def __repr__(self) -> str:
        """Return a debug representation naming the root."""
        return f"{type(self).__name__}(root={str(self._root)!r})"

    @property
    def root(self) -> Path:
        """The resolved directory every object lives under."""
        return self._root

    # -- path resolution ------------------------------------------------------------------

    def path_for(self, key: str) -> Path:
        """Return the absolute path *key* maps to, without touching the filesystem.

        Backend-specific: a download route may hand this to a file response when
        ``settings.storage_backend == "local"``. Callers that must work with either backend
        use :meth:`open` instead.

        Args:
            key: The object's key.

        Returns:
            The absolute path, guaranteed to sit under :attr:`root`.

        Raises:
            StorageKeyError: If *key* is malformed or resolves outside the root.
        """
        candidate = self._root.joinpath(*key_segments(key))
        resolved = Path(os.path.realpath(candidate))
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            logger.warning("storage.local_escape_refused", key=key, resolved=str(resolved))
            raise StorageKeyError(f"key resolves outside the storage root: {key!r}") from exc
        return resolved

    # -- synchronous primitives, all run in a worker thread ------------------------------

    def _temp_path(self, path: Path) -> Path:
        """Return a unique sibling path to stage a write in.

        Args:
            path: The destination.

        Returns:
            A path in the same directory — which keeps :func:`os.replace` a rename rather
            than a cross-device copy.
        """
        token = uuid.uuid4().hex
        return path.with_name(f"{path.name}{TEMP_TOKEN_SEPARATOR}{token}{TEMP_SUFFIX}")

    def _write_bytes_sync(self, path: Path, data: bytes) -> int:
        """Write *data* to *path* atomically.

        Args:
            path: The destination.
            data: The bytes to write.

        Returns:
            The number of bytes written.

        Raises:
            StorageError: If the directory cannot be created or the write fails.
        """
        temporary = self._temp_path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(data)
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StorageError(f"could not write {path}: {exc}") from exc
        return len(data)

    def _copy_file_sync(self, source: Path, path: Path) -> tuple[int, str]:
        """Copy *source* to *path* atomically, hashing as it goes.

        One pass: the digest is computed from the bytes being written rather than by
        re-reading the finished object.

        Args:
            source: The existing file.
            path: The destination.

        Returns:
            ``(size_bytes, sha256_hex)``.

        Raises:
            ObjectNotFoundError: If *source* does not exist.
            StorageError: If the copy fails.
        """
        if not source.is_file():
            raise ObjectNotFoundError(f"source file does not exist: {source}")

        digest = hashlib.sha256()
        size = 0
        temporary = self._temp_path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, temporary.open("wb") as writer:
                while chunk := reader.read(COPY_CHUNK_BYTES):
                    writer.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StorageError(f"could not copy {source} to {path}: {exc}") from exc
        return size, digest.hexdigest()

    def _read_sync(self, path: Path, key: str) -> bytes:
        """Read the whole object at *path*.

        Args:
            path: The resolved path.
            key: The key, for the error message.

        Returns:
            The stored bytes.

        Raises:
            ObjectNotFoundError: If the file is absent.
            StorageError: If the read fails.
        """
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"no object stored under {key!r}") from exc
        except OSError as exc:
            raise StorageError(f"could not read {key!r}: {exc}") from exc

    def _open_sync(self, path: Path, key: str) -> BinaryIO:
        """Open the object at *path* for streaming.

        Args:
            path: The resolved path.
            key: The key, for the error message.

        Returns:
            An open binary handle the caller must close.

        Raises:
            ObjectNotFoundError: If the file is absent.
            StorageError: If it cannot be opened.
        """
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"no object stored under {key!r}") from exc
        except OSError as exc:
            raise StorageError(f"could not open {key!r}: {exc}") from exc

    def _delete_sync(self, path: Path, key: str) -> bool:
        """Delete the object at *path* and prune the directories it emptied.

        Args:
            path: The resolved path.
            key: The key, for the error message.

        Returns:
            ``True`` if a file was removed.

        Raises:
            StorageError: If the unlink fails for a reason other than absence.
        """
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageError(f"could not delete {key!r}: {exc}") from exc
        self._prune_parents(path.parent)
        return True

    def _prune_parents(self, directory: Path) -> None:
        """Remove now-empty directories between *directory* and the root.

        A per-application directory that outlives its files is clutter in a folder the user
        can open; a directory that still holds anything is left alone.

        Args:
            directory: Where to start walking up from.
        """
        current = directory
        while current != self._root and self._root in current.parents:
            try:
                current.rmdir()
            except OSError:
                # Not empty, or in use. Either way there is nothing further up to prune.
                return
            current = current.parent

    # -- the protocol ----------------------------------------------------------------------

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        """Write *data* under *key*, replacing any existing object.

        Args:
            key: Destination key.
            data: The bytes to store.
            content_type: MIME type to record. Inferred from *key* when omitted.

        Returns:
            The stored object's catalogue metadata.

        Raises:
            StorageKeyError: If *key* is malformed or escapes the root.
            StorageError: If the write fails.
        """
        path = self.path_for(key)
        size = await asyncio.to_thread(self._write_bytes_sync, path, data)
        stored = StoredObject(
            key=key,
            size_bytes=size,
            sha256=sha256_bytes(data),
            content_type=resolve_content_type(content_type, key),
            backend=self.name,
        )
        logger.debug("storage.local_put", key=key, size_bytes=size)
        return stored

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        """Copy the file at *source* to *key*, replacing any existing object.

        Args:
            key: Destination key.
            source: An existing local file.
            content_type: MIME type to record. Inferred from *source* then *key*.

        Returns:
            The stored object's catalogue metadata.

        Raises:
            StorageKeyError: If *key* is malformed or escapes the root.
            ObjectNotFoundError: If *source* does not exist.
            StorageError: If the copy fails.
        """
        path = self.path_for(key)
        size, digest = await asyncio.to_thread(self._copy_file_sync, Path(source), path)
        stored = StoredObject(
            key=key,
            size_bytes=size,
            sha256=digest,
            content_type=resolve_content_type(content_type, Path(source).name, key),
            backend=self.name,
        )
        logger.debug("storage.local_put_file", key=key, size_bytes=size, source=str(source))
        return stored

    async def get(self, key: str) -> bytes:
        """Return the whole object stored under *key*.

        Args:
            key: The object's key.

        Returns:
            The stored bytes.

        Raises:
            StorageKeyError: If *key* is malformed or escapes the root.
            ObjectNotFoundError: If nothing is stored under *key*.
            StorageError: If the read fails.
        """
        path = self.path_for(key)
        return await asyncio.to_thread(self._read_sync, path, key)

    def open(self, key: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Stream the object under *key* in chunks.

        The key is validated eagerly; the file is opened on first iteration, so a missing
        object surfaces as :class:`ObjectNotFoundError` from the first ``anext``.

        Args:
            key: The object's key.
            chunk_size: Bytes to yield per iteration.

        Returns:
            An async iterator over the object's bytes.

        Raises:
            StorageKeyError: If *key* is malformed or escapes the root.
        """
        return self._stream(self.path_for(key), key, max(1, chunk_size))

    async def _stream(self, path: Path, key: str, chunk_size: int) -> AsyncIterator[bytes]:
        """Yield the file at *path* one chunk at a time, off the event loop.

        Args:
            path: The resolved path.
            key: The key, for the error message.
            chunk_size: Bytes per chunk.

        Yields:
            Successive chunks, the last one possibly short.

        Raises:
            ObjectNotFoundError: If the file is absent.
            StorageError: If a read fails.
        """
        handle = await asyncio.to_thread(self._open_sync, path, key)
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(handle.read, chunk_size)
                except OSError as exc:
                    raise StorageError(f"could not read {key!r}: {exc}") from exc
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def delete(self, key: str) -> bool:
        """Delete the object under *key*.

        Args:
            key: The object's key.

        Returns:
            ``True`` if an object was deleted, ``False`` if there was nothing there.

        Raises:
            StorageKeyError: If *key* is malformed or escapes the root.
            StorageError: If the unlink fails.
        """
        path = self.path_for(key)
        removed = await asyncio.to_thread(self._delete_sync, path, key)
        logger.debug("storage.local_delete", key=key, removed=removed)
        return removed

    async def exists(self, key: str) -> bool:
        """Return whether an object is stored under *key*.

        Args:
            key: The object's key.

        Returns:
            ``True`` when the file is there.

        Raises:
            StorageKeyError: If *key* is malformed or escapes the root.
        """
        path = self.path_for(key)
        return await asyncio.to_thread(path.is_file)

    async def url(
        self,
        key: str,
        *,
        expires_in: int = DEFAULT_URL_TTL_SECONDS,
        filename: str | None = None,
    ) -> str:
        """Return the API download path for *key*.

        Nothing is signed: these bytes are served by the local API to the desktop client
        over loopback, so the link is a path rather than a credential.

        Args:
            key: The object's key.
            expires_in: Ignored — a local link does not expire.
            filename: Name to suggest to the browser, appended as a query parameter for the
                download route to turn into a ``Content-Disposition`` header.

        Returns:
            A root-relative URL under :data:`DOWNLOAD_URL_PREFIX`.

        Raises:
            StorageKeyError: If *key* is malformed or escapes the root.
        """
        del expires_in  # Local links are not signed and never expire.
        self.path_for(key)  # Validate before handing a path back to a client.
        link = f"{self._download_prefix}/{quote(key, safe=KEY_SEPARATOR)}"
        if filename:
            link = f"{link}?filename={quote(filename, safe='')}"
        return link
