"""Cache abstractions — the :class:`Cache` protocol, shared machinery, and tiering.

Every backend in :mod:`app.cache` is a :class:`Cache`: a small async key/value store with
a namespace-aware :meth:`Cache.clear` and a stampede-safe :meth:`Cache.get_or_set`. This
module owns everything the backends share so no behaviour is written twice:

* :class:`Cache` — the structural type callers depend on (``docs/CONTRACTS.md`` §7). Depend
  on this, never on a concrete backend.
* :class:`CacheStats` — hit/miss/set/eviction counters, tracked by every backend.
* :class:`BaseCache` — the abstract base implementing :meth:`~BaseCache.get_or_set`,
  :meth:`~BaseCache.namespace_of`, TTL resolution, statistics, and metric emission.
* :class:`TieredCache` — a read-through composition of a fast front tier (memory) over a
  durable backing tier (disk or Redis).
* :func:`serialize` / :func:`deserialize` — the value codec used by the disk and Redis
  backends, ``orjson`` when available and :mod:`json` otherwise.

**Stampede protection.** :meth:`BaseCache.get_or_set` takes a per-key :class:`asyncio.Lock`
so that N concurrent callers asking for the same uncached value produce one call to the
factory, not N. This matters most for the LLM namespace, where the factory costs money.
The lock pool is keyed by the running event loop as well as the key, because a Celery
worker creates a fresh loop per task while the cache singleton outlives all of them.

**Metrics.** Every operation emits ``applicantos_cache_events_total{namespace,event}`` when
:mod:`app.observability.metrics` is importable. The import is optional so that this package
stands alone — in a test, a script, or before the observability module exists.

**Null values.** The protocol returns ``Any | None`` and therefore cannot distinguish a
cached ``None`` from a miss. Storing ``None`` is allowed but is re-computed on every read.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

import structlog

from app.cache.keys import (
    KEY_SEPARATOR,
    normalize_namespace,
    to_jsonable,
)
from app.cache.keys import (
    namespace_of as _namespace_of,
)

__all__ = [
    "BaseCache",
    "Cache",
    "CacheEvent",
    "CacheStats",
    "TieredCache",
    "deserialize",
    "emit_cache_event",
    "resolve_ttl",
    "serialize",
]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Metric label used for an operation that spans every namespace (``clear(None)``).
ALL_NAMESPACES_LABEL: Final[str] = "all"

#: Attribute names searched on ``app.observability.metrics`` for the cache-event counter,
#: in priority order, before giving up and disabling metric emission.
METRIC_COUNTER_ATTRIBUTES: Final[tuple[str, ...]] = (
    "CACHE_EVENTS",
    "cache_events",
    "applicantos_cache_events_total",
)

#: Name of an optional module-level helper on ``app.observability.metrics`` that records a
#: cache event directly. Preferred over poking at a counter object when it exists.
METRIC_RECORDER_ATTRIBUTE: Final[str] = "record_cache_event"

#: Upper bound on the lifetime of an entry promoted into the front tier of a
#: :class:`TieredCache`. A promotion is *additionally* clamped to whatever is really left
#: of the backing entry's lifetime (see :meth:`BaseCache.ttl_of`), so this value only
#: bounds how long a front copy may survive a ``delete`` performed by another process.
TIERED_FRONT_TTL_SECONDS: Final[int] = 300

#: ``orjson`` if it is installed, otherwise ``None`` and the stdlib codec is used. Values
#: are stored as JSON either way, so the two are wire-compatible.
_orjson: ModuleType | None
try:  # pragma: no cover - trivial import guard
    import orjson as _orjson_module
except ImportError:  # pragma: no cover - orjson is an optional accelerator
    _orjson = None
else:  # pragma: no cover - trivial import guard
    _orjson = _orjson_module


class CacheEvent:
    """The event names reported on ``applicantos_cache_events_total``.

    ``HIT`` / ``MISS``
        A read that found, or did not find, a live entry.
    ``SET``
        A value was written.
    ``DELETE``
        A single key was removed by an explicit request.
    ``CLEAR``
        A namespace (or the whole cache) was dropped.
    ``EVICTION``
        An entry was removed by the cache itself — LRU pressure or TTL expiry.
    ``ERROR``
        A backend operation failed and was degraded to a miss instead of raising.
    """

    __slots__ = ()

    HIT: Final[str] = "hit"
    MISS: Final[str] = "miss"
    SET: Final[str] = "set"
    DELETE: Final[str] = "delete"
    CLEAR: Final[str] = "clear"
    EVICTION: Final[str] = "eviction"
    ERROR: Final[str] = "error"


# --------------------------------------------------------------------------------------
# Value codec
# --------------------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Convert an object the JSON encoder does not understand.

    Args:
        value: The unserialisable object encountered mid-encode.

    Returns:
        JSON-native data produced by :func:`app.cache.keys.to_jsonable`. Because that
        function is total, this hook never raises and serialisation never fails.
    """
    return to_jsonable(value)


def serialize(value: Any) -> bytes:
    """Encode a cache value for storage in a byte-oriented backend.

    Values are stored as JSON. Objects that are not JSON-native — pydantic models,
    dataclasses, enums, ``Path``, ``UUID``, ``datetime``, ``Decimal``, byte strings — are
    converted by :func:`app.cache.keys.to_jsonable`, which means **they are read back in
    their JSON form, not as the original type**. Callers that cache a model must
    re-validate it on read; callers that cache primitives, lists, and dictionaries get
    exactly what they stored.

    Args:
        value: The value to encode.

    Returns:
        The UTF-8 JSON encoding of *value*.
    """
    if _orjson is not None:
        return _orjson.dumps(value, default=_json_default, option=_orjson.OPT_NON_STR_KEYS)
    return json.dumps(
        value, default=_json_default, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def deserialize(raw: bytes) -> Any:
    """Decode a value previously produced by :func:`serialize`.

    Args:
        raw: The stored bytes.

    Returns:
        The decoded JSON structure.

    Raises:
        ValueError: If *raw* is not valid JSON. Backends treat this as a corrupt entry —
            a miss plus a cleanup — rather than propagating it to the caller.
    """
    if _orjson is not None:
        try:
            return _orjson.loads(raw)
        except _orjson.JSONDecodeError as exc:  # pragma: no cover - depends on orjson
            raise ValueError(str(exc)) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(str(exc)) from exc


# --------------------------------------------------------------------------------------
# TTL handling
# --------------------------------------------------------------------------------------


def resolve_ttl(ttl: int | None, default: int | None) -> int | None:
    """Resolve a requested TTL against a backend default.

    The convention is uniform across every backend:

    * ``ttl is None`` — use the backend's configured default.
    * ``ttl <= 0`` — store the entry with **no expiry**.
    * otherwise — expire after that many seconds.

    Args:
        ttl: The TTL requested by the caller, in seconds.
        default: The backend's default TTL, in seconds, or ``None`` for no expiry.

    Returns:
        A positive number of seconds, or ``None`` when the entry should not expire.
    """
    effective = default if ttl is None else ttl
    if effective is None or effective <= 0:
        return None
    return int(effective)


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------


class _MetricEmitter:
    """Lazily-resolved bridge to ``applicantos_cache_events_total``.

    The observability module is optional: this package must import and work on its own, so
    resolution happens on first use and permanently disables itself when the module, the
    counter, or the Prometheus client is unavailable.
    """

    __slots__ = ("_emit", "_resolved")

    def __init__(self) -> None:
        self._resolved = False
        self._emit: Callable[[str, str], None] | None = None

    def _resolve(self) -> Callable[[str, str], None] | None:
        """Find a callable that records one cache event, or ``None`` if there is none."""
        try:
            from app.observability import metrics as metrics_module
        except ImportError:
            logger.debug("cache.metrics_unavailable", reason="module_not_importable")
            return None
        except Exception as exc:
            logger.debug("cache.metrics_unavailable", reason="module_error", error=str(exc))
            return None

        recorder = getattr(metrics_module, METRIC_RECORDER_ATTRIBUTE, None)
        if callable(recorder):
            return recorder

        for attribute in METRIC_COUNTER_ATTRIBUTES:
            counter = getattr(metrics_module, attribute, None)
            if counter is not None and hasattr(counter, "labels"):

                def emit(namespace: str, event: str, _counter: Any = counter) -> None:
                    _counter.labels(namespace=namespace, event=event).inc()

                return emit

        logger.debug("cache.metrics_unavailable", reason="counter_not_found")
        return None

    def __call__(self, namespace: str, event: str) -> None:
        """Record one cache event, swallowing any telemetry failure.

        Args:
            namespace: The namespace label.
            event: One of the :class:`CacheEvent` constants.
        """
        if not self._resolved:
            self._emit = self._resolve()
            self._resolved = True
        if self._emit is None:
            return
        try:
            self._emit(namespace, event)
        except Exception as exc:
            # Not ``event=``: that is structlog's own first parameter, and passing it as a
            # keyword alongside the positional event name is a TypeError.
            logger.debug("cache.metric_emit_failed", cache_event=event, error=str(exc))
            self._emit = None


#: Process-wide emitter. Resolution is memoised after the first call.
_emitter: Final[_MetricEmitter] = _MetricEmitter()


def emit_cache_event(namespace: str, event: str) -> None:
    """Increment ``applicantos_cache_events_total{namespace,event}``.

    A no-op when :mod:`app.observability.metrics` is not importable or exposes no cache
    counter. Never raises.

    Args:
        namespace: The cache namespace the event belongs to.
        event: One of the :class:`CacheEvent` constants.
    """
    _emitter(namespace, event)


# --------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class CacheStats:
    """Per-backend counters, surfaced on ``/settings`` and in maintenance logs.

    Attributes:
        hits: Reads that found a live entry.
        misses: Reads that found nothing, or found an expired entry.
        sets: Values written.
        evictions: Entries dropped by the cache itself — LRU pressure, TTL expiry, or a
            namespace clear.
    """

    hits: int = 0
    misses: int = 0
    sets: int = 0
    evictions: int = 0

    def record_hit(self, count: int = 1) -> None:
        """Add *count* hits."""
        self.hits += count

    def record_miss(self, count: int = 1) -> None:
        """Add *count* misses."""
        self.misses += count

    def record_set(self, count: int = 1) -> None:
        """Add *count* writes."""
        self.sets += count

    def record_eviction(self, count: int = 1) -> None:
        """Add *count* evictions."""
        self.evictions += count

    @property
    def reads(self) -> int:
        """Total number of read attempts (hits plus misses)."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Fraction of reads that were hits, in ``[0.0, 1.0]``; ``0.0`` before any read."""
        total = self.reads
        return self.hits / total if total else 0.0

    def reset(self) -> None:
        """Zero every counter."""
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.evictions = 0

    def as_dict(self) -> dict[str, float]:
        """Return the counters plus the derived hit rate, ready for JSON serialisation."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "evictions": self.evictions,
            "hit_rate": round(self.hit_rate, 4),
        }


# --------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------


@runtime_checkable
class Cache(Protocol):
    """The cache interface every backend implements (``docs/CONTRACTS.md`` §7).

    Callers depend on this protocol, obtained from :func:`app.cache.get_cache`, and never
    on a concrete backend — which backend is in play is a deployment decision.
    """

    async def get(self, key: str) -> Any | None:
        """Return the value stored under *key*, or ``None`` when absent or expired."""
        ...

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """Store *value* under *key*, expiring after *ttl* seconds."""
        ...

    async def delete(self, key: str) -> None:
        """Remove *key*. Removing a key that is not present is not an error."""
        ...

    async def exists(self, key: str) -> bool:
        """Return whether *key* holds a live (unexpired) entry."""
        ...

    async def clear(self, namespace: str | None = None) -> int:
        """Drop every entry in *namespace*, or the whole cache when it is ``None``."""
        ...

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        ttl: int | None = None,
    ) -> Any:
        """Return the cached value, computing and storing it with *factory* on a miss."""
        ...


# --------------------------------------------------------------------------------------
# Keyed lock pool
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _LockEntry:
    """One in-flight lock plus the number of coroutines currently interested in it."""

    lock: asyncio.Lock
    waiters: int = 0


class _KeyedLockPool:
    """A pool of per-key :class:`asyncio.Lock` objects, created and discarded on demand.

    Two properties make this safe for a process-wide cache singleton:

    * **Loop affinity.** An ``asyncio.Lock`` binds to the loop that first awaits it, and a
      Celery worker runs each task on a fresh loop. The pool notices the loop change and
      starts over rather than raising ``RuntimeError`` on a foreign lock.
    * **No leak.** Locks are reference-counted and dropped once the last waiter releases,
      so a long-lived cache does not accumulate one lock per key ever requested.

    All bookkeeping happens between ``await`` points, so it is atomic with respect to the
    event loop and needs no lock of its own.
    """

    __slots__ = ("_entries", "_loop")

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _checkout(self, key: str) -> asyncio.Lock:
        """Return the lock for *key*, creating it and registering this waiter."""
        running = asyncio.get_running_loop()
        if self._loop is not running:
            # A different event loop: every existing lock belongs to a dead loop.
            self._entries.clear()
            self._loop = running
        entry = self._entries.get(key)
        if entry is None:
            entry = _LockEntry(lock=asyncio.Lock())
            self._entries[key] = entry
        entry.waiters += 1
        return entry.lock

    def _checkin(self, key: str) -> None:
        """Deregister this waiter and drop the lock once nobody is left."""
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.waiters -= 1
        if entry.waiters <= 0:
            self._entries.pop(key, None)

    @contextlib.asynccontextmanager
    async def acquire(self, key: str) -> AsyncIterator[None]:
        """Hold the lock for *key* for the duration of the ``async with`` block.

        Args:
            key: The cache key being computed.

        Yields:
            ``None``, with the per-key lock held.
        """
        lock = self._checkout(key)
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self._checkin(key)

    def __len__(self) -> int:
        """Return the number of locks currently held or awaited."""
        return len(self._entries)


# --------------------------------------------------------------------------------------
# Abstract base
# --------------------------------------------------------------------------------------


class BaseCache(abc.ABC):
    """Shared implementation behind every ApplicantOS cache backend.

    Subclasses implement the five storage primitives — :meth:`get`, :meth:`set`,
    :meth:`delete`, :meth:`exists`, :meth:`clear` — and inherit stampede-safe
    :meth:`get_or_set`, TTL resolution, statistics, and metric emission.

    Subclasses must report every read through :meth:`_record_hit` / :meth:`_record_miss`
    and every write through :meth:`_record_set`, so statistics and the Prometheus counter
    stay consistent no matter which backend is configured.
    """

    #: Short backend identifier, bound onto this instance's logger.
    backend_name: ClassVar[str] = "base"

    def __init__(self, *, default_ttl: int | None = None) -> None:
        """Initialise shared cache state.

        Args:
            default_ttl: Lifetime applied when a caller does not pass an explicit ``ttl``.
                ``None`` (or a non-positive value) means entries do not expire by default.
        """
        self._default_ttl = default_ttl
        self._stats = CacheStats()
        self._locks = _KeyedLockPool()
        self._metrics_enabled = True
        self._log = logger.bind(backend=self.backend_name)

    # -- introspection -----------------------------------------------------------------

    @property
    def stats(self) -> CacheStats:
        """Live counters for this backend. Mutating the returned object is not supported."""
        return self._stats

    @property
    def default_ttl(self) -> int | None:
        """The TTL applied when a caller does not supply one."""
        return self._default_ttl

    def reset_stats(self) -> None:
        """Zero this backend's counters, e.g. between test cases."""
        self._stats.reset()

    def set_metrics_enabled(self, enabled: bool) -> None:
        """Turn Prometheus emission on or off for this instance.

        :class:`TieredCache` disables it on its tiers so that one logical operation
        produces exactly one metric sample rather than one per tier.

        Args:
            enabled: Whether this instance should emit cache events.
        """
        self._metrics_enabled = enabled

    def namespace_of(self, key: str) -> str:
        """Return the namespace *key* belongs to.

        Args:
            key: A cache key, ideally produced by :func:`app.cache.keys.make_key`.

        Returns:
            The normalised namespace prefix, or ``"misc"`` when the key has none.
        """
        return _namespace_of(key)

    def __repr__(self) -> str:
        """Return a concise, log-friendly description of this backend."""
        return f"<{type(self).__name__} default_ttl={self._default_ttl}>"

    # -- helpers for subclasses ----------------------------------------------------------

    def _ttl(self, ttl: int | None) -> int | None:
        """Resolve *ttl* against this backend's default. See :func:`resolve_ttl`."""
        return resolve_ttl(ttl, self._default_ttl)

    def _emit(self, namespace: str, event: str) -> None:
        """Emit one cache event unless metrics are disabled on this instance."""
        if self._metrics_enabled:
            emit_cache_event(namespace, event)

    def _record_hit(self, key: str) -> None:
        """Count a read that found a live entry."""
        self._stats.record_hit()
        self._emit(self.namespace_of(key), CacheEvent.HIT)

    def _record_miss(self, key: str) -> None:
        """Count a read that found nothing."""
        self._stats.record_miss()
        self._emit(self.namespace_of(key), CacheEvent.MISS)

    def _record_set(self, key: str) -> None:
        """Count a write."""
        self._stats.record_set()
        self._emit(self.namespace_of(key), CacheEvent.SET)

    def _record_delete(self, key: str) -> None:
        """Count an explicit removal."""
        self._emit(self.namespace_of(key), CacheEvent.DELETE)

    def _record_eviction(self, key: str, count: int = 1) -> None:
        """Count *count* entries dropped by the cache itself."""
        self._stats.record_eviction(count)
        self._emit(self.namespace_of(key), CacheEvent.EVICTION)

    def _record_clear(self, namespace: str | None, count: int) -> None:
        """Count a namespace clear that removed *count* entries."""
        self._stats.record_eviction(count)
        label = ALL_NAMESPACES_LABEL
        if namespace is not None:
            try:
                label = normalize_namespace(namespace)
            except (TypeError, ValueError):
                label = ALL_NAMESPACES_LABEL
        self._emit(label, CacheEvent.CLEAR)

    def _record_error(self, key: str) -> None:
        """Count a backend failure that was degraded to a miss instead of raised."""
        self._emit(self.namespace_of(key), CacheEvent.ERROR)

    def _namespace_prefix(self, namespace: str) -> str:
        """Return the key prefix matching every entry in *namespace*.

        Args:
            namespace: The namespace to scope to.

        Returns:
            ``"<normalised namespace>:"`` — the prefix every key in that namespace carries.
        """
        return f"{normalize_namespace(namespace)}{KEY_SEPARATOR}"

    # -- storage primitives ---------------------------------------------------------------

    @abc.abstractmethod
    async def get(self, key: str) -> Any | None:
        """Return the value stored under *key*.

        Args:
            key: The cache key.

        Returns:
            The stored value, or ``None`` when the key is absent or its entry expired.
        """

    @abc.abstractmethod
    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """Store *value* under *key*.

        Args:
            key: The cache key.
            value: The value to store. Backends other than memory store JSON, so see
                :func:`serialize` for how non-JSON-native values are treated.
            ttl: Lifetime in seconds. ``None`` uses the backend default; a non-positive
                value stores the entry without expiry.
        """

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove *key*.

        Args:
            key: The cache key. Removing an absent key is not an error.
        """

    @abc.abstractmethod
    async def exists(self, key: str) -> bool:
        """Return whether *key* currently holds a live entry.

        Args:
            key: The cache key.

        Returns:
            ``True`` when the entry exists and has not expired. Does not count as a
            hit or a miss.
        """

    @abc.abstractmethod
    async def clear(self, namespace: str | None = None) -> int:
        """Remove every entry in *namespace*.

        Args:
            namespace: The namespace to drop, or ``None`` to drop everything.

        Returns:
            The number of entries removed.
        """

    # -- optional capabilities ----------------------------------------------------------

    async def ttl_of(self, key: str) -> int | None:
        """Return how many whole seconds *key* has left before it expires.

        This is an *optional* capability, deliberately kept off the :class:`Cache` protocol
        so that a backend is free not to answer. The default implementation reports
        "unknown"; the memory, disk, and Redis backends override it (monotonic deadline,
        the sidecar's ``expires_at``, and ``PTTL`` respectively). :class:`TieredCache` uses
        it to bound a promoted front-tier copy by the backing entry's real deadline instead
        of a fixed window.

        Args:
            key: The cache key.

        Returns:
            A positive number of seconds when the entry is live and carries a deadline;
            ``None`` when it never expires, is absent, or this backend cannot tell. Never
            zero or negative: a caller may pass the result straight to :meth:`set`, where a
            non-positive TTL means *no expiry at all* and would invert the intent.
        """
        return None

    # -- composed behaviour -----------------------------------------------------------------

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        ttl: int | None = None,
    ) -> Any:
        """Return the cached value for *key*, computing it with *factory* on a miss.

        Concurrency-safe: while one coroutine runs the factory, others asking for the same
        key wait on a per-key lock and then read the value the winner stored. Without this,
        a burst of identical requests at startup would each pay for the same LLM call.

        A factory that returns ``None`` is re-invoked on the next call, because the
        protocol cannot distinguish a stored ``None`` from an absent key.

        Args:
            key: The cache key.
            factory: Zero-argument coroutine function producing the value on a miss.
            ttl: Lifetime for the newly computed value, in seconds.

        Returns:
            The cached or freshly computed value.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached

        async with self._locks.acquire(key):
            # Re-check: another coroutine may have populated the key while we queued.
            cached = await self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            if value is not None:
                await self.set(key, value, ttl=ttl)
            return value

    async def close(self) -> None:
        """Release any resources held by this backend.

        The default implementation does nothing; backends holding a connection pool or an
        open file handle override it. Safe to call more than once.
        """
        return


# --------------------------------------------------------------------------------------
# Tiering
# --------------------------------------------------------------------------------------


class TieredCache(BaseCache):
    """A fast front tier read through to a durable backing tier.

    The front tier is normally :class:`~app.cache.memory.MemoryCache` and the backing tier
    :class:`~app.cache.disk.DiskCache` or :class:`~app.cache.redis_cache.RedisCache`. Reads
    check the front tier first and promote a backing hit into it; writes go to both.

    The front copy's TTL is the smaller of *front_ttl* (:data:`TIERED_FRONT_TTL_SECONDS`
    by default) and whatever is really left of the backing entry's lifetime, which the
    backing tier reports through :meth:`BaseCache.ttl_of` (Redis ``PTTL``; the disk
    sidecar's ``expires_at``). A promoted copy therefore never outlives the value it
    mirrors. When the backing tier cannot answer — it holds no deadline for the key, or it
    does not implement ``ttl_of`` at all — the copy falls back to the full *front_ttl*
    window, which is also the bound on how long a key deleted by *another* process can
    still be served from here.

    Statistics are counted at this level and on each tier independently; Prometheus
    emission is disabled on the tiers so one logical operation yields one sample.
    """

    backend_name: ClassVar[str] = "tiered"

    def __init__(
        self,
        front: Cache,
        backing: Cache,
        *,
        default_ttl: int | None = None,
        front_ttl: int = TIERED_FRONT_TTL_SECONDS,
    ) -> None:
        """Compose two caches into one.

        Args:
            front: The fast, small, process-local tier.
            backing: The durable, shared tier.
            default_ttl: TTL applied when a caller does not supply one.
            front_ttl: Upper bound on how long a value may live in the front tier.
        """
        super().__init__(default_ttl=default_ttl)
        self._front = front
        self._backing = backing
        self._front_ttl = front_ttl
        for tier in (front, backing):
            if isinstance(tier, BaseCache):
                tier.set_metrics_enabled(False)

    @property
    def front(self) -> Cache:
        """The fast front tier."""
        return self._front

    @property
    def backing(self) -> Cache:
        """The durable backing tier."""
        return self._backing

    def _front_ttl_for(self, ttl: int | None) -> int:
        """Return the TTL to use for the front-tier copy.

        Args:
            ttl: The backing entry's lifetime — the resolved TTL just written, or the
                remaining lifetime of an entry being promoted — or ``None`` when it is
                unbounded or unknown.

        Returns:
            A positive number of seconds, never longer than ``front_ttl``. Never zero: a
            non-positive TTL means "never expire" to every backend, which is the opposite
            of what a nearly-dead backing entry calls for.
        """
        if ttl is None:
            return self._front_ttl
        return max(1, min(ttl, self._front_ttl))

    async def _backing_ttl(self, key: str) -> int | None:
        """Return the backing entry's remaining lifetime, or ``None`` when unknown.

        The backing tier is typed as the :class:`Cache` protocol, which by contract has no
        ``ttl_of``; a tier that does not provide one, or that fails to answer, yields
        ``None`` rather than an error. A TTL probe is an optimisation and must never turn
        a successful read into a failure.

        Args:
            key: The cache key.

        Returns:
            Seconds left on the backing entry, or ``None`` when it never expires, is gone,
            or the tier cannot report a deadline.
        """
        reader = getattr(self._backing, "ttl_of", None)
        if reader is None:
            return None
        try:
            remaining = await reader(key)
        except Exception as exc:
            self._log.debug(
                "cache.tier_ttl_probe_failed", tier=type(self._backing).__name__, error=str(exc)
            )
            return None
        if remaining is None or remaining <= 0:
            return None
        return int(remaining)

    async def ttl_of(self, key: str) -> int | None:
        """Report the backing tier's remaining lifetime for *key*.

        The front tier is a bounded mirror, so the backing tier owns the real deadline.

        Args:
            key: The cache key.

        Returns:
            Seconds left on the backing entry, or ``None`` when it is unbounded or unknown.
        """
        return await self._backing_ttl(key)

    async def get(self, key: str) -> Any | None:
        """Read through the front tier into the backing tier, promoting any backing hit.

        The promoted copy is given the *shorter* of ``front_ttl`` and the backing entry's
        own remaining lifetime, so a value cannot keep being served here after it has
        expired in the durable tier that every other process reads.
        """
        value = await self._front.get(key)
        if value is not None:
            self._record_hit(key)
            return value

        value = await self._backing.get(key)
        if value is None:
            self._record_miss(key)
            return None

        remaining = await self._backing_ttl(key)
        await self._front.set(key, value, ttl=self._front_ttl_for(remaining))
        self._record_hit(key)
        return value

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """Write to both tiers, clamping the front-tier lifetime."""
        effective = self._ttl(ttl)
        await self._backing.set(key, value, ttl=effective)
        await self._front.set(key, value, ttl=self._front_ttl_for(effective))
        self._record_set(key)

    async def delete(self, key: str) -> None:
        """Remove *key* from both tiers."""
        await self._front.delete(key)
        await self._backing.delete(key)
        self._record_delete(key)

    async def exists(self, key: str) -> bool:
        """Return whether either tier holds a live entry for *key*."""
        if await self._front.exists(key):
            return True
        return await self._backing.exists(key)

    async def clear(self, namespace: str | None = None) -> int:
        """Clear both tiers.

        Args:
            namespace: The namespace to drop, or ``None`` for everything.

        Returns:
            The larger of the two tier counts. The front tier is normally a subset of the
            backing tier, so summing would double-count; taking the maximum reports the
            number of distinct entries that are now gone.
        """
        front_count = await self._front.clear(namespace)
        backing_count = await self._backing.clear(namespace)
        removed = max(front_count, backing_count)
        self._record_clear(namespace, removed)
        return removed

    async def close(self) -> None:
        """Close both tiers, tolerating tiers that expose no ``close``."""
        for tier in (self._front, self._backing):
            closer = getattr(tier, "close", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:
                self._log.warning(
                    "cache.tier_close_failed", tier=type(tier).__name__, error=str(exc)
                )

    def __repr__(self) -> str:
        """Return a description naming both tiers."""
        return (
            f"<TieredCache front={type(self._front).__name__} "
            f"backing={type(self._backing).__name__}>"
        )
