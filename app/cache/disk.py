"""Content-addressed cache on the local filesystem.

:class:`DiskCache` is the durable tier for an install without Redis — which includes the
lightweight ``sqlite_mode`` deployment (``settings.sqlite_mode`` forces
``cache_backend="disk"``). It survives restarts, which is the whole point: an LLM response
or a parsed resume paid for yesterday should not be paid for again today.

Layout under ``settings.cache_path``::

    <root>/<namespace>/<shard>/<token>.json    the serialised value
    <root>/<namespace>/<shard>/<token>.meta    expiry and provenance sidecar

``token`` is the SHA-256 of the cache key and ``shard`` is its first two characters, which
fans entries across 256 directories per namespace — enough that no directory grows to the
size where filesystem lookups degrade, while keeping the tree shallow. Deriving the token
by hashing means an arbitrary key string is always a legal filename on every platform, and
the namespace directory means :meth:`DiskCache.clear` is a bounded walk rather than a scan
of the whole tree.

Writes are atomic: the payload goes to a uniquely-named temporary file in the destination
directory and is then moved into place with :func:`os.replace`, which is atomic on POSIX
and on Windows. The value file is written before its sidecar, so a crash between the two
leaves an orphan value (invisible, later swept) rather than a sidecar promising a value
that is not there. Files are not ``fsync``-ed — this is a cache, and a torn write after a
power loss is indistinguishable from a miss because every read tolerates corruption by
discarding the entry.

All filesystem work runs in a worker thread via :func:`asyncio.to_thread`, so a slow disk
never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final

import structlog

from app.cache.base import BaseCache, deserialize, serialize
from app.cache.keys import namespace_of, normalize_namespace
from app.config.settings import get_settings

__all__ = ["META_SUFFIX", "SHARD_LENGTH", "VALUE_SUFFIX", "DiskCache"]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Extension of the file holding the serialised value.
VALUE_SUFFIX: Final[str] = ".json"

#: Extension of the sidecar holding the expiry deadline and provenance.
META_SUFFIX: Final[str] = ".meta"

#: Extension of an in-progress write, before :func:`os.replace` moves it into place.
TEMP_SUFFIX: Final[str] = ".tmp"

#: Number of leading token characters used as the shard directory name. Two hex characters
#: give 256 shards per namespace.
SHARD_LENGTH: Final[int] = 2

#: Sidecar field names. Named constants because the sidecar is read by the maintenance
#: task as well as by this module.
META_KEY: Final[str] = "key"
META_NAMESPACE: Final[str] = "namespace"
META_EXPIRES_AT: Final[str] = "expires_at"
META_CREATED_AT: Final[str] = "created_at"
META_CONTENT_HASH: Final[str] = "content_hash"

#: Sentinel distinguishing "no entry" from "an entry whose value happens to be ``None``".
_MISSING: Final[object] = object()


@dataclass(slots=True)
class _EntryPaths:
    """The pair of files backing one cache entry.

    Attributes:
        value: Path of the serialised value.
        meta: Path of the expiry sidecar.
    """

    value: Path
    meta: Path

    @property
    def directory(self) -> Path:
        """The shard directory containing both files."""
        return self.value.parent


class DiskCache(BaseCache):
    """A durable, content-addressed cache stored as files under a root directory.

    Values are serialised to JSON (see :func:`app.cache.base.serialize`), so they come back
    in their JSON form: dictionaries and lists round-trip exactly, while pydantic models
    and dataclasses come back as dictionaries and must be re-validated by the caller.
    """

    backend_name: ClassVar[str] = "disk"

    def __init__(self, root: Path | None = None, *, default_ttl: int | None = None) -> None:
        """Create a disk cache rooted at *root*.

        Args:
            root: Directory holding the cache tree. Defaults to ``settings.cache_path``,
                which is created on first access.
            default_ttl: Lifetime applied when a caller passes no explicit ``ttl``.
        """
        super().__init__(default_ttl=default_ttl)
        self._root = Path(root) if root is not None else get_settings().cache_path

    # -- introspection --------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The directory holding this cache's tree."""
        return self._root

    def __repr__(self) -> str:
        """Return a description including the cache root."""
        return f"<DiskCache root={self._root}>"

    # -- path derivation -------------------------------------------------------------------

    def _paths_for(self, key: str) -> _EntryPaths:
        """Map a cache key onto its value and sidecar paths.

        The token is the SHA-256 of the whole key rather than the digest embedded in it,
        which makes the mapping total: any string a caller passes — including one that
        never went through :func:`app.cache.keys.make_key` — becomes a valid, collision-
        resistant filename.

        Args:
            key: The cache key.

        Returns:
            The paths of the value file and its sidecar.
        """
        token = hashlib.sha256(key.encode("utf-8")).hexdigest()
        directory = self._root / namespace_of(key) / token[:SHARD_LENGTH]
        return _EntryPaths(
            value=directory / f"{token}{VALUE_SUFFIX}",
            meta=directory / f"{token}{META_SUFFIX}",
        )

    def _namespace_root(self, namespace: str | None) -> Path:
        """Return the directory holding *namespace*, or the whole tree when it is ``None``.

        Args:
            namespace: The namespace to scope to, or ``None``.

        Returns:
            A directory inside the cache root. Normalisation strips path separators and
            leading dots, so a namespace can never escape the root.
        """
        if namespace is None:
            return self._root
        return self._root / normalize_namespace(namespace)

    # -- synchronous filesystem primitives ---------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        """Write *payload* to *path* atomically.

        The bytes land in a uniquely-named temporary file in the same directory — same
        filesystem, so the move is a rename — and :func:`os.replace` then swaps it into
        place. A reader therefore sees either the previous content or the new content, and
        never a partially written file.

        Args:
            path: Final destination.
            payload: Bytes to write.
        """
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}{TEMP_SUFFIX}")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        finally:
            # Reached only when the write or the replace failed; the temporary file must
            # not be left behind to accumulate.
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _remove(paths: _EntryPaths) -> None:
        """Delete both files of an entry, ignoring anything already gone."""
        paths.value.unlink(missing_ok=True)
        paths.meta.unlink(missing_ok=True)

    def _read_sync(self, key: str) -> Any:
        """Read one entry, returning :data:`_MISSING` when there is nothing live to read.

        The sidecar is consulted first so an expired entry costs one small read instead of
        loading a payload that is about to be discarded. Corrupt or truncated files are
        treated as misses and removed, which is how the cache self-heals after a crash.

        Args:
            key: The cache key.

        Returns:
            The stored value, or :data:`_MISSING`.
        """
        paths = self._paths_for(key)
        try:
            meta_raw = paths.meta.read_bytes()
        except FileNotFoundError:
            return _MISSING
        except OSError as exc:
            logger.warning("cache.disk_meta_unreadable", path=str(paths.meta), error=str(exc))
            return _MISSING

        try:
            meta = deserialize(meta_raw)
        except ValueError:
            logger.debug("cache.disk_meta_corrupt", path=str(paths.meta))
            self._remove(paths)
            return _MISSING

        if not isinstance(meta, dict):
            self._remove(paths)
            return _MISSING

        expires_at = meta.get(META_EXPIRES_AT)
        if isinstance(expires_at, (int, float)) and expires_at <= time.time():
            self._remove(paths)
            return _MISSING

        try:
            value_raw = paths.value.read_bytes()
        except FileNotFoundError:
            # Sidecar without a payload: a write that was interrupted. Clean it up.
            self._remove(paths)
            return _MISSING
        except OSError as exc:
            logger.warning("cache.disk_value_unreadable", path=str(paths.value), error=str(exc))
            return _MISSING

        try:
            return deserialize(value_raw)
        except ValueError:
            logger.debug("cache.disk_value_corrupt", path=str(paths.value))
            self._remove(paths)
            return _MISSING

    def _write_sync(self, key: str, value: Any, ttl: int | None) -> None:
        """Serialise and store one entry, value file first, then its sidecar.

        Args:
            key: The cache key.
            value: The value to store.
            ttl: Already-resolved lifetime in seconds, or ``None`` for no expiry.
        """
        paths = self._paths_for(key)
        paths.directory.mkdir(parents=True, exist_ok=True)

        payload = serialize(value)
        now = time.time()
        meta = {
            META_KEY: key,
            META_NAMESPACE: namespace_of(key),
            META_EXPIRES_AT: None if ttl is None else now + ttl,
            META_CREATED_AT: now,
            META_CONTENT_HASH: hashlib.sha256(payload).hexdigest(),
        }

        self._atomic_write(paths.value, payload)
        self._atomic_write(paths.meta, serialize(meta))

    def _delete_sync(self, key: str) -> bool:
        """Remove one entry.

        Args:
            key: The cache key.

        Returns:
            ``True`` when something was actually removed.
        """
        paths = self._paths_for(key)
        existed = paths.meta.exists() or paths.value.exists()
        self._remove(paths)
        return existed

    def _exists_sync(self, key: str) -> bool:
        """Return whether one entry exists and has not expired, removing it if it has."""
        paths = self._paths_for(key)
        try:
            meta_raw = paths.meta.read_bytes()
        except (FileNotFoundError, OSError):
            return False
        try:
            meta = deserialize(meta_raw)
        except ValueError:
            self._remove(paths)
            return False
        if not isinstance(meta, dict):
            self._remove(paths)
            return False
        expires_at = meta.get(META_EXPIRES_AT)
        if isinstance(expires_at, (int, float)) and expires_at <= time.time():
            self._remove(paths)
            return False
        return paths.value.exists()

    def _ttl_of_sync(self, key: str) -> int | None:
        """Read one entry's deadline out of its sidecar and turn it into seconds left.

        Only the sidecar is touched — the payload is irrelevant to a deadline — so this is
        one small read. Unlike :meth:`_read_sync`, a dead or unreadable entry is *not*
        removed here: this is a read-only probe on a path that has usually just loaded the
        value, and deleting under it would be a surprise.

        Args:
            key: The cache key.

        Returns:
            Seconds remaining, or ``None`` when the entry has no deadline, is absent,
            expired, or unreadable.
        """
        paths = self._paths_for(key)
        try:
            meta = deserialize(paths.meta.read_bytes())
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        expires_at = meta.get(META_EXPIRES_AT)
        if not isinstance(expires_at, (int, float)):
            return None
        remaining = expires_at - time.time()
        if remaining <= 0:
            return None
        return max(1, int(remaining))

    def _iter_shards(self, namespace: str | None) -> list[Path]:
        """List every shard directory in scope.

        Args:
            namespace: Restrict to one namespace, or ``None`` to walk them all.

        Returns:
            The shard directories that exist. Missing directories yield an empty list
            rather than an error — an unused namespace is not a failure.
        """
        namespace_roots: list[Path]
        if namespace is None:
            try:
                namespace_roots = [entry for entry in self._root.iterdir() if entry.is_dir()]
            except (FileNotFoundError, NotADirectoryError, OSError):
                return []
        else:
            candidate = self._namespace_root(namespace)
            namespace_roots = [candidate] if candidate.is_dir() else []

        shards: list[Path] = []
        for namespace_root in namespace_roots:
            try:
                shards.extend(entry for entry in namespace_root.iterdir() if entry.is_dir())
            except OSError as exc:
                logger.warning(
                    "cache.disk_shard_scan_failed", path=str(namespace_root), error=str(exc)
                )
        return shards

    def _clear_sync(self, namespace: str | None) -> int:
        """Delete every entry in scope by walking the shard directories.

        Args:
            namespace: Restrict to one namespace, or ``None`` for the whole tree.

        Returns:
            The number of entries removed, counted by sidecar.
        """
        removed = 0
        for shard in self._iter_shards(namespace):
            try:
                entries = list(shard.iterdir())
            except OSError as exc:
                logger.warning("cache.disk_shard_unreadable", path=str(shard), error=str(exc))
                continue
            for entry in entries:
                if entry.suffix == META_SUFFIX:
                    removed += 1
                if entry.suffix in (META_SUFFIX, VALUE_SUFFIX) or entry.name.endswith(TEMP_SUFFIX):
                    entry.unlink(missing_ok=True)
            self._remove_if_empty(shard)
        return removed

    def _purge_expired_sync(self) -> int:
        """Delete every entry whose deadline has passed, across every namespace.

        Returns:
            The number of entries removed.
        """
        now = time.time()
        removed = 0
        for shard in self._iter_shards(None):
            try:
                sidecars = [entry for entry in shard.iterdir() if entry.suffix == META_SUFFIX]
            except OSError as exc:
                logger.warning("cache.disk_shard_unreadable", path=str(shard), error=str(exc))
                continue
            for sidecar in sidecars:
                try:
                    meta = deserialize(sidecar.read_bytes())
                except (OSError, ValueError):
                    meta = None
                expires_at = meta.get(META_EXPIRES_AT) if isinstance(meta, dict) else 0
                is_dead = meta is None or (
                    isinstance(expires_at, (int, float)) and expires_at <= now
                )
                if not is_dead:
                    continue
                sidecar.unlink(missing_ok=True)
                sidecar.with_suffix(VALUE_SUFFIX).unlink(missing_ok=True)
                removed += 1
            self._remove_if_empty(shard)
        return removed

    @staticmethod
    def _remove_if_empty(directory: Path) -> None:
        """Remove *directory* when nothing is left in it, ignoring races and failures."""
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
            except OSError:
                # Another worker recreated or is writing into it; harmless.
                return
        except OSError:
            return

    # -- storage primitives -------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        """Return the value stored under *key*.

        Args:
            key: The cache key.

        Returns:
            The stored value, or ``None`` when absent, expired, or unreadable.
        """
        result = await asyncio.to_thread(self._read_sync, key)
        if result is _MISSING:
            self._record_miss(key)
            return None
        self._record_hit(key)
        return result

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """Store *value* under *key*.

        Args:
            key: The cache key.
            value: The value to store; serialised to JSON.
            ttl: Lifetime in seconds. ``None`` uses the backend default; a non-positive
                value stores the entry without expiry.
        """
        await asyncio.to_thread(self._write_sync, key, value, self._ttl(ttl))
        self._record_set(key)

    async def delete(self, key: str) -> None:
        """Remove *key* from disk.

        Args:
            key: The cache key. Deleting an absent key is not an error.
        """
        if await asyncio.to_thread(self._delete_sync, key):
            self._record_delete(key)

    async def exists(self, key: str) -> bool:
        """Return whether *key* holds a live entry on disk.

        Args:
            key: The cache key.

        Returns:
            ``True`` when both files are present and the entry has not expired.
        """
        return await asyncio.to_thread(self._exists_sync, key)

    async def clear(self, namespace: str | None = None) -> int:
        """Delete every entry in *namespace* by walking its shards.

        Args:
            namespace: The namespace to drop, or ``None`` for the entire cache tree.

        Returns:
            The number of entries removed.
        """
        removed = await asyncio.to_thread(self._clear_sync, namespace)
        self._record_clear(namespace, removed)
        return removed

    async def ttl_of(self, key: str) -> int | None:
        """Return how many whole seconds *key* has left, read from its sidecar.

        Lets :class:`~app.cache.base.TieredCache` bound a promoted front-tier copy by this
        entry's real deadline instead of a fixed window.

        Args:
            key: The cache key.

        Returns:
            A positive number of seconds, or ``None`` when the entry never expires, is
            absent, expired, or unreadable.
        """
        return await asyncio.to_thread(self._ttl_of_sync, key)

    async def purge_expired(self) -> int:
        """Delete every expired entry across every namespace.

        Backs the ``cleanup.prune_cache`` maintenance task: disk entries expire lazily on
        read, so a namespace that stops being read would otherwise retain dead files
        forever.

        Returns:
            The number of entries removed.
        """
        removed = await asyncio.to_thread(self._purge_expired_sync)
        if removed:
            self._stats.record_eviction(removed)
            logger.info("cache.disk_purged", removed=removed, root=str(self._root))
        return removed
