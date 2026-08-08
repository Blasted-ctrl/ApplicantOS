"""In-process LRU cache — the fastest tier, and the only one with no dependencies.

:class:`MemoryCache` is a bounded least-recently-used store with per-entry TTL. It backs
three situations:

* the ``memory`` cache backend, for tests and for a install with no Redis and no disk;
* the front tier of every :class:`~app.cache.base.TieredCache`, absorbing the repeated
  reads that dominate a pipeline run;
* any component that wants a private, disposable cache of its own.

Design constraints and how they are met:

* **O(1) reads and writes.** Entries live in an :class:`~collections.OrderedDict`; a hit
  moves the key to the end, and eviction pops from the front.
* **Bounded memory.** ``max_entries`` caps the map. When it overflows, expired entries are
  swept first (occasionally, so the sweep stays amortised) and only then is the true
  least-recently-used entry evicted.
* **No background task.** Expiry is lazy: an entry is only recognised as dead when it is
  read, or when a sweep runs during overflow. There is no timer thread and nothing to
  shut down, which is what makes this cache safe to construct in a Celery worker, a test,
  or the FastAPI process without lifecycle wiring. :meth:`MemoryCache.purge_expired` is
  available for the ``cleanup.prune_cache`` maintenance task.

Expiry deadlines use :func:`time.monotonic`, so a system clock adjustment can never make a
live entry look expired or vice versa.

All state mutation happens synchronously inside the ``async def`` methods, with no
``await`` in between, so operations are atomic with respect to the event loop and need no
lock.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from app.cache.base import BaseCache

__all__ = ["DEFAULT_MAX_ENTRIES", "MemoryCache"]

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Default capacity. Sized for the front tier of a pipeline run — thousands of postings,
#: scores and embeddings — while staying comfortably inside a desktop memory budget.
DEFAULT_MAX_ENTRIES: Final[int] = 4096

#: How many writes may pass between opportunistic expiry sweeps while the cache is full.
#: A sweep is O(n); spacing it out keeps :meth:`MemoryCache.set` amortised O(1).
PURGE_INTERVAL_SETS: Final[int] = 256


@dataclass(slots=True)
class _MemoryEntry:
    """One cached value and its expiry deadline.

    Attributes:
        value: The stored object, kept as-is — the memory backend does not serialise.
        expires_at: Monotonic deadline after which the entry is dead, or ``None`` when the
            entry never expires.
    """

    value: Any
    expires_at: float | None

    def is_expired(self, now: float) -> bool:
        """Return whether this entry is dead as of the monotonic timestamp *now*."""
        return self.expires_at is not None and self.expires_at <= now


class MemoryCache(BaseCache):
    """A bounded, TTL-aware, least-recently-used cache held entirely in this process.

    Values are stored by reference and returned by reference: unlike the disk and Redis
    backends, nothing is serialised, so a caller receives the very object it stored.
    Mutating a value obtained from this cache therefore mutates the cached entry — treat
    everything read out of a cache as immutable.
    """

    backend_name: ClassVar[str] = "memory"

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        default_ttl: int | None = None,
    ) -> None:
        """Create an empty cache.

        Args:
            max_entries: Maximum number of live entries before least-recently-used
                eviction begins. Must be positive.
            default_ttl: Lifetime applied when a caller passes no explicit ``ttl``.

        Raises:
            ValueError: If *max_entries* is not positive.
        """
        super().__init__(default_ttl=default_ttl)
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _MemoryEntry] = OrderedDict()
        self._sets_since_purge = 0

    # -- introspection -------------------------------------------------------------------

    @property
    def max_entries(self) -> int:
        """The capacity at which eviction begins."""
        return self._max_entries

    def __len__(self) -> int:
        """Return the number of stored entries, including any not yet swept expired ones."""
        return len(self._entries)

    def __repr__(self) -> str:
        """Return a description including occupancy."""
        return f"<MemoryCache entries={len(self._entries)}/{self._max_entries}>"

    # -- storage primitives ----------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        """Return the live value for *key*, refreshing its position in the LRU order.

        Args:
            key: The cache key.

        Returns:
            The stored value, or ``None`` when the key is absent or its entry has expired.
            An expired entry is dropped on the spot — this is the lazy expiry path.
        """
        entry = self._entries.get(key)
        if entry is None:
            self._record_miss(key)
            return None

        if entry.is_expired(time.monotonic()):
            del self._entries[key]
            self._record_eviction(key)
            self._record_miss(key)
            return None

        self._entries.move_to_end(key)
        self._record_hit(key)
        return entry.value

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """Store *value* under *key* as the most recently used entry.

        Args:
            key: The cache key.
            value: The value to store, by reference.
            ttl: Lifetime in seconds; ``None`` uses the backend default and a non-positive
                value stores the entry without expiry.
        """
        effective_ttl = self._ttl(ttl)
        expires_at = None if effective_ttl is None else time.monotonic() + effective_ttl
        self._entries[key] = _MemoryEntry(value=value, expires_at=expires_at)
        self._entries.move_to_end(key)
        self._record_set(key)
        self._sets_since_purge += 1
        self._enforce_capacity()

    async def delete(self, key: str) -> None:
        """Remove *key* if it is present.

        Args:
            key: The cache key. Deleting an absent key is not an error.
        """
        if self._entries.pop(key, None) is not None:
            self._record_delete(key)

    async def exists(self, key: str) -> bool:
        """Return whether *key* holds a live entry, without affecting LRU order or stats.

        Args:
            key: The cache key.

        Returns:
            ``True`` when the entry exists and has not expired. An expired entry found
            here is dropped.
        """
        entry = self._entries.get(key)
        if entry is None:
            return False
        if entry.is_expired(time.monotonic()):
            del self._entries[key]
            self._record_eviction(key)
            return False
        return True

    async def clear(self, namespace: str | None = None) -> int:
        """Drop every entry in *namespace*, or the whole cache.

        Args:
            namespace: The namespace to drop, or ``None`` for everything.

        Returns:
            The number of entries removed.
        """
        if namespace is None:
            removed = len(self._entries)
            self._entries.clear()
        else:
            prefix = self._namespace_prefix(namespace)
            doomed = [key for key in self._entries if key.startswith(prefix)]
            for key in doomed:
                del self._entries[key]
            removed = len(doomed)
        self._record_clear(namespace, removed)
        return removed

    # -- optional capabilities ---------------------------------------------------------------

    async def ttl_of(self, key: str) -> int | None:
        """Return how many whole seconds *key* has left, without touching LRU order.

        Args:
            key: The cache key.

        Returns:
            A positive number of seconds when the entry is live and has a deadline;
            ``None`` when it never expires, is absent, or has already expired. The value is
            truncated rather than rounded up, so it is never an over-estimate.
        """
        entry = self._entries.get(key)
        if entry is None or entry.expires_at is None:
            return None
        remaining = entry.expires_at - time.monotonic()
        if remaining <= 0:
            return None
        return max(1, int(remaining))

    # -- maintenance ------------------------------------------------------------------------

    def purge_expired(self) -> int:
        """Drop every entry whose TTL has elapsed.

        Called opportunistically when the cache is full, and available to the
        ``cleanup.prune_cache`` maintenance task. Cheaper than eviction in the sense that
        it only removes entries nobody could have read anyway.

        Returns:
            The number of entries removed.
        """
        now = time.monotonic()
        doomed = [key for key, entry in self._entries.items() if entry.is_expired(now)]
        for key in doomed:
            del self._entries[key]
        if doomed:
            self._record_eviction(doomed[0], len(doomed))
        self._sets_since_purge = 0
        return len(doomed)

    def _enforce_capacity(self) -> None:
        """Evict until the cache is back within ``max_entries``.

        Expired entries are swept first — but only every :data:`PURGE_INTERVAL_SETS`
        writes, so that a permanently full cache does not pay an O(n) scan per write.
        Whatever overflow remains is resolved by evicting the least recently used entry.
        """
        if len(self._entries) <= self._max_entries:
            return

        if self._sets_since_purge >= PURGE_INTERVAL_SETS:
            self.purge_expired()

        while len(self._entries) > self._max_entries:
            evicted_key, _ = self._entries.popitem(last=False)
            self._record_eviction(evicted_key)
