"""Application service for the knowledge engine — ``docs/CONTRACTS.md`` §13.

:class:`KnowledgeService` is the one seam between the HTTP layer and
:mod:`app.knowledge`. Everything the ``/api/v1/knowledge/*`` route group needs lives here,
and the routes stay thin: parse, delegate, serialise.

Three rules shape the whole module.

**Schemas out, never ORM rows.** Every method returns a model from
:mod:`app.schemas.knowledge`. SQLAlchemy instances are bound to a session, carry lazily
loaded relationships that raise ``MissingGreenlet`` when touched after the request, and
expose columns (embeddings) that must never be serialised. Converting at this boundary is
what keeps all three problems inside the service.

**Unknown ids raise, they do not return ``None``.** A missing source, fact or user id
raises :class:`LookupError` (which :class:`~app.knowledge.indexer.SourceNotFoundError`
already is), and malformed input raises :class:`ValueError` (which
:class:`~app.knowledge.indexer.UnsupportedSourceError` already is). ``app.api.errors`` maps
the first to ``404`` and the second to ``400``, so no route has to invent that mapping.

**Collaborators are built lazily.** A request that only lists sources should not construct
an indexer, resolve an embedder, or open a vector store. :attr:`~KnowledgeService.indexer`
and :attr:`~KnowledgeService.retriever` are created on first touch and reused after that;
the three stores are borrowed from the retriever so a request that both searches and reads
facts shares one set.

The service works with zero API keys: without an embedding provider the stores fall back to
the deterministic hashing embedder and SQL keyword matching, so search degrades in quality
but never fails.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

import structlog
from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import JSON

from app.config.settings import Settings, get_settings
from app.knowledge.facts import FACT_COLLECTION, FactStore
from app.knowledge.graph import DEFAULT_SUBGRAPH_LIMIT, KnowledgeGraph
from app.knowledge.indexer import IndexReport, KnowledgeIndexer
from app.knowledge.memory import MemoryStore
from app.knowledge.retrieval import (
    ITEM_CHUNK,
    ITEM_ENTITY,
    ITEM_FACT,
    ITEM_MEMORY,
    KnowledgeRetriever,
    RetrievalResult,
)
from app.knowledge.vector.base import VectorRecord
from app.models.enums import EntityKind, FactKind, IndexStatus, SourceKind
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeSource,
    MemoryEntry,
)
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.knowledge import (
    EntityRead,
    FactRead,
    FactUpdate,
    GraphView,
    IndexReportRead,
    KnowledgeSearchResult,
    KnowledgeStats,
    SourceCreate,
    SourceRead,
)

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "FACT_FILTER_KEYS",
    "MAX_SEARCH_LIMIT",
    "SEARCH_KINDS",
    "KnowledgeService",
]

logger = structlog.get_logger(__name__)

#: Hits returned by :meth:`KnowledgeService.search` when the caller does not ask for a
#: size. Deliberately larger than a screenful: the desktop search panel interleaves four
#: stores, so a page of ten would often show only facts.
DEFAULT_SEARCH_LIMIT: Final[int] = 25

#: Hard ceiling on a single search. Retrieval overfetches by a constant factor before
#: fusing, so an unbounded ``limit`` would turn one keystroke into a full-table scan.
MAX_SEARCH_LIMIT: Final[int] = 100

#: The four stores a search hit can come from. Mirrors
#: :data:`app.schemas.knowledge.KnowledgeSearchKind`, whose ``Literal`` cannot be iterated
#: at runtime without ``typing.get_args``.
SEARCH_KINDS: Final[frozenset[str]] = frozenset({ITEM_FACT, ITEM_CHUNK, ITEM_ENTITY, ITEM_MEMORY})

#: Every key :meth:`KnowledgeService.facts` understands. An unrecognised key raises rather
#: than being ignored: a typo in a query string that silently returns unfiltered rows is
#: worse than an error, because the caller believes the filter was applied.
FACT_FILTER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "kinds",
        "q",
        "query",
        "active",
        "is_active",
        "verified",
        "user_verified",
        "organization",
        "role",
        "skill",
        "min_impact",
        "min_confidence",
        "source_document_id",
        "entity_id",
        "limit",
        "offset",
    }
)

#: SQLAlchemy's ``JSON`` type writes a Python ``None`` as the JSON literal ``null``, not as
#: SQL ``NULL``. Any "is this embedded?" predicate has to exclude it explicitly.
_JSON_NULL_TEXT: Final[str] = "null"

#: Characters ``LIKE`` treats as wildcards, escaped before a user's text reaches a pattern.
_LIKE_ESCAPE: Final[str] = "\\"
_LIKE_SPECIALS: Final[tuple[str, ...]] = (_LIKE_ESCAPE, "%", "_")

#: Overfetch factor applied to the retriever's per-store ``k`` before fusion, so that a
#: kind filter applied afterwards still has enough survivors to fill ``limit``.
_SEARCH_OVERFETCH: Final[int] = 2


class KnowledgeService:
    """Reads and writes the user's knowledge base on behalf of the API.

    Bound to one session for its lifetime, which for an HTTP request is that request. It is
    not safe to share across concurrent tasks — an
    :class:`~sqlalchemy.ext.asyncio.AsyncSession` is not — so build one per request through
    the FastAPI dependency rather than caching one globally.

    Transaction ownership is split, and the split is worth stating because it is not
    uniform: :meth:`remove_source`, :meth:`reindex` and :meth:`reindex_all` delegate to
    :class:`~app.knowledge.indexer.KnowledgeIndexer`, which commits internally (a crawl is
    far too long to hold a write transaction across). :meth:`add_source` and
    :meth:`update_fact` commit their own edits. Every other method is read-only and commits
    nothing.

    Attributes:
        session: The session every query runs on.
        settings: Chunk sizes, refresh intervals and scan limits, passed to the indexer.
    """

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        """Bind the service to a session.

        Args:
            session: The async session for this unit of work.
            settings: Application settings. Resolved from
                :func:`~app.config.settings.get_settings` when omitted, so a caller that
                has no opinion does not have to fetch them first.
        """
        self.session = session
        self.settings = settings if settings is not None else get_settings()
        self._indexer: KnowledgeIndexer | None = None
        self._retriever: KnowledgeRetriever | None = None

    # ----------------------------------------------------------------------------------
    # Collaborators
    # ----------------------------------------------------------------------------------

    @property
    def indexer(self) -> KnowledgeIndexer:
        """The indexing pipeline, built on first use.

        Constructing one resolves an embedder, a cache and a vector store, none of which a
        read-only request needs.
        """
        if self._indexer is None:
            self._indexer = KnowledgeIndexer(self.session, self.settings)
        return self._indexer

    @property
    def retriever(self) -> KnowledgeRetriever:
        """The hybrid retriever, built on first use.

        Also the owner of the three stores below: borrowing them from here means a request
        that searches and then reads facts shares one embedder and one vector-store
        connection instead of opening a second set.
        """
        if self._retriever is None:
            self._retriever = KnowledgeRetriever(self.session)
        return self._retriever

    @property
    def fact_store(self) -> FactStore:
        """The fact store backing :meth:`facts` and :meth:`update_fact`."""
        return self.retriever.facts

    @property
    def graph_store(self) -> KnowledgeGraph:
        """The graph store backing :meth:`graph` and :meth:`entities`.

        Named ``graph_store`` rather than ``graph`` because :meth:`graph` is the API-facing
        method that returns a :class:`~app.schemas.knowledge.GraphView`.
        """
        return self.retriever.graph

    @property
    def memory_store(self) -> MemoryStore:
        """The memory store counted by :meth:`stats`."""
        return self.retriever.memory

    # ----------------------------------------------------------------------------------
    # Sources
    # ----------------------------------------------------------------------------------

    async def add_source(self, user_id: uuid.UUID, payload: SourceCreate) -> SourceRead:
        """Register a source for indexing, or fold the request into the existing row.

        Identity is ``(user_id, kind, uri)``, so pointing the app at the same repository
        twice updates one row rather than indexing the same content into the graph twice.

        The indexer validates the kind against the registered analyzers before writing
        anything, so a source nothing can read is rejected here — where the user can act on
        the message — rather than becoming a row that fails on every pass forever. It always
        writes ``enabled=True, auto_refresh=True``; this method then applies the payload's
        values, which is the only place those two flags can be set at creation time.

        Args:
            user_id: Owning user.
            payload: What to register.

        Returns:
            The stored source, committed.

        Raises:
            ValueError: If no registered analyzer supports the requested kind. Raised as
                :class:`~app.knowledge.indexer.UnsupportedSourceError`, whose message lists
                the kinds that are supported.
        """
        ref = _payload_to_source_ref(payload)
        source = await self.indexer.add_source(user_id, ref)

        # `add_source` hard-codes both flags; honour the payload's intent instead.
        changed = False
        if source.enabled != payload.enabled:
            source.enabled = payload.enabled
            changed = True
        if source.auto_refresh != payload.auto_refresh:
            source.auto_refresh = payload.auto_refresh
            changed = True
        if changed:
            await self.session.commit()

        logger.info(
            "knowledge_service.source_added",
            user_id=str(user_id),
            source_id=str(source.id),
            kind=source.kind.value,
        )
        return SourceRead.model_validate(source)

    async def list_sources(self, user_id: uuid.UUID) -> list[SourceRead]:
        """Return every source the user has registered, newest first.

        Unpaginated by design: sources are hand-registered, so a user has tens of them, not
        thousands. A route that must return a :class:`~app.schemas.common.Page` can wrap the
        result with :func:`~app.schemas.common.paginate`.

        Args:
            user_id: Owning user.

        Returns:
            The sources, ordered by creation time descending.
        """
        statement = (
            select(KnowledgeSource)
            .where(KnowledgeSource.user_id == user_id)
            .order_by(KnowledgeSource.created_at.desc(), KnowledgeSource.id.desc())
        )
        rows = (await self.session.execute(statement)).scalars().all()
        return [SourceRead.model_validate(row) for row in rows]

    async def remove_source(self, source_id: uuid.UUID) -> None:
        """Unregister a source and delete its documents, chunks and vectors.

        **Knowledge outlives its source.** Facts and graph nodes learned from it stay in the
        database — a verified accomplishment does not stop being true because the user
        unregistered the repository it was first read from — but facts sourced from it are
        deactivated so they stop being cited by generated resumes while remaining a full
        audit trail.

        Args:
            source_id: The source to remove.

        Raises:
            LookupError: If no such source exists. The indexer's own ``remove_source`` is
                idempotent, which is right for a worker retrying a delete but wrong for an
                HTTP ``DELETE`` that must answer ``404``.
        """
        source = await self.session.get(KnowledgeSource, source_id)
        if source is None:
            raise LookupError(f"knowledge source {source_id} not found")
        await self.indexer.remove_source(source_id)
        logger.info("knowledge_service.source_removed", source_id=str(source_id))

    # ----------------------------------------------------------------------------------
    # Indexing
    # ----------------------------------------------------------------------------------

    async def reindex(self, source_id: uuid.UUID, force: bool = False) -> IndexReportRead:
        """Run one source through the indexing pipeline.

        A pass whose fingerprint has not moved is skipped — ``skipped=True``, no analysis,
        no embedding calls — which is what makes indexing on every launch affordable.

        Analyzer and database failures do not raise: the source is marked
        :attr:`~app.models.enums.IndexStatus.FAILED` and the report carries the error, so
        one unreachable repository never costs a batch its other results. Inspect
        ``errors`` on the returned report.

        Args:
            source_id: The source to index.
            force: Re-analyze, re-chunk and re-embed even when nothing changed, and bypass
                the short-lived fingerprint probe memo.

        Returns:
            What the pass did.

        Raises:
            LookupError: If no such source exists (as
                :class:`~app.knowledge.indexer.SourceNotFoundError`).
        """
        report = await self.indexer.index_source(source_id, force=force)
        return _report_to_schema(report)

    async def reindex_all(self, user_id: uuid.UUID, force: bool = False) -> list[IndexReportRead]:
        """Index every enabled source the user has, one report per source.

        The indexer gives each source its own session and commits independently, so a
        failure in one is isolated to that source's report rather than aborting the batch.

        Args:
            user_id: Owning user.
            force: Re-index even sources whose content has not changed.

        Returns:
            One report per source attempted; empty when the user has no enabled sources.
        """
        reports = await self.indexer.index_all(user_id, force=force)
        logger.info(
            "knowledge_service.reindex_all",
            user_id=str(user_id),
            sources=len(reports),
            force=force,
        )
        return [_report_to_schema(report) for report in reports]

    # ----------------------------------------------------------------------------------
    # Search and graph
    # ----------------------------------------------------------------------------------

    async def search(
        self,
        user_id: uuid.UUID,
        q: str,
        kinds: Sequence[str] | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[KnowledgeSearchResult]:
        """Search everything the user knows, fused into one ranked list.

        Delegates to :meth:`~app.knowledge.retrieval.KnowledgeRetriever.retrieve`, which
        combines vector similarity, keyword matching and one-hop graph expansion by
        reciprocal-rank fusion across facts, document chunks, entities and memories.

        ``kinds`` accepts two vocabularies at once, because both are useful and neither
        collides with the other:

        * a :data:`~app.schemas.knowledge.KnowledgeSearchKind` (``fact``, ``chunk``,
          ``entity``, ``memory``) restricts which **stores** appear in the results;
        * a :class:`~app.models.enums.FactKind` (``accomplishment``, ``skill_usage``, …)
          restricts which **facts** are considered.

        So ``kinds=["fact", "accomplishment"]`` means "accomplishments only", and
        ``kinds=["chunk"]`` means "document text only".

        Args:
            user_id: Owning user.
            q: The query. Blank returns no results rather than every row.
            kinds: Store kinds and/or fact kinds to restrict to. ``None`` searches
                everything.
            limit: Maximum hits, clamped to ``[1, MAX_SEARCH_LIMIT]``.

        Returns:
            Hits ordered by fused score, highest first.

        Raises:
            ValueError: If an entry in *kinds* is neither a search kind nor a fact kind.
        """
        query = (q or "").strip()
        if not query:
            return []

        store_kinds, fact_kinds = _split_search_kinds(kinds)
        size = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        depth = size * _SEARCH_OVERFETCH

        result = await self.retriever.retrieve(
            user_id,
            query,
            k_facts=depth,
            k_chunks=depth,
            kinds=fact_kinds or None,
            expand_graph=ITEM_ENTITY in store_kinds,
        )

        hits: list[KnowledgeSearchResult] = []
        if ITEM_FACT in store_kinds:
            hits.extend(_fact_hits(result))
        if ITEM_CHUNK in store_kinds:
            hits.extend(_chunk_hits(result))
        if ITEM_ENTITY in store_kinds:
            hits.extend(_entity_hits(result))
        if ITEM_MEMORY in store_kinds:
            hits.extend(_memory_hits(result))

        hits.sort(key=lambda hit: hit.score, reverse=True)
        logger.debug(
            "knowledge_service.search",
            user_id=str(user_id),
            returned=min(len(hits), size),
            candidates=len(hits),
        )
        return hits[:size]

    async def graph(
        self,
        user_id: uuid.UUID,
        kinds: Sequence[str | EntityKind] | None = None,
        limit: int = DEFAULT_SUBGRAPH_LIMIT,
    ) -> GraphView:
        """Return a renderable slice of the user's knowledge graph.

        The view is capped at *limit* nodes, chosen by prominence, and its ``stats`` describe
        the **whole** graph rather than the returned slice — so the desktop canvas can say
        "showing 500 of 3,214 nodes" honestly instead of implying it drew everything.

        Args:
            user_id: Owning user.
            kinds: Entity kinds to include; ``None`` includes every kind.
            limit: Maximum nodes, clamped by the graph store's own ceiling.

        Returns:
            Nodes, edges between the returned nodes, and whole-graph statistics.

        Raises:
            ValueError: If an entry in *kinds* is not a valid
                :class:`~app.models.enums.EntityKind`.
        """
        resolved = _coerce_enums(kinds, EntityKind, "entity kind")
        return await self.graph_store.subgraph(user_id, kinds=resolved or None, limit=int(limit))

    async def entities(
        self,
        user_id: uuid.UUID,
        kind: str | EntityKind | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[EntityRead]:
        """Return the user's graph entities, most-mentioned first.

        Ordering by ``mention_count`` puts the technologies and employers that recur across
        many sources at the top, which is what a reviewer wants to see first.

        Args:
            user_id: Owning user.
            kind: Restrict to one entity kind; ``None`` returns every kind.
            limit: Maximum rows. ``None`` returns all of them, which is what a route that
                paginates in memory wants.
            offset: Rows to skip, for SQL-level paging.

        Returns:
            The matching entities.

        Raises:
            ValueError: If *kind* is not a valid :class:`~app.models.enums.EntityKind`.
        """
        statement = select(KnowledgeEntity).where(KnowledgeEntity.user_id == user_id)
        if kind is not None:
            statement = statement.where(
                KnowledgeEntity.kind == _coerce_enum(kind, EntityKind, "entity kind")
            )
        statement = statement.order_by(
            KnowledgeEntity.mention_count.desc(),
            KnowledgeEntity.confidence.desc(),
            KnowledgeEntity.normalized_name.asc(),
        )
        if offset:
            statement = statement.offset(max(0, int(offset)))
        if limit is not None:
            statement = statement.limit(max(0, int(limit)))

        rows = (await self.session.execute(statement)).scalars().all()
        return [EntityRead.model_validate(row) for row in rows]

    # ----------------------------------------------------------------------------------
    # Facts
    # ----------------------------------------------------------------------------------

    async def facts(
        self, user_id: uuid.UUID, filters: Mapping[str, Any] | None = None
    ) -> Page[FactRead]:
        """Return a filtered, paginated page of the user's facts.

        This is the fact **table**, not fact search: matching is exact or substring, and the
        page carries a true unpaginated ``total`` so the client can render "1–50 of 812".
        Semantic search lives in :meth:`search`, which cannot report a meaningful total
        because rank fusion has no row count.

        Recognised keys (:data:`FACT_FILTER_KEYS`):

        ==========================  ===================================================
        Key                         Meaning
        ==========================  ===================================================
        ``kind`` / ``kinds``        One or more :class:`~app.models.enums.FactKind`.
        ``q`` / ``query``           Case-insensitive substring of the text, organization
                                    or role.
        ``active`` / ``is_active``  Filter on the retirement flag.
        ``verified`` /
        ``user_verified``           Filter on human confirmation.
        ``organization``            Case-insensitive exact match.
        ``role``                    Case-insensitive exact match.
        ``skill``                   Coarse containment against the ``skills`` and
                                    ``technologies`` JSON arrays.
        ``min_impact``              Minimum ``impact_score`` (0–100), inclusive.
        ``min_confidence``          Minimum ``confidence`` (0.0–1.0), inclusive.
        ``source_document_id``      Facts extracted from one document.
        ``entity_id``               Facts anchored to one graph entity.
        ``limit`` / ``offset``      Paging; ``limit`` is capped at ``MAX_PAGE_LIMIT``.
        ==========================  ===================================================

        Args:
            user_id: Owning user.
            filters: The filter mapping, typically a route's query parameters. ``None`` is
                the same as ``{}``.

        Returns:
            The page, ordered by impact then confidence then age.

        Raises:
            ValueError: If a key is not recognised or a value cannot be coerced. Unknown
                keys are rejected rather than ignored: a mistyped filter that silently
                returns unfiltered rows misleads the caller.
        """
        supplied = dict(filters or {})
        unknown = sorted(set(supplied) - FACT_FILTER_KEYS)
        if unknown:
            allowed = ", ".join(sorted(FACT_FILTER_KEYS))
            raise ValueError(
                f"unknown fact filter(s): {', '.join(unknown)}. Allowed keys: {allowed}"
            )

        conditions = self._fact_conditions(user_id, supplied)
        limit, offset = _paging(supplied)

        total = int(
            (
                await self.session.execute(select(func.count(KnowledgeFact.id)).where(*conditions))
            ).scalar_one()
            or 0
        )

        statement = (
            select(KnowledgeFact)
            .where(*conditions)
            .order_by(
                KnowledgeFact.impact_score.desc(),
                KnowledgeFact.confidence.desc(),
                KnowledgeFact.created_at.asc(),
                KnowledgeFact.id.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).scalars().all()
        return Page.of(
            [FactRead.model_validate(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def _fact_conditions(
        self, user_id: uuid.UUID, supplied: Mapping[str, Any]
    ) -> list[ColumnElement[bool]]:
        """Translate a validated filter mapping into SQL predicates.

        Args:
            user_id: Owning user; always the first predicate.
            supplied: The filter mapping, already checked for unknown keys.

        Returns:
            The predicates, to be spread into ``where(*conditions)``.

        Raises:
            ValueError: If a value cannot be coerced to the type its column needs.
        """
        conditions: list[ColumnElement[bool]] = [KnowledgeFact.user_id == user_id]

        kinds = _coerce_enums(_first(supplied, "kinds", "kind"), FactKind, "fact kind")
        if kinds:
            conditions.append(KnowledgeFact.kind.in_(kinds))

        active = _first(supplied, "active", "is_active")
        if active is not None:
            conditions.append(KnowledgeFact.is_active.is_(_coerce_bool(active, "active")))

        verified = _first(supplied, "verified", "user_verified")
        if verified is not None:
            conditions.append(KnowledgeFact.user_verified.is_(_coerce_bool(verified, "verified")))

        text = _clean_str(_first(supplied, "q", "query"))
        if text:
            pattern = _like_pattern(text)
            conditions.append(
                func.lower(KnowledgeFact.text).like(pattern, escape=_LIKE_ESCAPE)
                | func.lower(func.coalesce(KnowledgeFact.organization, "")).like(
                    pattern, escape=_LIKE_ESCAPE
                )
                | func.lower(func.coalesce(KnowledgeFact.role, "")).like(
                    pattern, escape=_LIKE_ESCAPE
                )
            )

        organization = _clean_str(supplied.get("organization"))
        if organization:
            conditions.append(func.lower(KnowledgeFact.organization) == organization.lower())

        role = _clean_str(supplied.get("role"))
        if role:
            conditions.append(func.lower(KnowledgeFact.role) == role.lower())

        skill = _clean_str(supplied.get("skill"))
        if skill:
            # `skills` and `technologies` are JSON arrays of strings. Matching the quoted
            # term against the serialised array is coarse but portable across the JSON and
            # JSONB backends, and precise enough for a table filter because the quotes
            # anchor both ends of the element.
            needle = f'%"{_escape_like(skill.lower())}"%'
            conditions.append(
                func.lower(cast(KnowledgeFact.skills, String)).like(needle, escape=_LIKE_ESCAPE)
                | func.lower(cast(KnowledgeFact.technologies, String)).like(
                    needle, escape=_LIKE_ESCAPE
                )
            )

        min_impact = supplied.get("min_impact")
        if min_impact is not None:
            conditions.append(KnowledgeFact.impact_score >= _coerce_int(min_impact, "min_impact"))

        min_confidence = supplied.get("min_confidence")
        if min_confidence is not None:
            conditions.append(
                KnowledgeFact.confidence >= _coerce_float(min_confidence, "min_confidence")
            )

        document_id = supplied.get("source_document_id")
        if document_id is not None:
            conditions.append(
                KnowledgeFact.source_document_id == _coerce_uuid(document_id, "source_document_id")
            )

        entity_id = supplied.get("entity_id")
        if entity_id is not None:
            conditions.append(KnowledgeFact.entity_id == _coerce_uuid(entity_id, "entity_id"))

        return conditions

    async def update_fact(self, fact_id: uuid.UUID, payload: FactUpdate) -> FactRead:
        """Apply a user's correction to one fact, keeping every derived value consistent.

        This is the human-in-the-loop path, and four things have to move together or the
        engine quietly breaks:

        1. ``normalized_text`` and ``content_hash`` are derived from the text *and* the
           organization, role and dates. Change any of them without recomputing and
           deduplication stops matching this row forever.
        2. Rewriting the text invalidates the embedding, so it is re-embedded and its vector
           record replaced — done by
           :meth:`~app.knowledge.facts.FactStore.update_text`.
        3. Retiring a fact must also remove its vector, or semantic search keeps surfacing
           something the user deleted. Un-retiring must put it back.
        4. Verification freezes the fact's identifying fields against later indexing runs,
           which is why it goes through
           :meth:`~app.knowledge.facts.FactStore.verify` rather than a bare assignment.

        Fields absent from *payload* are left alone (``exclude_unset``), so a client can
        ``PATCH`` one field without echoing the row back.

        Args:
            fact_id: The fact to edit.
            payload: The partial update.

        Returns:
            The updated fact, committed.

        Raises:
            LookupError: If no such fact exists.
            ValueError: If ``text`` is present but blank — a fact with no claim is not a
                fact, and its hash would collide with every other blank one.
        """
        row = await self.session.get(KnowledgeFact, fact_id)
        if row is None:
            raise LookupError(f"knowledge fact {fact_id} not found")

        data = payload.model_dump(exclude_unset=True)
        if not data:
            return FactRead.model_validate(row)

        # Identity fields first: `update_text` recomputes the content hash from them, so
        # they have to be in place before it runs.
        identity_changed = False
        for field in ("organization", "role", "date_start", "date_end"):
            if field in data:
                setattr(row, field, _clean_str(data[field]))
                identity_changed = True
        if "location" in data:
            row.location = _clean_str(data["location"])
        if "kind" in data and data["kind"] is not None:
            row.kind = FactKind(data["kind"])
        for field in ("skills", "technologies", "metrics"):
            if field in data and data[field] is not None:
                setattr(row, field, _clean_list(data[field]))
        if "impact_score" in data and data["impact_score"] is not None:
            row.impact_score = int(data["impact_score"])
        if "confidence" in data and data["confidence"] is not None:
            row.confidence = float(data["confidence"])

        text = data.get("text")
        if text is not None:
            cleaned = str(text).strip()
            if not cleaned:
                raise ValueError("fact text must not be blank")
            if cleaned != row.text or identity_changed:
                # Rewrites text, refreshes the derived fields, re-embeds, and replaces the
                # vector record — all four in one place, which is why it is not inlined.
                await self.fact_store.update_text(fact_id, cleaned)
        elif identity_changed:
            row.refresh_derived_fields()

        if "user_verified" in data and data["user_verified"] is not None:
            await self.fact_store.verify(fact_id, bool(data["user_verified"]))

        if "is_active" in data and data["is_active"] is not None:
            await self._set_active(row, bool(data["is_active"]))

        await self.session.flush()
        await self.session.commit()
        logger.info(
            "knowledge_service.fact_updated",
            fact_id=str(fact_id),
            fields=sorted(data),
        )
        return FactRead.model_validate(row)

    async def _set_active(self, row: KnowledgeFact, active: bool) -> None:
        """Move a fact in or out of the retrieval pool, vector record included.

        Retiring goes through :meth:`~app.knowledge.facts.FactStore.deactivate`, which
        deletes the vector so semantic search cannot resurrect the fact. Un-retiring has no
        counterpart in the store, so the record is rebuilt here from the row's own
        embedding; a fact that never had one is simply left for the embedding backlog.

        Args:
            row: The fact, already attached to the session.
            active: The desired state.
        """
        if bool(row.is_active) == active:
            return
        if not active:
            await self.fact_store.deactivate(row.id)
            return

        row.is_active = True
        await self.session.flush()
        embedding = list(row.embedding or [])
        if embedding:
            await self.fact_store.vector_upsert(
                FACT_COLLECTION,
                [
                    VectorRecord(
                        id=str(row.id),
                        embedding=embedding,
                        text=row.text,
                        metadata={
                            "user_id": str(row.user_id),
                            "kind": FactKind(row.kind).value,
                            "impact_score": int(row.impact_score or 0),
                            "content_hash": row.content_hash,
                        },
                    )
                ],
            )
        logger.info("knowledge_service.fact_reactivated", fact_id=str(row.id))

    # ----------------------------------------------------------------------------------
    # Statistics
    # ----------------------------------------------------------------------------------

    async def stats(self, user_id: uuid.UUID) -> KnowledgeStats:
        """Return the aggregate counts behind ``GET /knowledge/stats``.

        Counted directly rather than by summing the individual stores' ``stats()`` helpers,
        because those return flat prefixed keys (``entity_skill``, ``fact_accomplishment``)
        that would have to be string-parsed back apart to fill this schema — a coupling that
        breaks silently the day a prefix changes.

        ``chunks_embedded`` is the honest measure of the embedding backlog, and it is not
        ``embedding IS NOT NULL``: SQLAlchemy's ``JSON`` type persists a Python ``None`` as
        the JSON literal ``null``, so on the SQLite install every unembedded chunk has a
        non-``NULL`` column. The predicate excludes that literal explicitly.

        ``by_source_kind`` counts **documents** per kind, matching the
        ``applicantos_knowledge_documents_total{kind}`` metric, so the dashboard and the
        metric never disagree.

        Args:
            user_id: Owning user.

        Returns:
            The populated statistics.
        """
        stats = KnowledgeStats()

        source_rows = (
            await self.session.execute(
                select(
                    KnowledgeSource.index_status,
                    KnowledgeSource.enabled,
                    func.count(KnowledgeSource.id),
                )
                .where(KnowledgeSource.user_id == user_id)
                .group_by(KnowledgeSource.index_status, KnowledgeSource.enabled)
            )
        ).all()
        for index_status, enabled, count in source_rows:
            count = int(count)
            stats.sources += count
            if enabled:
                stats.sources_enabled += count
            if IndexStatus(index_status) is IndexStatus.FAILED:
                stats.sources_failed += count

        stats.last_indexed_at = (
            await self.session.execute(
                select(func.max(KnowledgeSource.last_indexed_at)).where(
                    KnowledgeSource.user_id == user_id
                )
            )
        ).scalar_one_or_none()

        document_rows = (
            await self.session.execute(
                select(
                    KnowledgeDocument.kind,
                    func.count(KnowledgeDocument.id),
                    func.coalesce(func.sum(KnowledgeDocument.token_count), 0),
                )
                .where(KnowledgeDocument.user_id == user_id)
                .group_by(KnowledgeDocument.kind)
            )
        ).all()
        for kind, count, tokens in document_rows:
            resolved = SourceKind(kind).value
            stats.by_source_kind[resolved] = int(count)
            stats.documents += int(count)
            stats.tokens_indexed += int(tokens or 0)

        chunk_scope = KnowledgeChunk.document_id.in_(
            select(KnowledgeDocument.id).where(KnowledgeDocument.user_id == user_id)
        )
        stats.chunks = await self._count(select(func.count(KnowledgeChunk.id)), chunk_scope)
        stats.chunks_embedded = await self._count(
            select(func.count(KnowledgeChunk.id)),
            chunk_scope,
            self._embedding_present(KnowledgeChunk.embedding),
        )

        entity_rows = (
            await self.session.execute(
                select(KnowledgeEntity.kind, func.count(KnowledgeEntity.id))
                .where(KnowledgeEntity.user_id == user_id)
                .group_by(KnowledgeEntity.kind)
            )
        ).all()
        for kind, count in entity_rows:
            stats.by_entity_kind[EntityKind(kind).value] = int(count)
            stats.entities += int(count)

        graph_counts = await self.graph_store.stats(user_id)
        stats.edges = int(graph_counts.get("edges", 0))

        fact_rows = (
            await self.session.execute(
                select(KnowledgeFact.kind, func.count(KnowledgeFact.id))
                .where(KnowledgeFact.user_id == user_id)
                .group_by(KnowledgeFact.kind)
            )
        ).all()
        for kind, count in fact_rows:
            stats.by_fact_kind[FactKind(kind).value] = int(count)
            stats.facts += int(count)

        stats.facts_active = await self._count(
            select(func.count(KnowledgeFact.id)),
            KnowledgeFact.user_id == user_id,
            KnowledgeFact.is_active.is_(True),
        )
        stats.facts_verified = await self._count(
            select(func.count(KnowledgeFact.id)),
            KnowledgeFact.user_id == user_id,
            KnowledgeFact.user_verified.is_(True),
        )
        stats.memories = await self._count(
            select(func.count(MemoryEntry.id)), MemoryEntry.user_id == user_id
        )
        return stats

    async def _count(self, statement: Any, *conditions: ColumnElement[bool]) -> int:
        """Execute a ``COUNT`` and return it as a plain int.

        Args:
            statement: A ``select(func.count(...))`` with no ``WHERE`` yet.
            *conditions: Predicates to apply.

        Returns:
            The count, ``0`` when the aggregate came back ``NULL``.
        """
        result = await self.session.execute(statement.where(*conditions))
        return int(result.scalar_one() or 0)

    def _embedding_present(self, column: Any) -> ColumnElement[bool]:
        """Return the predicate matching rows that really do carry a vector.

        :class:`~app.database.types.EmbeddingType` becomes a pgvector ``vector`` column on
        PostgreSQL when the extension package is importable and a plain ``JSON`` column
        everywhere else — including on PostgreSQL *without* pgvector. The two disagree about
        ``None``, so the predicate asks the bound dialect which one it is talking to rather
        than guessing from the dialect's name.

        Args:
            column: The embedding column to test.

        Returns:
            A predicate true for rows with a usable embedding.
        """
        dialect = self.session.get_bind().dialect
        implementation = column.type.load_dialect_impl(dialect)
        if isinstance(implementation, JSON):
            return and_(
                column.is_not(None),
                cast(column, String) != _JSON_NULL_TEXT,
            )
        return column.is_not(None)


# ======================================================================================
# Module-private helpers
# ======================================================================================


def _payload_to_source_ref(payload: SourceCreate) -> Any:
    """Convert a :class:`~app.schemas.knowledge.SourceCreate` into an analyzer input.

    Imported lazily so that the schema layer never pulls the analyzer plugins in as a side
    effect of being imported.

    Args:
        payload: The API-supplied registration.

    Returns:
        The equivalent :class:`~app.knowledge.analyzers.base.SourceRef`.
    """
    from app.knowledge.analyzers.base import SourceRef

    return SourceRef(
        kind=payload.kind,
        uri=payload.uri,
        label=payload.label,
        config=dict(payload.config or {}),
    )


def _report_to_schema(report: IndexReport) -> IndexReportRead:
    """Convert an indexer report into its API shape.

    Args:
        report: The dataclass the indexer returned.

    Returns:
        The serialisable read model. ``IndexReport.failed`` has no counterpart in the
        schema — a failed pass is already legible from a non-empty ``errors``.
    """
    return IndexReportRead(
        source_id=report.source_id,
        documents=report.documents,
        chunks=report.chunks,
        facts=report.facts,
        entities=report.entities,
        edges=report.edges,
        skipped=report.skipped,
        duration_seconds=report.duration_seconds,
        errors=list(report.errors),
    )


def _split_search_kinds(
    kinds: Sequence[str] | None,
) -> tuple[frozenset[str], list[FactKind]]:
    """Split a mixed ``kinds`` argument into store kinds and fact kinds.

    Args:
        kinds: The caller's filter, or ``None`` for "everything".

    Returns:
        ``(store_kinds, fact_kinds)``. ``store_kinds`` is every searchable store when the
        caller named none, so ``kinds=["accomplishment"]`` narrows the facts without
        accidentally hiding chunks, entities and memories.

    Raises:
        ValueError: If an entry is neither a search kind nor a fact kind.
    """
    if not kinds:
        return SEARCH_KINDS, []

    stores: set[str] = set()
    fact_kinds: list[FactKind] = []
    for raw in kinds:
        value = str(getattr(raw, "value", raw)).strip().lower()
        if not value:
            continue
        if value in SEARCH_KINDS:
            stores.add(value)
            continue
        try:
            resolved = FactKind(value)
        except ValueError as exc:
            allowed = ", ".join(sorted(SEARCH_KINDS))
            fact_options = ", ".join(sorted(member.value for member in FactKind))
            raise ValueError(
                f"unknown search kind {value!r}. Expected one of {allowed} "
                f"or a fact kind ({fact_options})."
            ) from exc
        if resolved not in fact_kinds:
            fact_kinds.append(resolved)

    if not stores:
        # Only fact kinds were named, which is a filter on facts rather than a request to
        # search facts alone. Keep every store; the fact kinds narrow one of them.
        stores = set(SEARCH_KINDS)
    if fact_kinds:
        # A fact-kind filter is meaningless unless facts are actually being returned.
        stores.add(ITEM_FACT)
    return frozenset(stores), fact_kinds


def _fact_hits(result: RetrievalResult) -> list[KnowledgeSearchResult]:
    """Render the retrieved facts as search results.

    Args:
        result: The retrieval to read.

    Returns:
        One hit per fact, carrying its fused score and the reasons it was retrieved.
    """
    hits: list[KnowledgeSearchResult] = []
    for fact in result.facts:
        hits.append(
            KnowledgeSearchResult(
                id=fact.id,
                kind=ITEM_FACT,
                score=result.score_of(ITEM_FACT, fact.id),
                title=fact.organization or fact.role,
                text=fact.text,
                source_uri=_document_uri(fact),
                metadata={
                    "fact_kind": FactKind(fact.kind).value,
                    "impact_score": int(fact.impact_score or 0),
                    "confidence": float(fact.confidence or 0.0),
                    "user_verified": bool(fact.user_verified),
                    "skills": list(fact.skills or []),
                    "technologies": list(fact.technologies or []),
                    "reasons": result.reasons_for(ITEM_FACT, fact.id),
                },
            )
        )
    return hits


def _chunk_hits(result: RetrievalResult) -> list[KnowledgeSearchResult]:
    """Render the retrieved document chunks as search results.

    A chunk's vector id is its ``KnowledgeChunk`` primary key as a string. A record whose id
    is not a UUID cannot be linked back to a row, so it is skipped rather than surfaced as a
    dead result.

    Args:
        result: The retrieval to read.

    Returns:
        One hit per chunk whose id resolves.
    """
    hits: list[KnowledgeSearchResult] = []
    for hit in result.chunks:
        identifier = _parse_uuid(hit.id)
        if identifier is None:
            logger.warning("knowledge_service.chunk_id_unparseable", chunk_id=hit.id)
            continue
        metadata = dict(hit.metadata or {})
        title = metadata.get("title")
        uri = metadata.get("uri")
        metadata["reasons"] = result.reasons_for(ITEM_CHUNK, hit.id)
        hits.append(
            KnowledgeSearchResult(
                id=identifier,
                kind=ITEM_CHUNK,
                score=result.score_of(ITEM_CHUNK, hit.id) or float(hit.score),
                title=str(title) if title else None,
                text=hit.text,
                source_uri=str(uri) if uri else None,
                metadata=metadata,
            )
        )
    return hits


def _entity_hits(result: RetrievalResult) -> list[KnowledgeSearchResult]:
    """Render the retrieved graph entities as search results.

    Args:
        result: The retrieval to read.

    Returns:
        One hit per entity. ``text`` falls back to the name when the entity has no summary,
        because an empty result row is worse than a redundant one.
    """
    hits: list[KnowledgeSearchResult] = []
    for entity in result.entities:
        hits.append(
            KnowledgeSearchResult(
                id=entity.id,
                kind=ITEM_ENTITY,
                score=result.score_of(ITEM_ENTITY, entity.id),
                title=entity.name,
                text=entity.summary or entity.name,
                source_uri=None,
                metadata={
                    "entity_kind": EntityKind(entity.kind).value,
                    "mention_count": int(entity.mention_count or 0),
                    "confidence": float(entity.confidence or 0.0),
                    "aliases": list(entity.aliases or []),
                    "reasons": result.reasons_for(ITEM_ENTITY, entity.id),
                },
            )
        )
    return hits


def _memory_hits(result: RetrievalResult) -> list[KnowledgeSearchResult]:
    """Render the retrieved memories as search results.

    Args:
        result: The retrieval to read.

    Returns:
        One hit per memory entry.
    """
    hits: list[KnowledgeSearchResult] = []
    for memory in result.memories:
        hits.append(
            KnowledgeSearchResult(
                id=memory.id,
                kind=ITEM_MEMORY,
                score=result.score_of(ITEM_MEMORY, memory.id),
                title=None,
                text=memory.text,
                source_uri=None,
                metadata={
                    "memory_kind": str(getattr(memory.kind, "value", memory.kind)),
                    "weight": float(memory.weight or 0.0),
                    "reasons": result.reasons_for(ITEM_MEMORY, memory.id),
                },
            )
        )
    return hits


def _document_uri(fact: KnowledgeFact) -> str | None:
    """Return the uri of the document a fact came from, without triggering lazy IO.

    ``FactStore`` eagerly loads ``source_document`` on the queries retrieval uses, but a
    fact reaching this function by another route might not have it, and touching an unloaded
    relationship inside an async session raises ``MissingGreenlet``. Checking first turns
    that crash into a missing field.

    Args:
        fact: The fact to inspect.

    Returns:
        The document uri, or ``None`` when there is none or it is not loaded.
    """
    from sqlalchemy import inspect as sa_inspect

    if "source_document" in sa_inspect(fact).unloaded:
        return None
    document = fact.source_document
    return document.uri if document is not None else None


def _paging(supplied: Mapping[str, Any]) -> tuple[int, int]:
    """Read ``limit`` and ``offset`` out of a filter mapping, clamped to sane bounds.

    Args:
        supplied: The filter mapping.

    Returns:
        ``(limit, offset)``, with ``limit`` in ``[1, MAX_PAGE_LIMIT]`` and ``offset >= 0``.

    Raises:
        ValueError: If either value is not an integer.
    """
    raw_limit = supplied.get("limit")
    raw_offset = supplied.get("offset")
    limit = DEFAULT_PAGE_LIMIT if raw_limit is None else _coerce_int(raw_limit, "limit")
    offset = 0 if raw_offset is None else _coerce_int(raw_offset, "offset")
    return max(1, min(limit, MAX_PAGE_LIMIT)), max(0, offset)


def _first(supplied: Mapping[str, Any], *names: str) -> Any:
    """Return the first present, non-``None`` value among *names*.

    Lets a filter accept both the query-string spelling (``verified``) and the column
    spelling (``user_verified``) without duplicating the handling.

    Args:
        supplied: The filter mapping.
        *names: Keys to try, in order of preference.

    Returns:
        The value, or ``None`` when no name is present.
    """
    for name in names:
        value = supplied.get(name)
        if value is not None:
            return value
    return None


def _coerce_enum(value: Any, enum_cls: type[Any], label: str) -> Any:
    """Coerce one value to an enum member, or explain what was expected.

    Args:
        value: A member, or its string value in any casing.
        enum_cls: The target enum.
        label: Human name of the field, used in the error message.

    Returns:
        The enum member.

    Raises:
        ValueError: If the value is not a member of *enum_cls*.
    """
    if isinstance(value, enum_cls):
        return value
    text = str(getattr(value, "value", value)).strip().lower()
    try:
        return enum_cls(text)
    except ValueError as exc:
        options = ", ".join(sorted(member.value for member in enum_cls))
        raise ValueError(f"unknown {label} {text!r}. Expected one of: {options}") from exc


def _coerce_enums(value: Any, enum_cls: type[Any], label: str) -> list[Any]:
    """Coerce a scalar or an iterable of values to a de-duplicated list of enum members.

    Args:
        value: ``None``, one value, or an iterable of values.
        enum_cls: The target enum.
        label: Human name of the field, used in the error message.

    Returns:
        The members, in the order given, without duplicates. Empty for ``None``.

    Raises:
        ValueError: If any entry is not a member of *enum_cls*.
    """
    if value is None:
        return []
    candidates = [value] if isinstance(value, str | bytes | enum_cls) else list(value)
    members: list[Any] = []
    for candidate in candidates:
        member = _coerce_enum(candidate, enum_cls, label)
        if member not in members:
            members.append(member)
    return members


def _coerce_bool(value: Any, label: str) -> bool:
    """Coerce a query-string-ish value to a bool.

    Args:
        value: A bool, or one of ``true/false/1/0/yes/no/on/off`` in any casing.
        label: Human name of the field, used in the error message.

    Returns:
        The boolean.

    Raises:
        ValueError: If the value is not recognisably boolean.
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"{label} must be a boolean, got {value!r}")


def _coerce_int(value: Any, label: str) -> int:
    """Coerce a value to an int, or explain that it is not one.

    Args:
        value: The candidate.
        label: Human name of the field, used in the error message.

    Returns:
        The integer.

    Raises:
        ValueError: If the value cannot be read as an integer.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer, got {value!r}") from exc


def _coerce_float(value: Any, label: str) -> float:
    """Coerce a value to a float, or explain that it is not one.

    Args:
        value: The candidate.
        label: Human name of the field, used in the error message.

    Returns:
        The float.

    Raises:
        ValueError: If the value cannot be read as a number.
    """
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number, got {value!r}") from exc


def _coerce_uuid(value: Any, label: str) -> uuid.UUID:
    """Coerce a value to a UUID, or explain that it is not one.

    Args:
        value: A UUID or its string form.
        label: Human name of the field, used in the error message.

    Returns:
        The UUID.

    Raises:
        ValueError: If the value is not a valid UUID.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a uuid, got {value!r}") from exc


def _parse_uuid(value: str) -> uuid.UUID | None:
    """Parse a vector-store record id, returning ``None`` when it is not a UUID.

    Args:
        value: The record id.

    Returns:
        The UUID, or ``None``.
    """
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _clean_str(value: Any) -> str | None:
    """Trim a string, mapping empty and ``None`` alike to ``None``.

    A blank organization must become ``NULL`` rather than ``""``: the two would otherwise
    hash to different content hashes for the same fact.

    Args:
        value: The candidate.

    Returns:
        The trimmed string, or ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_list(values: Any) -> list[str]:
    """Normalise a JSON list column: trimmed, non-empty, de-duplicated, order preserved.

    Args:
        values: An iterable of candidates.

    Returns:
        The cleaned list.
    """
    cleaned: list[str] = []
    for value in values or ():
        text = str(value).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _escape_like(term: str) -> str:
    """Escape the characters ``LIKE`` treats as wildcards.

    Args:
        term: Raw user text.

    Returns:
        The escaped text, safe to embed in a pattern with ``escape="\\\\"``.
    """
    escaped = term
    for special in _LIKE_SPECIALS:
        escaped = escaped.replace(special, f"{_LIKE_ESCAPE}{special}")
    return escaped


def _like_pattern(term: str) -> str:
    """Build a case-insensitive "contains" pattern for *term*.

    Args:
        term: Raw user text.

    Returns:
        A lowercased, wildcard-escaped ``%term%`` pattern.
    """
    return f"%{_escape_like(term.lower())}%"
