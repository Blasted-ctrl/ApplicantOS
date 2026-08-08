"""Shared cache over Redis — the default backing tier.

:class:`RedisCache` is what makes the cache *shared*: the FastAPI process, every Celery
worker, and any script all read the same entries, so work paid for by one is never repeated
by another. It is the default backend (``settings.cache_backend == "redis"``) for any
install that is not in lightweight ``sqlite_mode``.

Two properties matter more than raw speed here.

**Graceful degradation.** Redis being down must never take the application down — a cache
is an optimisation, not a data store. Every operation is guarded: on a connection failure
the backend logs once, marks itself degraded, and behaves as a permanent miss for
:data:`DEGRADED_RETRY_SECONDS` before probing again. Callers see slower work, not an
exception. The same applies when the ``redis`` package is not installed at all, which is
then permanent rather than retried.

**Never ``KEYS``.** Namespace clears iterate with ``SCAN`` in bounded batches and delete
with ``UNLINK`` (falling back to ``DEL`` on older servers). ``KEYS`` blocks the Redis event
loop for the duration of a full keyspace walk and is unacceptable on a shared instance.

Values are serialised with ``orjson`` when available and :mod:`json` otherwise; the two are
wire-compatible, so a process with the accelerator and one without share a cache happily.
Keys are stored under :data:`DEFAULT_KEY_PREFIX` so that ApplicantOS can share a Redis
database with the Celery broker and result backend without collisions.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Final

import structlog

from app.cache.base import BaseCache, deserialize, serialize
from app.cache.keys import KEY_SEPARATOR, normalize_namespace
from app.config.settings import get_settings

__all__ = ["DEFAULT_KEY_PREFIX", "DEGRADED_RETRY_SECONDS", "RedisCache"]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Prefix applied to every key, so the cache can share a database with Celery.
DEFAULT_KEY_PREFIX: Final[str] = "applicantos:cache:"

#: How long the backend stays in permanent-miss mode after a connection failure before it
#: tries Redis again. Short enough that a restarted server is picked up quickly, long
#: enough that a sustained outage does not turn every cache read into a connection attempt.
DEGRADED_RETRY_SECONDS: Final[float] = 30.0

#: ``COUNT`` hint for ``SCAN``. Larger batches mean fewer round trips; this value keeps each
#: server-side scan step short enough not to affect other clients.
SCAN_BATCH_SIZE: Final[int] = 500

#: Maximum number of keys deleted in one ``UNLINK``/``DEL`` command during a clear.
DELETE_BATCH_SIZE: Final[int] = 500

#: Seconds to wait when establishing a connection before declaring Redis unreachable.
CONNECT_TIMEOUT_SECONDS: Final[float] = 2.0

#: Seconds to wait on a single command before declaring Redis unreachable.
COMMAND_TIMEOUT_SECONDS: Final[float] = 5.0


class RedisCache(BaseCache):
    """A cache backed by a Redis server, degrading to a no-op when Redis is unavailable.

    Values round-trip as JSON: dictionaries and lists come back exactly, while pydantic
    models and dataclasses come back as dictionaries and must be re-validated by the
    caller (see :func:`app.cache.base.serialize`).
    """

    backend_name: ClassVar[str] = "redis"

    def __init__(
        self,
        url: str | None = None,
        *,
        default_ttl: int | None = None,
        key_prefix: str = DEFAULT_KEY_PREFIX,
    ) -> None:
        """Create a Redis-backed cache.

        No connection is made here: the client is constructed on first use and connects
        lazily, so importing or instantiating this class never blocks and never fails.

        Args:
            url: Redis connection URL. Defaults to ``settings.redis_url``.
            default_ttl: Lifetime applied when a caller passes no explicit ``ttl``.
            key_prefix: String prepended to every key stored in Redis.
        """
        super().__init__(default_ttl=default_ttl)
        self._url = url if url is not None else get_settings().redis_url
        self._key_prefix = key_prefix
        self._client: Any | None = None
        self._unavailable = False
        self._degraded_until: float | None = None
        self._degraded_logged = False

    # -- introspection ---------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The configured Redis connection URL."""
        return self._url

    @property
    def is_degraded(self) -> bool:
        """Whether the backend is currently answering every read as a miss."""
        if self._unavailable:
            return True
        return self._degraded_until is not None and time.monotonic() < self._degraded_until

    def __repr__(self) -> str:
        """Return a description including the degradation state."""
        return f"<RedisCache degraded={self.is_degraded}>"

    # -- connection management ----------------------------------------------------------------

    def _redis_key(self, key: str) -> str:
        """Return the prefixed key actually stored in Redis."""
        return f"{self._key_prefix}{key}"

    def _match_pattern(self, namespace: str | None) -> str:
        """Return the ``SCAN`` glob selecting every key in *namespace*.

        Args:
            namespace: The namespace to scope to, or ``None`` for every ApplicantOS key.

        Returns:
            A Redis glob pattern. It is always anchored to the instance key prefix, so a
            clear can never touch the Celery broker's keys.
        """
        if namespace is None:
            return f"{self._key_prefix}*"
        return f"{self._key_prefix}{normalize_namespace(namespace)}{KEY_SEPARATOR}*"

    def _enter_degraded(self, reason: str, error: BaseException | None = None) -> None:
        """Mark the backend degraded and log the transition once.

        Args:
            reason: Short machine-readable cause, bound onto the log event.
            error: The triggering exception, when there was one.
        """
        self._degraded_until = time.monotonic() + DEGRADED_RETRY_SECONDS
        if self._degraded_logged:
            return
        self._degraded_logged = True
        logger.warning(
            "cache.redis_degraded",
            reason=reason,
            retry_in_seconds=DEGRADED_RETRY_SECONDS,
            error=str(error) if error is not None else None,
        )

    def _leave_degraded(self) -> None:
        """Clear degradation after a successful command, logging the recovery once."""
        if self._degraded_until is None and not self._degraded_logged:
            return
        self._degraded_until = None
        if self._degraded_logged:
            self._degraded_logged = False
            logger.info("cache.redis_recovered")

    def _client_or_none(self) -> Any | None:
        """Return a usable Redis client, or ``None`` while degraded.

        Returns:
            The lazily-constructed client, or ``None`` when the ``redis`` package is
            missing or the retry window after a failure has not elapsed.
        """
        if self._unavailable:
            return None
        if self._degraded_until is not None and time.monotonic() < self._degraded_until:
            return None
        if self._client is not None:
            return self._client

        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            # Permanent: no retry window will make an uninstalled package appear.
            self._unavailable = True
            logger.warning("cache.redis_unavailable", reason="package_missing", error=str(exc))
            return None

        try:
            self._client = Redis.from_url(
                self._url,
                decode_responses=False,
                socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
                socket_timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self._enter_degraded("client_construction_failed", exc)
            return None
        return self._client

    def _on_failure(self, operation: str, key: str | None, error: BaseException) -> None:
        """Record a failed Redis command and degrade.

        The client is deliberately kept: ``redis.asyncio`` reconnects on its own, so
        discarding the connection pool on every blip would only add churn.

        Args:
            operation: The command that failed, for the log event.
            key: The key involved, when there was one.
            error: The exception raised.
        """
        if key is not None:
            self._record_error(key)
        logger.debug("cache.redis_operation_failed", operation=operation, error=str(error))
        self._enter_degraded(f"{operation}_failed", error)

    # -- storage primitives ---------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        """Return the value stored under *key*.

        Args:
            key: The cache key.

        Returns:
            The stored value, or ``None`` when absent, expired, corrupt, or when Redis is
            unreachable — an outage looks exactly like a miss to the caller.
        """
        client = self._client_or_none()
        if client is None:
            self._record_miss(key)
            return None

        try:
            raw = await client.get(self._redis_key(key))
        except Exception as exc:
            self._on_failure("get", key, exc)
            self._record_miss(key)
            return None

        self._leave_degraded()
        if raw is None:
            self._record_miss(key)
            return None

        try:
            value = deserialize(raw)
        except ValueError:
            logger.debug("cache.redis_value_corrupt", key=key)
            await self.delete(key)
            self._record_miss(key)
            return None

        self._record_hit(key)
        return value

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """Store *value* under *key*.

        Args:
            key: The cache key.
            value: The value to store; serialised to JSON.
            ttl: Lifetime in seconds. ``None`` uses the backend default; a non-positive
                value stores the entry without expiry.
        """
        client = self._client_or_none()
        if client is None:
            return

        payload = serialize(value)
        effective_ttl = self._ttl(ttl)
        try:
            if effective_ttl is None:
                await client.set(self._redis_key(key), payload)
            else:
                await client.set(self._redis_key(key), payload, ex=effective_ttl)
        except Exception as exc:
            self._on_failure("set", key, exc)
            return

        self._leave_degraded()
        self._record_set(key)

    async def delete(self, key: str) -> None:
        """Remove *key* from Redis.

        Args:
            key: The cache key. Deleting an absent key is not an error.
        """
        client = self._client_or_none()
        if client is None:
            return
        try:
            await client.delete(self._redis_key(key))
        except Exception as exc:
            self._on_failure("delete", key, exc)
            return
        self._leave_degraded()
        self._record_delete(key)

    async def exists(self, key: str) -> bool:
        """Return whether *key* holds a live entry.

        Args:
            key: The cache key.

        Returns:
            ``True`` when Redis reports the key present. ``False`` while degraded.
        """
        client = self._client_or_none()
        if client is None:
            return False
        try:
            count = await client.exists(self._redis_key(key))
        except Exception as exc:
            self._on_failure("exists", key, exc)
            return False
        self._leave_degraded()
        return bool(count)

    async def ttl_of(self, key: str) -> int | None:
        """Return how many whole seconds *key* has left, via ``PTTL``.

        Lets :class:`~app.cache.base.TieredCache` bound a promoted front-tier copy by this
        entry's real deadline instead of a fixed window — which matters here more than
        anywhere else, because Redis is shared between the API process and every worker,
        so the process that wrote an entry is rarely the process that reads it.

        ``PTTL`` is used rather than ``TTL`` because it reports the sub-second remainder,
        which is then truncated down: an entry with 400 ms left reports the floor of one
        second rather than rounding up to a deadline it does not have.

        Args:
            key: The cache key.

        Returns:
            A positive number of seconds, or ``None`` when the key has no expiry, does not
            exist, or Redis is unreachable.
        """
        client = self._client_or_none()
        if client is None:
            return None
        try:
            milliseconds = await client.pttl(self._redis_key(key))
        except Exception as exc:
            self._on_failure("pttl", key, exc)
            return None

        self._leave_degraded()
        # -1 means the key exists with no expiry; -2 means it is gone. Neither is a
        # deadline, and both are reported as "no bound" rather than as an error.
        if milliseconds is None or int(milliseconds) < 0:
            return None
        return max(1, int(milliseconds) // 1000)

    async def clear(self, namespace: str | None = None) -> int:
        """Delete every key in *namespace* using ``SCAN``.

        Iterates the keyspace in bounded batches and unlinks them in bounded batches, so
        the operation never blocks the Redis server the way ``KEYS`` would.

        Args:
            namespace: The namespace to drop, or ``None`` for every ApplicantOS cache key.

        Returns:
            The number of keys deleted; ``0`` while degraded.
        """
        client = self._client_or_none()
        if client is None:
            return 0

        pattern = self._match_pattern(namespace)
        removed = 0
        batch: list[bytes | str] = []
        try:
            async for raw_key in client.scan_iter(match=pattern, count=SCAN_BATCH_SIZE):
                batch.append(raw_key)
                if len(batch) >= DELETE_BATCH_SIZE:
                    removed += await self._unlink(client, batch)
                    batch.clear()
            if batch:
                removed += await self._unlink(client, batch)
        except Exception as exc:
            self._on_failure("clear", None, exc)
            return removed

        self._leave_degraded()
        self._record_clear(namespace, removed)
        return removed

    @staticmethod
    async def _unlink(client: Any, keys: list[bytes | str]) -> int:
        """Delete a batch of keys, preferring the non-blocking ``UNLINK``.

        Args:
            client: The Redis client.
            keys: Raw keys as returned by ``SCAN``.

        Returns:
            The number of keys the server reports as removed.
        """
        try:
            return int(await client.unlink(*keys))
        except Exception:
            return int(await client.delete(*keys))

    async def close(self) -> None:
        """Close the connection pool. Safe to call when no client was ever created."""
        client = self._client
        if client is None:
            return
        self._client = None
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception as exc:
            logger.debug("cache.redis_close_failed", error=str(exc))
