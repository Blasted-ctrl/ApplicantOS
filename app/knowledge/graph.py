"""The knowledge graph — entity resolution, typed edges, and the view the desktop renders.

``docs/CONTRACTS.md`` §8.3 gives this module one job: turn the stream of
:class:`~app.knowledge.analyzers.base.ExtractedEntity` and
:class:`~app.knowledge.analyzers.base.ExtractedEdge` values that analyzers produce into a
*stable* graph, where "Node.js" seen in a README and "NodeJS" seen in a resume are one node
that grew stronger rather than two nodes that compete.

Three ideas carry the whole module.

**Resolution, not insertion.** Every write goes through
:meth:`KnowledgeGraph.upsert_entity`, which resolves on
``(user_id, kind, normalized_name)`` — the table's unique key — and *merges* on a hit.
Merging is strictly additive: aliases union, the longer summary wins, attributes are
shallow-merged with the incumbent winning unless the new observation is more confident,
:attr:`~app.models.knowledge.KnowledgeEntity.mention_count` increments, and confidence is
raised toward 1.0 by :data:`ENTITY_CONFIDENCE_REINFORCEMENT` (see
:meth:`KnowledgeStore.reinforce_confidence` for the formula and its properties). Nothing a
previous index learned is ever overwritten by a later one.

**A lost race is not an error.** Two workers indexing two sources that both mention Python
will both miss the ``SELECT`` and both try to ``INSERT``. The loser catches
:class:`~sqlalchemy.exc.IntegrityError` *inside a SAVEPOINT* (``session.begin_nested()``)
and re-selects, so the outer transaction survives — catching the error without a savepoint
would poison the session and roll back everything the caller had already done.

**A dangling edge is a rendering bug.** :meth:`KnowledgeGraph.subgraph` selects nodes
first, then returns **only** edges whose *both* endpoints are in the selected node set. A
force-directed canvas handed an edge pointing at an absent node either throws or silently
drops the node's position, so this filter is load-bearing rather than defensive.

The module also owns :class:`KnowledgeStore`, the small shared substrate that
:class:`KnowledgeGraph`, :class:`~app.knowledge.facts.FactStore` and
:class:`~app.knowledge.memory.MemoryStore` are all built on: it resolves the embedder and
the vector store lazily, and **degrades to SQL-only behaviour with a single log line** when
either is unavailable, because ApplicantOS is required to run offline with zero API keys.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.database.types import utcnow
from app.models.enums import EntityKind, RelationKind
from app.models.knowledge import (
    DEFAULT_EDGE_WEIGHT,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeFact,
)
from app.schemas.knowledge import GraphEdge, GraphNode, GraphView

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ai.embeddings import Embedder
    from app.knowledge.analyzers.base import ExtractedEdge, ExtractedEntity
    from app.knowledge.vector.base import VectorHit, VectorRecord, VectorStore

__all__ = [
    "DEFAULT_SUBGRAPH_LIMIT",
    "EDGE_ENDPOINT_CONFIDENCE",
    "EDGE_EVIDENCE_LOG_KEY",
    "ENTITY_CONFIDENCE_REINFORCEMENT",
    "MAX_EDGE_EVIDENCE_ENTRIES",
    "MAX_NEIGHBOR_DEPTH",
    "MAX_NEIGHBOR_NODES",
    "MAX_SUBGRAPH_EDGES",
    "MAX_SUBGRAPH_LIMIT",
    "SQL_IN_CHUNK_SIZE",
    "KnowledgeGraph",
    "KnowledgeStore",
    "chunked",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Ids per ``IN (...)`` clause. SQLite historically caps a statement at 999 bound
#: parameters (``SQLITE_MAX_VARIABLE_NUMBER``); chunking below that keeps every query in
#: this package portable between the SQLite and PostgreSQL installs without the caller
#: having to think about it.
SQL_IN_CHUNK_SIZE: Final[int] = 400

#: How far confidence moves toward certainty on each corroborating observation. See
#: :meth:`KnowledgeStore.reinforce_confidence` for the formula.
ENTITY_CONFIDENCE_REINFORCEMENT: Final[float] = 0.25

#: Upper bound every confidence is asymptotically raised toward, never reached by
#: reinforcement alone.
CONFIDENCE_CEILING: Final[float] = 1.0

#: Confidence given to an entity that is created only because an edge referred to it. Below
#: the extractor default (0.5) on purpose: the only evidence for the node is that something
#: else was said to relate to it, which is weaker than having been extracted in its own
#: right. A later :meth:`KnowledgeGraph.upsert_entity` raises it.
EDGE_ENDPOINT_CONFIDENCE: Final[float] = 0.4

#: Key under which :meth:`KnowledgeGraph.upsert_edge` keeps its bounded observation log
#: inside ``KnowledgeEdge.evidence``.
EDGE_EVIDENCE_LOG_KEY: Final[str] = "observations"

#: Hard cap on that log. An edge between two popular technologies is re-observed on every
#: index of every source forever; without a cap its ``evidence`` JSON would grow without
#: bound and eventually dominate the row. The **most recent** entries are kept, because
#: recent provenance is what a user asking "why is this here?" wants to see.
MAX_EDGE_EVIDENCE_ENTRIES: Final[int] = 25

#: Ceiling applied to the ``depth`` argument of :meth:`KnowledgeGraph.neighbors`. A
#: knowledge graph of a few thousand nodes is small-world: past this the frontier is
#: effectively the whole graph, so a deeper walk costs time and returns noise.
MAX_NEIGHBOR_DEPTH: Final[int] = 6

#: Hard cap on how many distinct entities one :meth:`KnowledgeGraph.neighbors` walk may
#: return, whatever the depth. Bounds both the query fan-out and the caller's prompt.
MAX_NEIGHBOR_NODES: Final[int] = 500

#: Default and maximum node counts for :meth:`KnowledgeGraph.subgraph`. The default matches
#: ``docs/CONTRACTS.md`` §8.3; the maximum is what the desktop canvas can lay out while
#: holding the 60fps interaction budget in ``docs/UI.md``.
DEFAULT_SUBGRAPH_LIMIT: Final[int] = 500
MAX_SUBGRAPH_LIMIT: Final[int] = 2000

#: Hard cap on edges returned by :meth:`KnowledgeGraph.subgraph`. Reached only by a very
#: dense graph; the strongest edges (highest ``weight``) survive truncation.
MAX_SUBGRAPH_EDGES: Final[int] = 4000

#: ``stats`` keys that describe the graph as a whole rather than one kind. Entity kinds are
#: reported as ``entity_<kind>`` and relations as ``relation_<relation>``, so no
#: :class:`~app.models.enums.EntityKind` or :class:`~app.models.enums.RelationKind` value
#: can ever collide with a total.
STATS_TOTAL_ENTITIES: Final[str] = "entities"
STATS_TOTAL_EDGES: Final[str] = "edges"
STATS_NODES_RETURNED: Final[str] = "nodes_returned"
STATS_EDGES_RETURNED: Final[str] = "edges_returned"

#: ``1`` when the returned view is a proper subset of the graph — either the node limit bit
#: or a ``kinds`` filter was applied — and ``0`` when it is the whole thing. An ``int``
#: rather than a ``bool`` because ``GraphView.stats`` is typed ``dict[str, int]``.
STATS_TRUNCATED: Final[str] = "truncated"

#: Prefixes for the per-kind entries of :meth:`KnowledgeGraph.stats`.
STATS_ENTITY_KIND_PREFIX: Final[str] = "entity_"
STATS_RELATION_PREFIX: Final[str] = "relation_"

#: Events already logged by :meth:`KnowledgeStore._warn_once`, so a degraded installation
#: says so once rather than on every request. Module-level (not per instance) because the
#: condition being reported — no vector store, no embedder — is a property of the process,
#: not of one store object.
_WARNED_EVENTS: set[str] = set()


def chunked(values: Sequence[Any], size: int = SQL_IN_CHUNK_SIZE) -> Iterable[list[Any]]:
    """Yield *values* in lists of at most *size* items.

    Used to keep every ``IN (...)`` clause in the knowledge package below SQLite's bound
    parameter limit. Defined here rather than three times over; the fact and memory stores
    import it.

    Args:
        values: The items to split. May be empty, in which case nothing is yielded.
        size: Maximum items per chunk. Must be positive.

    Yields:
        Consecutive slices of *values*, in order.

    Raises:
        ValueError: If *size* is not positive, which would loop forever.
    """
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


# ======================================================================================
# Shared substrate
# ======================================================================================


class KnowledgeStore:
    """Session, embedder and vector store plumbing shared by the three knowledge stores.

    :class:`KnowledgeGraph`, :class:`~app.knowledge.facts.FactStore` and
    :class:`~app.knowledge.memory.MemoryStore` all take the same constructor arguments and
    all need the same two things from the outside world: a way to embed text and a vector
    index to search. Both are resolved **lazily and defensively**:

    * nothing is constructed in ``__init__``, so a store is safe to build in a request
      handler, a worker task, or a test with no configuration at all;
    * a failure to resolve — no vector backend, an unopenable database file, a missing
      dependency — is logged exactly once per process and then treated as "not available",
      after which every vector operation is a no-op and callers fall back to SQL.

    That degradation is a hard product requirement, not a convenience: ApplicantOS must run
    end to end offline with zero API keys, and a knowledge base that refuses to answer
    because a vector extension is missing would fail that.

    Attributes:
        session: The async session every query runs on. Owned by the caller — this class
            never commits, so a service can compose several stores in one transaction.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        """Bind the store to a session, optionally overriding its collaborators.

        Args:
            session: The async session to run queries on.
            embedder: Embedder to use; resolved from settings on first use when omitted.
            vector_store: Vector index to use; resolved from settings on first use when
                omitted. Pass an
                :class:`~app.knowledge.vector.memory_store.InMemoryVectorStore` in tests.
        """
        self.session = session
        self._embedder = embedder
        self._vector_store = vector_store
        self._vector_resolved = vector_store is not None

    # -- collaborators ----------------------------------------------------------------

    @staticmethod
    def _warn_once(event: str, **fields: Any) -> None:
        """Log *event* at warning level at most once per process.

        Args:
            event: The structlog event name, also the deduplication key.
            **fields: Additional bound fields.
        """
        if event in _WARNED_EVENTS:
            return
        _WARNED_EVENTS.add(event)
        logger.warning(event, **fields)

    @property
    def vector_store(self) -> VectorStore | None:
        """The vector index, or ``None`` when this installation has none.

        Resolved once per store instance through
        :func:`app.knowledge.vector.get_vector_store` (itself a process-wide singleton).
        A resolution failure is logged once and cached as ``None`` rather than retried on
        every call, so a misconfigured backend costs one log line instead of one exception
        per query.
        """
        if self._vector_resolved:
            return self._vector_store
        self._vector_resolved = True
        try:
            from app.knowledge.vector import get_vector_store

            self._vector_store = get_vector_store()
        except Exception as exc:
            self._vector_store = None
            self._warn_once(
                "knowledge.vector_store_unavailable",
                error=str(exc),
                detail="semantic search degraded to SQL keyword matching",
            )
        return self._vector_store

    async def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        """Embed *texts* in one batched call, or return ``None`` if embedding is broken.

        Always goes through :func:`app.ai.embeddings.embed_texts`, which collapses
        duplicates, serves cache hits, and sends only the misses upstream. **Callers must
        never embed one item at a time in a loop** — a re-index presents thousands of
        strings and per-item calls turn a second of work into minutes.

        Args:
            texts: The texts to embed, in the order vectors must come back in.

        Returns:
            One vector per input, or ``None`` when no embedder could be used. ``None`` is
            distinct from ``[]`` (an empty request), so a caller can tell "nothing to do"
            from "vectors unavailable" and skip the semantic path accordingly.
        """
        if not texts:
            return []
        try:
            from app.ai.embeddings import embed_texts

            return await embed_texts(list(texts), embedder=self._embedder)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once(
                "knowledge.embedder_unavailable",
                error=str(exc),
                detail="semantic search degraded to SQL keyword matching",
            )
            return None

    # -- guarded vector operations ----------------------------------------------------

    async def vector_upsert(self, collection: str, records: Sequence[VectorRecord]) -> int:
        """Write *records* to *collection*, tolerating an unavailable index.

        Args:
            collection: Target collection.
            records: Records to store; an empty sequence is a no-op.

        Returns:
            The number of records written, or ``0`` when there is no usable vector store.
        """
        store = self.vector_store
        if store is None or not records:
            return 0
        try:
            return await store.upsert(collection, list(records))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "knowledge.vector_upsert_failed",
                collection=collection,
                records=len(records),
                error=str(exc),
            )
            return 0

    async def vector_query(
        self,
        collection: str,
        embedding: Sequence[float],
        *,
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """Search *collection*, returning ``[]`` when the index is unavailable.

        Args:
            collection: Collection to search.
            embedding: Query vector.
            k: Maximum hits. Values below one short-circuit to ``[]``.
            filters: Metadata filter; see :mod:`app.knowledge.vector.base`.

        Returns:
            Hits sorted best-first, or ``[]`` on any failure.
        """
        store = self.vector_store
        if store is None or k < 1 or not embedding:
            return []
        try:
            return await store.query(collection, embedding, k=k, filters=filters)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "knowledge.vector_query_failed",
                collection=collection,
                k=k,
                error=str(exc),
            )
            return []

    @staticmethod
    def reinforce_confidence(incumbent: float, observed: float) -> float:
        """Raise a stored confidence toward certainty given a corroborating observation.

        The one confidence formula in the knowledge engine — entities reinforce with it on
        every re-mention, and facts reinforce with it on every merge, so "seen again" means
        the same thing everywhere. It is::

            best  = max(incumbent, observed)
            value = best + (1 - best) * ENTITY_CONFIDENCE_REINFORCEMENT * observed

        with three properties this needs:

        * **Monotonic.** The result is never below either input, so re-indexing can only
          strengthen what is already known.
        * **Bounded.** Each step closes a fixed fraction of the remaining gap to 1.0, so the
          value converges on certainty without ever exceeding it — no clamping required, and
          nothing ever claims more confidence than the evidence supports.
        * **Evidence-weighted.** The step scales with *observed*, so a hesitant extractor
          (0.3) moves the number far less than a curated vocabulary match (0.8).

        Args:
            incumbent: The stored confidence.
            observed: The new observation's confidence.

        Returns:
            The reinforced confidence, in ``[0.0, 1.0]``.
        """
        best = max(incumbent, observed)
        raised = best + (CONFIDENCE_CEILING - best) * (ENTITY_CONFIDENCE_REINFORCEMENT * observed)
        return min(CONFIDENCE_CEILING, raised)

    async def vector_delete(self, collection: str, ids: Sequence[str]) -> int:
        """Remove records by id, tolerating an unavailable index.

        Args:
            collection: Collection to delete from.
            ids: Record ids; unknown ids are ignored.

        Returns:
            The number of records removed, or ``0`` on any failure.
        """
        store = self.vector_store
        if store is None or not ids:
            return 0
        try:
            return await store.delete(collection, list(ids))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "knowledge.vector_delete_failed",
                collection=collection,
                ids=len(ids),
                error=str(exc),
            )
            return 0


# ======================================================================================
# The graph
# ======================================================================================


class KnowledgeGraph(KnowledgeStore):
    """Read and write the user's entity/edge graph (``docs/CONTRACTS.md`` §8.3).

    Every method is scoped to one ``user_id``: knowledge never crosses users, and the
    unique constraints the resolution logic depends on are all user-scoped.

    The class never commits. Callers own the transaction, which is what lets the indexer
    upsert a whole analysis result — documents, facts, entities, edges — atomically.
    """

    # ----------------------------------------------------------------------------------
    # Entities
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _union_aliases(*groups: Iterable[str]) -> list[str]:
        """Merge alias lists, preserving first-appearance order and folding case.

        Args:
            *groups: Alias iterables, most authoritative first.

        Returns:
            A new list — assigned rather than mutated in place, because the ``aliases``
            column is a plain JSON type with no mutation tracking, so an in-place ``append``
            would never be written back.
        """
        seen: set[str] = set()
        merged: list[str] = []
        for group in groups:
            for alias in group or ():
                text = str(alias).strip()
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(text)
        return merged

    @staticmethod
    def _merge_attributes(
        incumbent: dict[str, Any],
        incoming: dict[str, Any],
        *,
        incoming_wins: bool,
    ) -> dict[str, Any]:
        """Shallow-merge two attribute maps into a new dict.

        Args:
            incumbent: The stored attributes.
            incoming: The new observation's attributes.
            incoming_wins: Whether a key present in both takes the incoming value. The
                caller sets this from a confidence comparison, so a more confident
                observation may correct a less confident one but never the reverse.

        Returns:
            A new dict. Never mutated in place — see :meth:`_union_aliases`.
        """
        merged: dict[str, Any] = dict(incumbent or {})
        for key, value in (incoming or {}).items():
            if key not in merged or incoming_wins:
                merged[key] = value
        return merged

    async def _find_entity(
        self, user_id: uuid.UUID, kind: EntityKind, normalized_name: str
    ) -> KnowledgeEntity | None:
        """Return the entity identified by the table's unique key, or ``None``.

        Args:
            user_id: Owning user.
            kind: Node type.
            normalized_name: Deduplication key from
                :meth:`~app.models.knowledge.KnowledgeEntity.normalize`.

        Returns:
            The matching row, or ``None``.
        """
        statement = (
            select(KnowledgeEntity)
            .where(
                KnowledgeEntity.user_id == user_id,
                KnowledgeEntity.kind == kind,
                KnowledgeEntity.normalized_name == normalized_name,
            )
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def _insert_entity(
        self,
        user_id: uuid.UUID,
        *,
        kind: EntityKind,
        name: str,
        normalized_name: str,
        summary: str | None,
        attributes: dict[str, Any],
        aliases: list[str],
        confidence: float,
    ) -> tuple[KnowledgeEntity, bool]:
        """Insert a new entity, resolving a concurrent insert of the same key.

        The insert happens inside a SAVEPOINT (``session.begin_nested()``). Without one, an
        :class:`~sqlalchemy.exc.IntegrityError` raised by the flush would leave the *outer*
        transaction in a failed state, so catching it would not help — every subsequent
        statement in the caller's unit of work would fail too. With one, only the failed
        insert is rolled back and the loser of the race simply re-selects the winner's row.

        Args:
            user_id: Owning user.
            kind: Node type.
            name: Display name.
            normalized_name: Precomputed deduplication key.
            summary: Optional description.
            attributes: Kind-specific extras.
            aliases: Other surface forms.
            confidence: Extractor confidence.

        Returns:
            ``(entity, created)``. ``created`` is ``False`` when this call lost an insert
            race and the returned row is the winner's — which the caller must know, because
            the losing observation still has to be merged in rather than dropped.

        Raises:
            IntegrityError: If the insert failed for a reason other than losing this race —
                a foreign key to a user that does not exist, for example.
        """
        entity = KnowledgeEntity(
            user_id=user_id,
            kind=kind,
            name=name,
            summary=summary,
            attributes=attributes,
            aliases=aliases,
            confidence=confidence,
            mention_count=1,
        )
        # Assigned after construction: the model derives `normalized_name` from every
        # assignment to `name`, and this is the key we already resolved on.
        entity.normalized_name = normalized_name
        moment = utcnow()
        entity.first_seen_at = moment
        entity.last_seen_at = moment

        try:
            async with self.session.begin_nested():
                self.session.add(entity)
                await self.session.flush()
        except IntegrityError:
            if entity in self.session:  # pragma: no cover - savepoint usually expunges it
                self.session.expunge(entity)
            existing = await self._find_entity(user_id, kind, normalized_name)
            if existing is None:
                raise
            logger.debug(
                "graph.entity_insert_race",
                user_id=str(user_id),
                kind=kind.value,
                normalized_name=normalized_name,
            )
            return existing, False
        return entity, True

    async def upsert_entity(self, user_id: uuid.UUID, e: ExtractedEntity) -> KnowledgeEntity:
        """Resolve *e* against the user's graph, merging into the incumbent on a hit.

        Identity is ``(user_id, kind, normalized_name)``. On a miss the entity is inserted
        (see :meth:`_insert_entity` for the concurrency story). On a hit **nothing is
        overwritten**:

        =====================  ==========================================================
        Field                  Merge rule
        =====================  ==========================================================
        ``name``               Incumbent keeps its casing; the new surface form is
                               recorded as an alias when it differs.
        ``aliases``            Union, first-appearance order, case-insensitively deduped.
        ``summary``            The longer of the two — length is the only signal available
                               for "more informative" without asking a model.
        ``attributes``         Shallow merge; the incumbent wins a key present in both
                               unless the new observation is strictly more confident.
        ``mention_count``      Incremented by one. This is the prominence signal the
                               resume engine ranks by, so every observation must count.
        ``last_seen_at``       Set to now.
        ``confidence``         Raised by
                               :meth:`~KnowledgeStore.reinforce_confidence`.
        =====================  ==========================================================

        Args:
            user_id: Owning user.
            e: The extracted entity.

        Returns:
            The stored entity, flushed so its ``id`` is available to edge resolution.

        Raises:
            ValueError: If the name normalises to the empty string — a name made only of
                punctuation ("---", "///"). Every such entity would collide on the unique
                key, so it is rejected loudly rather than merged into a junk node.
        """
        kind = EntityKind(e.kind)
        normalized = KnowledgeEntity.normalize(e.name)
        if not normalized:
            raise ValueError(
                f"entity name {e.name!r} normalises to the empty string and cannot identify a node"
            )

        existing = await self._find_entity(user_id, kind, normalized)
        if existing is None:
            inserted, created = await self._insert_entity(
                user_id,
                kind=kind,
                name=e.name,
                normalized_name=normalized,
                summary=e.summary,
                attributes=dict(e.attributes or {}),
                aliases=list(e.aliases or []),
                confidence=e.confidence,
            )
            if created:
                return inserted
            # Lost the race. The observation is still an observation — falling through to
            # the merge below is what stops a concurrent index from silently discarding it.
            existing = inserted

        return await self._merge_entity(existing, e)

    async def _merge_entity(self, existing: KnowledgeEntity, e: ExtractedEntity) -> KnowledgeEntity:
        """Fold one observation into an existing entity, per :meth:`upsert_entity`'s table.

        Args:
            existing: The incumbent row, which owns identity.
            e: The new observation.

        Returns:
            *existing*, flushed.
        """
        incoming_aliases = list(e.aliases or [])
        if e.name and e.name != existing.name:
            incoming_aliases.append(e.name)
        existing.aliases = self._union_aliases(existing.aliases, incoming_aliases)

        if e.summary and len(e.summary) > len(existing.summary or ""):
            existing.summary = e.summary

        existing.attributes = self._merge_attributes(
            existing.attributes,
            e.attributes,
            incoming_wins=e.confidence > existing.confidence,
        )
        existing.confidence = self.reinforce_confidence(existing.confidence, e.confidence)
        existing.mention_count = int(existing.mention_count or 0) + 1
        existing.last_seen_at = utcnow()

        await self.session.flush()
        return existing

    # ----------------------------------------------------------------------------------
    # Edges
    # ----------------------------------------------------------------------------------

    async def _resolve_endpoint(
        self, user_id: uuid.UUID, endpoint: tuple[EntityKind, str]
    ) -> KnowledgeEntity:
        """Return the entity an edge endpoint names, creating a stub node if needed.

        Deliberately **does not** increment ``mention_count``. The indexer upserts every
        :class:`~app.knowledge.analyzers.base.ExtractedEntity` before it upserts the edges
        that reference them, so counting the edge's reference as well would double every
        entity's prominence and quietly corrupt bullet ranking.

        Args:
            user_id: Owning user.
            endpoint: The ``(kind, name)`` pair the edge carries.

        Returns:
            The resolved entity.

        Raises:
            ValueError: If the endpoint name normalises to the empty string.
        """
        kind, name = EntityKind(endpoint[0]), str(endpoint[1])
        normalized = KnowledgeEntity.normalize(name)
        if not normalized:
            raise ValueError(
                f"edge endpoint name {name!r} normalises to the empty string and cannot "
                "identify a node"
            )
        existing = await self._find_entity(user_id, kind, normalized)
        if existing is not None:
            return existing
        entity, _created = await self._insert_entity(
            user_id,
            kind=kind,
            name=name,
            normalized_name=normalized,
            summary=None,
            attributes={},
            aliases=[],
            confidence=EDGE_ENDPOINT_CONFIDENCE,
        )
        return entity

    @staticmethod
    def _merge_evidence(incumbent: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        """Fold a new observation into an edge's evidence, keeping the log bounded.

        Scalar keys ("extractor", "project") are shallow-merged with the incumbent winning,
        so the first attribution stays stable. Everything else accumulates under
        :data:`EDGE_EVIDENCE_LOG_KEY` as a list capped at
        :data:`MAX_EDGE_EVIDENCE_ENTRIES`, **oldest entries dropped first**.

        The cap is the whole point of this method: an edge like "Python *used_in*
        ApplicantOS" is re-observed on every index of every source, and an unbounded
        evidence list would grow until one JSON column dwarfed the rest of the table.

        Args:
            incumbent: The stored evidence.
            incoming: The new observation's evidence.

        Returns:
            A new dict; the caller assigns it, since the column has no mutation tracking.
        """
        merged = {
            key: value for key, value in (incumbent or {}).items() if key != EDGE_EVIDENCE_LOG_KEY
        }
        log: list[Any] = list((incumbent or {}).get(EDGE_EVIDENCE_LOG_KEY) or [])

        payload = {
            key: value for key, value in (incoming or {}).items() if key != EDGE_EVIDENCE_LOG_KEY
        }
        for key, value in payload.items():
            merged.setdefault(key, value)

        candidates: list[Any] = []
        if payload:
            candidates.append(payload)
        candidates.extend((incoming or {}).get(EDGE_EVIDENCE_LOG_KEY) or [])
        for entry in candidates:
            if entry not in log:
                log.append(entry)

        if len(log) > MAX_EDGE_EVIDENCE_ENTRIES:
            log = log[-MAX_EDGE_EVIDENCE_ENTRIES:]
        if log:
            merged[EDGE_EVIDENCE_LOG_KEY] = log
        return merged

    async def _find_edge(
        self,
        source_entity_id: uuid.UUID,
        target_entity_id: uuid.UUID,
        relation: RelationKind,
    ) -> KnowledgeEdge | None:
        """Return the edge identified by the table's unique triple, or ``None``.

        Args:
            source_entity_id: Subject.
            target_entity_id: Object.
            relation: Relationship type.

        Returns:
            The matching row, or ``None``.
        """
        statement = (
            select(KnowledgeEdge)
            .where(
                KnowledgeEdge.source_entity_id == source_entity_id,
                KnowledgeEdge.target_entity_id == target_entity_id,
                KnowledgeEdge.relation == relation,
            )
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def upsert_edge(self, user_id: uuid.UUID, edge: ExtractedEdge) -> KnowledgeEdge:
        """Resolve both endpoints and strengthen (or create) the relationship between them.

        Uniqueness is ``(source_entity_id, target_entity_id, relation)``. A repeated
        observation accumulates :attr:`~app.models.knowledge.KnowledgeEdge.weight` — which
        is exactly what makes "Python used_in ApplicantOS" outrank a one-off mention during
        graph expansion — and appends to a **capped** evidence log (see
        :meth:`_merge_evidence`). As with entities, a lost insert race is resolved inside a
        SAVEPOINT and re-selected.

        Args:
            user_id: Owning user.
            edge: The extracted relationship.

        Returns:
            The stored edge, flushed.

        Raises:
            ValueError: If either endpoint name normalises to the empty string.
        """
        source = await self._resolve_endpoint(user_id, edge.source)
        target = await self._resolve_endpoint(user_id, edge.target)
        relation = RelationKind(edge.relation)
        weight = float(edge.weight if edge.weight is not None else DEFAULT_EDGE_WEIGHT)

        existing = await self._find_edge(source.id, target.id, relation)
        if existing is not None:
            existing.weight = float(existing.weight or 0.0) + weight
            existing.evidence = self._merge_evidence(existing.evidence, edge.evidence)
            await self.session.flush()
            return existing

        row = KnowledgeEdge(
            user_id=user_id,
            source_entity_id=source.id,
            target_entity_id=target.id,
            relation=relation,
            weight=weight,
            evidence=self._merge_evidence({}, edge.evidence),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            if row in self.session:  # pragma: no cover - savepoint usually expunges it
                self.session.expunge(row)
            existing = await self._find_edge(source.id, target.id, relation)
            if existing is None:
                raise
            logger.debug(
                "graph.edge_insert_race",
                user_id=str(user_id),
                relation=relation.value,
            )
            existing.weight = float(existing.weight or 0.0) + weight
            existing.evidence = self._merge_evidence(existing.evidence, edge.evidence)
            await self.session.flush()
            return existing
        return row

    # ----------------------------------------------------------------------------------
    # Traversal
    # ----------------------------------------------------------------------------------

    async def neighbors(
        self,
        entity_id: uuid.UUID,
        *,
        relations: Sequence[RelationKind] | None = None,
        depth: int = 1,
    ) -> list[KnowledgeEntity]:
        """Walk outward from *entity_id* in **both** directions and return what it reaches.

        Direction is deliberately ignored during the walk. "PyTorch *used_in* PoseNet" is
        the same piece of knowledge whether the question was "what did they build with
        PyTorch?" or "what did PoseNet use?", and a directed walk would answer only one of
        them.

        Termination is guaranteed by three independent bounds: a ``visited`` set (so a
        cycle — and this graph is full of them — is walked once), *depth* clamped to
        :data:`MAX_NEIGHBOR_DEPTH`, and a hard cap of :data:`MAX_NEIGHBOR_NODES` distinct
        entities.

        Args:
            entity_id: The node to start from. Excluded from the result.
            relations: Restrict traversal to these relationship types; ``None`` walks all.
            depth: How many hops to take. Values below one return ``[]``; values above
                :data:`MAX_NEIGHBOR_DEPTH` are clamped with a log line.

        Returns:
            The reached entities, ordered by hop distance (nearest first), then by
            descending ``mention_count``, then by ``normalized_name`` — so the result is
            deterministic and the most prominent neighbours lead.
        """
        if depth < 1:
            return []
        if depth > MAX_NEIGHBOR_DEPTH:
            logger.debug(
                "graph.neighbors_depth_clamped",
                requested=depth,
                maximum=MAX_NEIGHBOR_DEPTH,
            )
            depth = MAX_NEIGHBOR_DEPTH

        relation_filter = [RelationKind(relation) for relation in relations] if relations else None

        visited: set[uuid.UUID] = {entity_id}
        levels: dict[uuid.UUID, int] = {}
        frontier: list[uuid.UUID] = [entity_id]

        for hop in range(1, depth + 1):
            if not frontier or len(levels) >= MAX_NEIGHBOR_NODES:
                break
            discovered: list[uuid.UUID] = []
            for chunk in chunked(frontier):
                statement = select(
                    KnowledgeEdge.source_entity_id, KnowledgeEdge.target_entity_id
                ).where(
                    or_(
                        KnowledgeEdge.source_entity_id.in_(chunk),
                        KnowledgeEdge.target_entity_id.in_(chunk),
                    )
                )
                if relation_filter is not None:
                    statement = statement.where(KnowledgeEdge.relation.in_(relation_filter))
                for source_id, target_id in (await self.session.execute(statement)).all():
                    for candidate in (source_id, target_id):
                        if candidate in visited:
                            continue
                        visited.add(candidate)
                        levels[candidate] = hop
                        discovered.append(candidate)
                        if len(levels) >= MAX_NEIGHBOR_NODES:
                            break
                    if len(levels) >= MAX_NEIGHBOR_NODES:
                        break
                if len(levels) >= MAX_NEIGHBOR_NODES:
                    break
            frontier = discovered

        if not levels:
            return []
        if len(levels) >= MAX_NEIGHBOR_NODES:
            logger.debug(
                "graph.neighbors_truncated",
                entity_id=str(entity_id),
                limit=MAX_NEIGHBOR_NODES,
            )

        entities = await self._load_entities(list(levels))
        entities.sort(
            key=lambda entity: (
                levels.get(entity.id, MAX_NEIGHBOR_DEPTH + 1),
                -int(entity.mention_count or 0),
                entity.normalized_name,
            )
        )
        return entities

    async def _load_entities(self, entity_ids: Sequence[uuid.UUID]) -> list[KnowledgeEntity]:
        """Load entities by id in chunked ``IN`` queries.

        Args:
            entity_ids: The ids to load; unknown ids are silently absent from the result.

        Returns:
            The rows, in whatever order the database returned them — every caller sorts.
        """
        loaded: list[KnowledgeEntity] = []
        for chunk in chunked(list(entity_ids)):
            statement = select(KnowledgeEntity).where(KnowledgeEntity.id.in_(chunk))
            loaded.extend((await self.session.execute(statement)).scalars().all())
        return loaded

    # ----------------------------------------------------------------------------------
    # The rendered view
    # ----------------------------------------------------------------------------------

    async def subgraph(
        self,
        user_id: uuid.UUID,
        *,
        kinds: Sequence[EntityKind] | None = None,
        limit: int = DEFAULT_SUBGRAPH_LIMIT,
    ) -> GraphView:
        """Return a renderable slice of the user's graph, nodes first.

        Nodes are the highest-``mention_count`` entities (ties broken by confidence, then
        name, so the same graph renders identically twice). Edges are then filtered to
        those whose **both** endpoints are in that node set — a dangling edge crashes or
        corrupts a force-directed canvas, so this is a correctness requirement of the
        desktop renderer rather than a nicety.

        ``stats`` describes the **whole** graph (see :meth:`stats`) plus how much of it this
        view contains, so the UI can say "showing 500 of 3,214 nodes" honestly.

        Args:
            user_id: Owning user.
            kinds: Restrict nodes to these entity kinds; ``None`` includes all.
            limit: Maximum nodes, clamped to ``[1, MAX_SUBGRAPH_LIMIT]``.

        Returns:
            A :class:`~app.schemas.knowledge.GraphView` ready to serialise.
        """
        effective_limit = max(1, min(int(limit), MAX_SUBGRAPH_LIMIT))

        statement = select(KnowledgeEntity).where(KnowledgeEntity.user_id == user_id)
        if kinds:
            statement = statement.where(
                KnowledgeEntity.kind.in_([EntityKind(kind) for kind in kinds])
            )
        statement = statement.order_by(
            KnowledgeEntity.mention_count.desc(),
            KnowledgeEntity.confidence.desc(),
            KnowledgeEntity.normalized_name.asc(),
        ).limit(effective_limit)

        entities = list((await self.session.execute(statement)).scalars().all())
        node_ids = {entity.id for entity in entities}
        fact_counts = await self._fact_counts(node_ids)

        nodes = [
            GraphNode(
                id=entity.id,
                kind=entity.kind,
                label=entity.name,
                summary=entity.summary,
                mention_count=int(entity.mention_count or 0),
                confidence=float(entity.confidence or 0.0),
                fact_count=fact_counts.get(entity.id, 0),
            )
            for entity in entities
        ]

        edges = [
            GraphEdge(
                id=edge.id,
                source=edge.source_entity_id,
                target=edge.target_entity_id,
                relation=edge.relation,
                weight=float(edge.weight or 0.0),
            )
            for edge in await self._edges_within(user_id, node_ids)
        ]

        stats = await self.stats(user_id)
        stats[STATS_NODES_RETURNED] = len(nodes)
        stats[STATS_EDGES_RETURNED] = len(edges)
        stats[STATS_TRUNCATED] = int(
            len(nodes) < stats.get(STATS_TOTAL_ENTITIES, 0) or kinds is not None
        )

        logger.debug(
            "graph.subgraph",
            user_id=str(user_id),
            nodes=len(nodes),
            edges=len(edges),
            limit=effective_limit,
        )
        return GraphView(nodes=nodes, edges=edges, stats=stats)

    async def _fact_counts(self, entity_ids: set[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Count the active facts anchored to each of *entity_ids*.

        Args:
            entity_ids: The entities to count for.

        Returns:
            Entity id to active fact count. Entities with no facts are absent.
        """
        if not entity_ids:
            return {}
        counts: dict[uuid.UUID, int] = {}
        for chunk in chunked(sorted(entity_ids)):
            statement = (
                select(KnowledgeFact.entity_id, func.count(KnowledgeFact.id))
                .where(
                    KnowledgeFact.entity_id.in_(chunk),
                    KnowledgeFact.is_active.is_(True),
                )
                .group_by(KnowledgeFact.entity_id)
            )
            for entity_id, count in (await self.session.execute(statement)).all():
                if entity_id is not None:
                    counts[entity_id] = int(count)
        return counts

    async def _edges_within(
        self, user_id: uuid.UUID, node_ids: set[uuid.UUID]
    ) -> list[KnowledgeEdge]:
        """Return the edges whose *both* endpoints are in *node_ids*.

        Queried by source in chunks and filtered by target in Python. Doing it the other
        way — two ``IN`` clauses over the same 500-id list — doubles the bound parameters
        for no gain, and the target check is a set membership test on data already in hand.

        Args:
            user_id: Owning user.
            node_ids: The selected node set.

        Returns:
            At most :data:`MAX_SUBGRAPH_EDGES` edges, strongest first.
        """
        if not node_ids:
            return []
        collected: list[KnowledgeEdge] = []
        for chunk in chunked(sorted(node_ids)):
            statement = select(KnowledgeEdge).where(
                KnowledgeEdge.user_id == user_id,
                KnowledgeEdge.source_entity_id.in_(chunk),
            )
            collected.extend(
                edge
                for edge in (await self.session.execute(statement)).scalars().all()
                if edge.target_entity_id in node_ids
            )
        collected.sort(key=lambda edge: (-float(edge.weight or 0.0), str(edge.id)))
        if len(collected) > MAX_SUBGRAPH_EDGES:
            logger.debug(
                "graph.subgraph_edges_truncated",
                user_id=str(user_id),
                edges=len(collected),
                limit=MAX_SUBGRAPH_EDGES,
            )
        return collected[:MAX_SUBGRAPH_EDGES]

    # ----------------------------------------------------------------------------------
    # Maintenance
    # ----------------------------------------------------------------------------------

    async def merge_entities(self, keep_id: uuid.UUID, drop_id: uuid.UUID) -> KnowledgeEntity:
        """Fold the *drop* entity into the *keep* entity and delete it.

        The manual correction path: normalisation cannot know that "MIT" and
        "Massachusetts Institute of Technology" are one organization, so the user says so
        and this executes it. Everything moves — edges are repointed (collapsing into the
        surviving edge when that produces a duplicate triple, and dropped entirely when it
        produces a self-loop), facts are re-anchored, aliases union, mention counts sum,
        the earliest ``first_seen_at`` and latest ``last_seen_at`` win.

        **Idempotent.** Calling it twice with the same arguments is a no-op the second time:
        a missing *drop* row means the merge already happened, and ``keep_id == drop_id`` is
        answered without touching anything.

        Args:
            keep_id: The surviving entity.
            drop_id: The entity to fold in and delete.

        Returns:
            The surviving entity, flushed.

        Raises:
            ValueError: If *keep_id* does not exist, or the two entities belong to
                different users — merging across users would leak one person's knowledge
                into another's graph.
        """
        keep = await self.session.get(KnowledgeEntity, keep_id)
        if keep is None:
            raise ValueError(f"entity {keep_id} does not exist and cannot be merged into")
        if keep_id == drop_id:
            return keep

        drop = await self.session.get(KnowledgeEntity, drop_id)
        if drop is None:
            logger.debug("graph.merge_entities_noop", keep_id=str(keep_id), drop_id=str(drop_id))
            return keep
        if drop.user_id != keep.user_id:
            raise ValueError(
                f"cannot merge entity {drop_id} (user {drop.user_id}) into {keep_id} "
                f"(user {keep.user_id}): knowledge never crosses users"
            )

        await self._repoint_edges(keep_id, drop_id)

        await self.session.execute(
            update(KnowledgeFact)
            .where(KnowledgeFact.entity_id == drop_id)
            .values(entity_id=keep_id)
            .execution_options(synchronize_session="fetch")
        )

        keep.aliases = self._union_aliases(
            keep.aliases, drop.aliases, [drop.name] if drop.name != keep.name else []
        )
        if drop.summary and len(drop.summary) > len(keep.summary or ""):
            keep.summary = drop.summary
        keep.attributes = self._merge_attributes(
            keep.attributes, drop.attributes, incoming_wins=False
        )
        keep.mention_count = int(keep.mention_count or 0) + int(drop.mention_count or 0)
        keep.confidence = max(float(keep.confidence or 0.0), float(drop.confidence or 0.0))
        keep.first_seen_at = min(keep.first_seen_at, drop.first_seen_at)
        keep.last_seen_at = max(keep.last_seen_at, drop.last_seen_at)

        await self.session.delete(drop)
        await self.session.flush()

        logger.info(
            "graph.entities_merged",
            keep_id=str(keep_id),
            drop_id=str(drop_id),
            mention_count=keep.mention_count,
        )
        return keep

    async def _repoint_edges(self, keep_id: uuid.UUID, drop_id: uuid.UUID) -> None:
        """Move every edge touching *drop_id* onto *keep_id*, resolving collisions.

        Three cases arise, and all three have to be handled before anything is flushed,
        because the surviving rows must not violate
        ``UNIQUE(source_entity_id, target_entity_id, relation)`` mid-flush:

        * the repointed triple already exists — merge weight and evidence into the survivor
          and delete the moved edge;
        * the repointed triple is a self-loop (both endpoints become *keep*) — delete it,
          since "X relates to X" carries no information;
        * otherwise — rewrite the endpoint.

        Deletions are flushed before the updates so the database never sees two rows
        claiming the same triple.

        Args:
            keep_id: The surviving entity.
            drop_id: The entity being folded in.
        """
        moving = list(
            (
                await self.session.execute(
                    select(KnowledgeEdge).where(
                        or_(
                            KnowledgeEdge.source_entity_id == drop_id,
                            KnowledgeEdge.target_entity_id == drop_id,
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        if not moving:
            return

        surviving = list(
            (
                await self.session.execute(
                    select(KnowledgeEdge).where(
                        or_(
                            KnowledgeEdge.source_entity_id == keep_id,
                            KnowledgeEdge.target_entity_id == keep_id,
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        index: dict[tuple[uuid.UUID, uuid.UUID, RelationKind], KnowledgeEdge] = {
            (edge.source_entity_id, edge.target_entity_id, edge.relation): edge
            for edge in surviving
            if edge.source_entity_id != drop_id and edge.target_entity_id != drop_id
        }

        rewrites: list[tuple[KnowledgeEdge, uuid.UUID, uuid.UUID]] = []
        for edge in moving:
            source = keep_id if edge.source_entity_id == drop_id else edge.source_entity_id
            target = keep_id if edge.target_entity_id == drop_id else edge.target_entity_id
            key = (source, target, edge.relation)

            if source == target:
                await self.session.delete(edge)
                continue
            winner = index.get(key)
            if winner is not None and winner is not edge:
                winner.weight = float(winner.weight or 0.0) + float(edge.weight or 0.0)
                winner.evidence = self._merge_evidence(winner.evidence, edge.evidence)
                await self.session.delete(edge)
                continue
            rewrites.append((edge, source, target))
            index[key] = edge

        # Deletes first: an UPDATE that collides with a row still present would fail the
        # unique constraint, and SQLAlchemy emits inserts and updates before deletes.
        await self.session.flush()
        for edge, source, target in rewrites:
            edge.source_entity_id = source
            edge.target_entity_id = target
        await self.session.flush()

    async def stats(self, user_id: uuid.UUID) -> dict[str, int]:
        """Return graph totals and per-kind breakdowns for *user_id*.

        Keys are :data:`STATS_TOTAL_ENTITIES` and :data:`STATS_TOTAL_EDGES` for the totals,
        then one ``entity_<kind>`` entry per :class:`~app.models.enums.EntityKind` actually
        present and one ``relation_<relation>`` entry per
        :class:`~app.models.enums.RelationKind` actually present. Absent kinds are omitted
        rather than reported as zero, so the payload stays proportional to what the user
        actually has; a client should treat a missing key as ``0``.

        Args:
            user_id: Owning user.

        Returns:
            A flat ``dict[str, int]``, directly usable as ``GraphView.stats``.
        """
        counts: dict[str, int] = {STATS_TOTAL_ENTITIES: 0, STATS_TOTAL_EDGES: 0}

        entity_rows = await self.session.execute(
            select(KnowledgeEntity.kind, func.count(KnowledgeEntity.id))
            .where(KnowledgeEntity.user_id == user_id)
            .group_by(KnowledgeEntity.kind)
        )
        for kind, count in entity_rows.all():
            resolved = EntityKind(kind)
            counts[f"{STATS_ENTITY_KIND_PREFIX}{resolved.value}"] = int(count)
            counts[STATS_TOTAL_ENTITIES] += int(count)

        edge_rows = await self.session.execute(
            select(KnowledgeEdge.relation, func.count(KnowledgeEdge.id))
            .where(KnowledgeEdge.user_id == user_id)
            .group_by(KnowledgeEdge.relation)
        )
        for relation, count in edge_rows.all():
            resolved_relation = RelationKind(relation)
            counts[f"{STATS_RELATION_PREFIX}{resolved_relation.value}"] = int(count)
            counts[STATS_TOTAL_EDGES] += int(count)

        return counts

    async def prune(self, user_id: uuid.UUID, *, min_confidence: float) -> int:
        """Delete the user's entities whose confidence is below *min_confidence*.

        The cleanup for a noisy source: an analyzer that emitted a hundred speculative nodes
        leaves them all at low confidence, and repeated corroboration is what raises them
        (:meth:`~KnowledgeStore.reinforce_confidence`). Anything never corroborated can go.

        Deleting an entity cascades to its edges at the database level. Facts anchored to it
        are **not** deleted — ``knowledge_facts.entity_id`` is ``ON DELETE SET NULL``
        precisely so that pruning the graph never destroys evidence.

        Args:
            user_id: Owning user.
            min_confidence: Exclusive floor. Entities with ``confidence <
                min_confidence`` are removed; pass ``0.0`` to delete nothing.

        Returns:
            The number of entities deleted.
        """
        result = await self.session.execute(
            delete(KnowledgeEntity)
            .where(
                KnowledgeEntity.user_id == user_id,
                KnowledgeEntity.confidence < min_confidence,
            )
            .execution_options(synchronize_session="fetch")
        )
        await self.session.flush()
        removed = int(result.rowcount or 0)
        if removed:
            logger.info(
                "graph.pruned",
                user_id=str(user_id),
                entities=removed,
                min_confidence=min_confidence,
            )
        return removed
