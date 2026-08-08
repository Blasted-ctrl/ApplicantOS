"""Caching layer — the system-wide memo of every expensive thing ApplicantOS has done.

Golden rule #9 of ``docs/CONTRACTS.md``: *cache aggressively, invalidate precisely*. LLM
completions, embeddings, parsed resumes, project summaries, GitHub analyses, company
metadata, job descriptions, rendered documents and scoring results all flow through here,
keyed by content so that identical work is never paid for twice.

Use the package, not the modules::

    from app.cache import NAMESPACES, cached, get_cache, make_key

    cache = get_cache()
    key = make_key(NAMESPACES.COMPANY, "acme.com")
    profile = await cache.get_or_set(key, lambda: enrich("acme.com"), ttl=86400)

    @cached(NAMESPACES.SCORE, ttl=3600)
    async def score(posting_id: str, user_id: str) -> dict[str, int]:
        ...

**Backend selection** follows ``settings.cache_backend``:

===========  ==========================================================================
``memory``   :class:`~app.cache.memory.MemoryCache` alone — process-local, nothing to
             install, wiped on restart. The right choice for tests.
``disk``     :class:`~app.cache.memory.MemoryCache` over
             :class:`~app.cache.disk.DiskCache` — survives restarts with no services to
             run. Forced by ``settings.sqlite_mode``.
``redis``    :class:`~app.cache.memory.MemoryCache` over
             :class:`~app.cache.redis_cache.RedisCache` — shared between the API process
             and every Celery worker. The default.
===========  ==========================================================================

The two durable backends are always fronted by a :class:`~app.cache.base.TieredCache`, so
a repeated read inside one pipeline run costs a dictionary lookup rather than a network or
filesystem round trip, while the durable tier keeps the value available to every other
process and to tomorrow's run.
"""

from __future__ import annotations

from functools import lru_cache

import structlog

from app.cache.base import (
    BaseCache,
    Cache,
    CacheEvent,
    CacheStats,
    TieredCache,
    deserialize,
    emit_cache_event,
    resolve_ttl,
    serialize,
)
from app.cache.decorators import CachedFunction, cached, invalidate
from app.cache.disk import DiskCache
from app.cache.keys import (
    DEFAULT_NAMESPACE,
    KEY_SEPARATOR,
    NAMESPACES,
    canonical_json,
    hash_payload,
    make_key,
    namespace_of,
    normalize_namespace,
    to_jsonable,
)
from app.cache.memory import DEFAULT_MAX_ENTRIES, MemoryCache
from app.cache.redis_cache import RedisCache
from app.config.settings import Settings, get_settings

__all__ = [
    "DEFAULT_NAMESPACE",
    "KEY_SEPARATOR",
    "NAMESPACES",
    "BaseCache",
    "Cache",
    "CacheEvent",
    "CacheStats",
    "CachedFunction",
    "DiskCache",
    "MemoryCache",
    "RedisCache",
    "TieredCache",
    "build_cache",
    "cached",
    "canonical_json",
    "deserialize",
    "emit_cache_event",
    "get_cache",
    "hash_payload",
    "invalidate",
    "make_key",
    "namespace_of",
    "normalize_namespace",
    "reset_cache",
    "resolve_ttl",
    "serialize",
    "to_jsonable",
]

logger = structlog.get_logger(__name__)


def build_cache(settings: Settings) -> Cache:
    """Construct the cache described by *settings*, without memoising it.

    Args:
        settings: Configuration supplying ``cache_backend``, ``cache_dir``,
            ``cache_default_ttl`` and ``redis_url``.

    Returns:
        A ready-to-use cache. The ``disk`` and ``redis`` backends are wrapped in a
        :class:`~app.cache.base.TieredCache` behind an in-process
        :class:`~app.cache.memory.MemoryCache`; ``memory`` is returned bare.

    Raises:
        ValueError: If ``cache_backend`` is not one of ``memory``, ``disk`` or ``redis``.
            Settings validation normally catches this first.
    """
    default_ttl = settings.cache_default_ttl
    backend = settings.cache_backend

    if backend == "memory":
        cache: Cache = MemoryCache(max_entries=DEFAULT_MAX_ENTRIES, default_ttl=default_ttl)
    elif backend == "disk":
        cache = TieredCache(
            MemoryCache(max_entries=DEFAULT_MAX_ENTRIES, default_ttl=default_ttl),
            DiskCache(settings.cache_path, default_ttl=default_ttl),
            default_ttl=default_ttl,
        )
    elif backend == "redis":
        cache = TieredCache(
            MemoryCache(max_entries=DEFAULT_MAX_ENTRIES, default_ttl=default_ttl),
            RedisCache(settings.redis_url, default_ttl=default_ttl),
            default_ttl=default_ttl,
        )
    else:  # pragma: no cover - unreachable while Settings validates the literal
        raise ValueError(f"unknown cache backend: {backend!r}")

    logger.debug("cache.configured", backend=backend, default_ttl=default_ttl)
    return cache


@lru_cache(maxsize=1)
def get_cache() -> Cache:
    """Return the process-wide cache singleton.

    Memoised, so every caller shares one front tier and one connection pool. Construction
    is lazy and never connects eagerly: importing this module does not touch Redis or the
    filesystem.

    Returns:
        The configured :class:`~app.cache.base.Cache`.
    """
    return build_cache(get_settings())


def reset_cache() -> None:
    """Discard the memoised cache singleton so the next call rebuilds it.

    Intended for tests that change ``cache_backend`` and for a clean shutdown path. Any
    still-open backend should be closed by the caller before or after this call —
    ``await cache.close()`` — because this function only drops the reference.
    """
    get_cache.cache_clear()
