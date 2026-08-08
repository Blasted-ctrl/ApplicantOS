"""In-process vector store — the backend that is always available.

``docs/CONTRACTS.md`` §8.2 requires a numpy-free, pure-Python vector store, and golden rule
"everything must work offline with zero API keys" makes it load-bearing rather than a
convenience: paired with
:class:`~app.ai.embeddings.HashingEmbedder`, this class is what lets the whole knowledge
engine index, retrieve and generate a resume on a laptop with nothing installed and no
network.

Shape
-----

Records live in a dict of dicts — ``collection -> id -> entry`` — so upsert, delete and
count are O(1) and a query touches only the collection it was given.

Every entry caches a **unit-length copy** of its embedding alongside the original record.
Cosine similarity between unit vectors is just their dot product, so scoring one candidate
is a single pass over the components instead of the three that
:func:`~app.knowledge.vector.base.cosine_similarity` needs. The cost is one extra vector's
worth of memory per record, paid once at write time, on the read-heavy side of a workload
that queries far more often than it indexes.

Filters are applied **before** scoring, so a search restricted to one user never pays for
another user's vectors, and ``k`` counts matching records rather than being trimmed after
the fact.

Concurrency
-----------

All state changes happen synchronously inside the ``async def`` methods with no ``await``
between them, so each operation is atomic with respect to the event loop and no lock is
needed — the same design as :class:`~app.cache.memory.MemoryCache`. The store is *not*
safe to share between OS threads; nothing in ApplicantOS does that.

Durability
----------

None. This store lives and dies with the process, which is exactly right for tests, for
``VECTOR_STORE=memory``, and as a scratch index. Anything that must survive a restart uses
:class:`~app.knowledge.vector.sqlite_vec.SqliteVecStore`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

import structlog

from app.knowledge.vector.base import (
    DEFAULT_TOP_K,
    Filter,
    VectorHit,
    VectorRecord,
    batch_dimension,
    check_dimension,
    dot,
    matches_filters,
    normalize,
    rank_hits,
    validate_collection,
    validate_filters,
    validate_k,
)

__all__ = ["InMemoryVectorStore"]

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Entry:
    """A stored record plus the unit-length form used for scoring.

    Attributes:
        record: The record exactly as the caller supplied it.
        unit: :attr:`record.embedding <app.knowledge.vector.base.VectorRecord.embedding>`
            scaled to length ``1.0``, or a vector of zeros when the embedding was the zero
            vector. Scoring is then a dot product against a unit-length query.
    """

    record: VectorRecord
    unit: list[float]


class InMemoryVectorStore:
    """A complete :class:`~app.knowledge.vector.base.VectorStore` held in this process.

    Implements exact (not approximate) nearest-neighbour search: results are the true
    top-``k`` by cosine similarity, never an index's estimate. That makes it the reference
    the other two backends are checked against.

    Example:
        >>> import asyncio
        >>> store = InMemoryVectorStore()
        >>> record = VectorRecord(id="a", embedding=[1.0, 0.0], text="hello",
        ...                       metadata={"user_id": "u1"})
        >>> asyncio.run(store.upsert("chunks", [record]))
        1
        >>> hits = asyncio.run(store.query("chunks", [1.0, 0.0], filters={"user_id": "u1"}))
        >>> hits[0].id, round(hits[0].score, 6)
        ('a', 1.0)
    """

    backend_name: ClassVar[str] = "memory"

    def __init__(self) -> None:
        """Create an empty store."""
        self._collections: dict[str, dict[str, _Entry]] = {}
        self._dimensions: dict[str, int] = {}

    def __repr__(self) -> str:
        """Return a description including collection and record counts."""
        total = sum(len(entries) for entries in self._collections.values())
        return f"<InMemoryVectorStore collections={len(self._collections)} records={total}>"

    # -- introspection -------------------------------------------------------------------

    def dimension_of(self, collection: str) -> int | None:
        """Return the embedding width *collection* is bound to.

        Args:
            collection: Collection name.

        Returns:
            The width fixed by the first record written, or ``None`` when the collection is
            empty or unknown.
        """
        return self._dimensions.get(validate_collection(collection))

    # -- writes ---------------------------------------------------------------------------

    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> int:
        """Insert or replace *records*, keyed by :attr:`VectorRecord.id`.

        The whole batch is validated before anything is written, so a bad record cannot
        leave the collection half-updated.

        Args:
            collection: Target collection, created on first write.
            records: Records to store. Empty is a no-op returning ``0``.

        Returns:
            The number of records written.

        Raises:
            VectorStoreError: If an embedding is empty, or a width disagrees with the batch
                or with the collection's established width (both are named in the message).
        """
        name = validate_collection(collection)
        if not records:
            return 0

        dimension = batch_dimension(records, self._dimensions.get(name))
        prepared = [_Entry(record=record, unit=normalize(record.embedding)) for record in records]

        bucket = self._collections.setdefault(name, {})
        for entry in prepared:
            bucket[entry.record.id] = entry
        self._dimensions[name] = dimension

        logger.debug("vector.memory.upsert", collection=name, records=len(prepared), dim=dimension)
        return len(prepared)

    async def delete(self, collection: str, ids: Sequence[str]) -> int:
        """Remove records by id.

        Args:
            collection: Collection to delete from.
            ids: Record ids; unknown ids are ignored.

        Returns:
            The number of records actually removed.
        """
        name = validate_collection(collection)
        bucket = self._collections.get(name)
        if not bucket or not ids:
            return 0

        removed = sum(1 for identifier in ids if bucket.pop(identifier, None) is not None)
        if not bucket:
            self._forget(name)
        logger.debug("vector.memory.delete", collection=name, removed=removed)
        return removed

    async def clear(self, collection: str) -> int:
        """Drop *collection* entirely, forgetting its embedding width.

        Args:
            collection: Collection to empty.

        Returns:
            The number of records removed.
        """
        name = validate_collection(collection)
        bucket = self._collections.get(name)
        removed = len(bucket) if bucket else 0
        self._forget(name)
        logger.debug("vector.memory.clear", collection=name, removed=removed)
        return removed

    # -- reads -----------------------------------------------------------------------------

    async def count(self, collection: str) -> int:
        """Return how many records *collection* holds (``0`` when unknown)."""
        return len(self._collections.get(validate_collection(collection), ()))

    async def list_collections(self) -> list[str]:
        """Return the names of all non-empty collections, sorted ascending."""
        return sorted(name for name, bucket in self._collections.items() if bucket)

    async def query(
        self,
        collection: str,
        embedding: Sequence[float],
        *,
        k: int = DEFAULT_TOP_K,
        filters: Filter | None = None,
    ) -> list[VectorHit]:
        """Return the *k* records most similar to *embedding*, filtered first.

        Args:
            collection: Collection to search. Unknown or empty yields ``[]``.
            embedding: Query vector, of the collection's width.
            k: Maximum number of hits. A *k* larger than the candidate set simply returns
                every candidate.
            filters: Metadata filter applied before scoring.

        Returns:
            Hits sorted by descending score, ties broken by ascending id.

        Raises:
            VectorStoreError: If *k* is not positive, a filter key is not an identifier, or
                the query width differs from the collection's (both widths are named).
        """
        name = validate_collection(collection)
        validate_k(k)
        active_filters = validate_filters(filters)

        bucket = self._collections.get(name)
        if not bucket:
            return []

        check_dimension(len(embedding), self._dimensions[name], context=f"collection {name!r}")
        query_unit = normalize(embedding)

        hits = [
            entry.record.to_hit(dot(query_unit, entry.unit))
            for entry in bucket.values()
            if matches_filters(entry.record.metadata, active_filters)
        ]
        ranked = rank_hits(hits, k)
        logger.debug(
            "vector.memory.query",
            collection=name,
            candidates=len(hits),
            returned=len(ranked),
            k=k,
        )
        return ranked

    # -- lifecycle ---------------------------------------------------------------------------

    async def close(self) -> None:
        """Release everything the store holds.

        Present so callers can shut any backend down uniformly; here it simply empties the
        maps, since there is no connection to close.
        """
        self._collections.clear()
        self._dimensions.clear()

    async def healthcheck(self) -> bool:
        """Return ``True`` — an in-process store is reachable whenever the process is."""
        return True

    # -- internals -----------------------------------------------------------------------------

    def _forget(self, name: str) -> None:
        """Drop *name*'s bucket and its width binding, if present."""
        self._collections.pop(name, None)
        self._dimensions.pop(name, None)
