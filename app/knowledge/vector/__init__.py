"""Vector storage — one protocol, three interchangeable backends.

Everything that needs semantic search goes through :func:`get_vector_store`, never through
a concrete class::

    from app.knowledge.vector import VectorRecord, get_vector_store

    store = get_vector_store()
    await store.upsert("chunks", [VectorRecord(id=str(chunk.id), embedding=vec,
                                               text=chunk.text,
                                               metadata={"user_id": str(user.id),
                                                         "kind": "resume"})])
    hits = await store.query("chunks", query_vec, k=12, filters={"user_id": str(user.id)})

**Backend selection** follows ``settings.vector_store`` (``docs/CONTRACTS.md`` §1, §8.2):

==============  =======================================================================
``pgvector``    :class:`~app.knowledge.vector.pgvector.PgVectorStore` — the production
                default. Shares the application's async engine; needs PostgreSQL with the
                ``vector`` extension.
``sqlite_vec``  :class:`~app.knowledge.vector.sqlite_vec.SqliteVecStore` — one file under
                ``settings.data_path``, durable, no server. Forced by
                ``settings.sqlite_mode``. Uses the ``sqlite-vec`` extension when it is
                installed and a pure-Python cosine scan when it is not; both are complete.
``memory``      :class:`~app.knowledge.vector.memory_store.InMemoryVectorStore` — exact
                search, no dependencies, nothing to install, gone on restart.
==============  =======================================================================

All three implement :class:`~app.knowledge.vector.base.VectorStore` with identical filter
semantics and an identical score scale, so switching backends changes performance and
durability but never results. See :mod:`app.knowledge.vector.base` for the filter grammar
and the reserved ``user_id`` / ``kind`` keys.
"""

from __future__ import annotations

from functools import lru_cache

import structlog

from app.config.settings import Settings, get_settings
from app.knowledge.vector.base import (
    DEFAULT_TOP_K,
    FILTER_KEY_KIND,
    FILTER_KEY_USER_ID,
    RESERVED_FILTER_KEYS,
    Filter,
    VectorHit,
    VectorRecord,
    VectorStore,
    VectorStoreError,
    cosine_similarity,
    dot,
    matches_filters,
    normalize,
    rank_hits,
)
from app.knowledge.vector.memory_store import InMemoryVectorStore
from app.knowledge.vector.pgvector import PgVectorStore
from app.knowledge.vector.sqlite_vec import SqliteVecStore

__all__ = [
    "DEFAULT_TOP_K",
    "FILTER_KEY_KIND",
    "FILTER_KEY_USER_ID",
    "Filter",
    "InMemoryVectorStore",
    "PgVectorStore",
    "RESERVED_FILTER_KEYS",
    "SqliteVecStore",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
    "VectorStoreError",
    "build_vector_store",
    "close_vector_store",
    "cosine_similarity",
    "dot",
    "get_vector_store",
    "matches_filters",
    "normalize",
    "rank_hits",
    "reset_vector_store",
]

logger = structlog.get_logger(__name__)


def build_vector_store(settings: Settings) -> VectorStore:
    """Construct the vector store described by *settings*, without memoising it.

    Args:
        settings: Configuration supplying ``vector_store`` (and, for the concrete backends,
            ``embedding_dim`` and ``data_dir``).

    Returns:
        A ready-to-use store. Construction performs no I/O for any backend: the SQLite file
        is opened and the PostgreSQL schema created on first operation.

    Raises:
        VectorStoreError: If ``vector_store`` names an unknown backend. Settings validation
            normally catches this first, since the field is a ``Literal``.
    """
    backend = settings.vector_store
    if backend == "pgvector":
        store: VectorStore = PgVectorStore(settings)
    elif backend == "sqlite_vec":
        store = SqliteVecStore(settings)
    elif backend == "memory":
        store = InMemoryVectorStore()
    else:  # pragma: no cover - unreachable while Settings validates the literal
        raise VectorStoreError(f"unknown vector store backend: {backend!r}")

    logger.debug("vector.configured", backend=backend, dim=settings.embedding_dim)
    return store


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    """Return the process-wide vector store singleton.

    Memoised so the indexer, the retriever and every API request share one SQLite
    connection or one schema-ready flag rather than re-establishing them per call.

    Returns:
        The configured :class:`~app.knowledge.vector.base.VectorStore`.
    """
    return build_vector_store(get_settings())


def reset_vector_store() -> None:
    """Discard the memoised singleton so the next call rebuilds it.

    For tests that change ``vector_store``, and for shutdown. This only drops the
    reference; use :func:`close_vector_store` when the current instance holds an open
    SQLite connection.
    """
    get_vector_store.cache_clear()


async def close_vector_store() -> None:
    """Close the memoised store, if one was built, and forget it.

    Called from application and worker shutdown. Backends expose ``close()`` as a
    convention rather than as part of the protocol, so a store without one is simply
    dropped.
    """
    store = get_vector_store()
    closer = getattr(store, "close", None)
    if callable(closer):
        await closer()
    reset_vector_store()
