"""The indexing pipeline — how a source on disk or on the web becomes retrievable knowledge.

``docs/CONTRACTS.md`` §8.3 names one class here, :class:`KnowledgeIndexer`, and one report,
:class:`IndexReport`. Between them they own the whole write path of the knowledge engine:
an analyzer produces plain dataclasses, and this module turns them into
:class:`~app.models.knowledge.KnowledgeSource`,
:class:`~app.models.knowledge.KnowledgeDocument`,
:class:`~app.models.knowledge.KnowledgeChunk`,
:class:`~app.models.knowledge.KnowledgeFact`,
:class:`~app.models.knowledge.KnowledgeEntity` and
:class:`~app.models.knowledge.KnowledgeEdge` rows plus the vector records that make them
searchable.

**The pipeline, in order** (:meth:`KnowledgeIndexer.index_source`):

1. Load the source and mark it :attr:`~app.models.enums.IndexStatus.INDEXING`.
2. Resolve its analyzer through :func:`~app.knowledge.analyzers.base.analyzer_for` — never
   by importing a concrete analyzer (golden rule #5).
3. Probe :meth:`~app.knowledge.analyzers.base.Analyzer.fingerprint`. **If it has not moved,
   stop here.** This one comparison is what makes continuous re-indexing cheap enough to run
   every hour: an untouched GitHub account or project folder costs one directory walk, not
   thousands of embeddings.
4. Analyze.
5. Upsert documents on ``(source_id, uri)``. A document whose ``content_hash`` is unchanged
   **keeps its existing chunks** and is never re-embedded. Documents that vanished from the
   source are deleted.
6. Chunk, embed **in batches**, replace each changed document's chunk rows inside a
   savepoint, and upsert the vectors into the ``chunks`` collection.
7. Merge facts (:class:`~app.knowledge.facts.FactStore`) and entities and edges
   (:class:`~app.knowledge.graph.KnowledgeGraph`).
8. Mark the source :attr:`~app.models.enums.IndexStatus.INDEXED`, storing the analysis
   fingerprint so step 3 can short-circuit the next run.

**A source is never left stuck in ``indexing``.** Every failure path — an analyzer raising,
a database error, a cancelled task — routes through :meth:`KnowledgeIndexer._fail`, which
rolls back, writes ``failed`` plus ``last_error`` with a Core ``UPDATE`` (so it works even
when the ORM identity map is poisoned by the failure that got us here), commits that, and
returns a report carrying the error. One broken source never aborts a batch.

**Change detection is a comparison between two digests produced by the same analyzer.**
:meth:`Analyzer.fingerprint` is documented to return "a digest comparable with the
``AnalysisResult.fingerprint`` this analyzer produces", so ``KnowledgeSource.content_hash``
stores the *analysis* fingerprint and the probe is compared against it. An analyzer with no
cheap probe inherits the default, whose identity digest can never equal a content digest, so
it is correctly re-analyzed every time; an analyzer that leaves ``fingerprint`` empty stores
``None`` and is likewise always re-analyzed. Failing in that direction costs time; failing in
the other would silently freeze the user's knowledge base, which is the one thing this
engine exists to prevent.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import structlog
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.cache.keys import make_key
from app.database.types import utcnow
from app.knowledge.analyzers.base import (
    AnalysisResult,
    Analyzer,
    ExtractedDocument,
    ExtractedFact,
    SourceRef,
    analyzer_for,
    chunk_text,
    estimate_tokens,
)
from app.knowledge.facts import FACT_COLLECTION, FactStore
from app.knowledge.graph import KnowledgeGraph, KnowledgeStore, chunked
from app.knowledge.vector.base import VectorRecord
from app.models.enums import IndexStatus, PluginKind, SourceKind
from app.models.knowledge import (
    CONTENT_HASH_LENGTH,
    TITLE_MAX_LENGTH,
    URI_MAX_LENGTH,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeFact,
    KnowledgeSource,
)
from app.observability.metrics import observe_knowledge_index, record_knowledge_document
from app.plugins.base import PluginNotFound

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ai.embeddings import Embedder
    from app.cache.base import Cache
    from app.config.settings import Settings
    from app.knowledge.vector.base import VectorStore

__all__ = [
    "CHUNK_COLLECTION",
    "DEFAULT_INDEX_CONCURRENCY",
    "EMBEDDING_BATCH_SIZE",
    "FINGERPRINT_PROBE_TTL_SECONDS",
    "INDEX_STAGES",
    "PROBE_CACHE_NAMESPACE",
    "SQLITE_INDEX_CONCURRENCY",
    "STAGE_ANALYZE",
    "STAGE_CHUNKS",
    "STAGE_DOCUMENTS",
    "STAGE_FACTS",
    "STAGE_FAILED",
    "STAGE_FINALIZE",
    "STAGE_FINGERPRINT",
    "STAGE_GRAPH",
    "STAGE_RESOLVE",
    "STAGE_SKIPPED",
    "STAGE_START",
    "IndexReport",
    "IndexerError",
    "KnowledgeIndexer",
    "SourceNotFoundError",
    "UnsupportedSourceError",
    "supported_source_kinds",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Vector-store collection holding one record per document chunk. Fixed by
#: ``docs/CONTRACTS.md`` §8.2's worked example and by
#: :class:`~app.knowledge.retrieval.KnowledgeRetriever`, which reads it.
CHUNK_COLLECTION: Final[str] = "chunks"

#: Chunks sent to the embedder per request. Bounded so that re-indexing a large corpus has a
#: flat memory profile and can report progress as it goes, rather than materialising every
#: vector of a ten-thousand-chunk source before writing any of them.
EMBEDDING_BATCH_SIZE: Final[int] = 64

#: Sources indexed concurrently by :meth:`KnowledgeIndexer.index_all` and
#: :meth:`KnowledgeIndexer.refresh_stale`. Analyzers are dominated by network and disk
#: latency, so a handful in flight is a large speedup; more than that mostly buys rate-limit
#: responses from GitHub.
DEFAULT_INDEX_CONCURRENCY: Final[int] = 4

#: Concurrency used when the database is SQLite. SQLite permits exactly one writer at a
#: time and this installation sets no ``busy_timeout``, so parallel indexing would trade
#: analyzer latency for ``database is locked`` errors. Serialising is both faster and
#: correct there.
SQLITE_INDEX_CONCURRENCY: Final[int] = 1

#: Cache namespace for the memoised fingerprint probe.
PROBE_CACHE_NAMESPACE: Final[str] = "knowledge"

#: How long a fingerprint probe is reused (see :meth:`KnowledgeIndexer._fingerprint`).
#: Deliberately tiny: it exists only to collapse the burst of overlapping requests a desktop
#: launch produces — the app fires a reindex, the scheduler fires ``refresh_stale``, and the
#: user clicks the button — into one upstream probe. ``force=True`` always bypasses it.
FINGERPRINT_PROBE_TTL_SECONDS: Final[int] = 30

#: Longest ``last_error`` written to a source. The column is unbounded ``TEXT``; a traceback
#: repr from a badly behaved dependency is not worth storing in full.
MAX_ERROR_CHARS: Final[int] = 2000

#: ``analyzer`` label used when a pass failed before an analyzer could be resolved — an
#: unsupported source kind, or a plugin that is not installed. Bounded, like every other
#: value this label takes.
_UNKNOWN_ANALYZER: Final[str] = "unknown"

#: Multiple of ``settings.knowledge_reindex_interval_minutes`` after which a source still
#: marked ``indexing`` is presumed abandoned — the process died mid-pass — and becomes
#: eligible for :meth:`KnowledgeIndexer.refresh_stale` again. Without this a hard crash
#: would strand a source outside every refresh query forever.
STALE_INDEXING_GRACE_MULTIPLIER: Final[int] = 2

# -- progress stages -------------------------------------------------------------------
#
# Emitted through ``progress_callback`` and published by the API as
# ``knowledge.index_progress``. The names are part of the desktop app's contract, so they
# are constants rather than inline literals.

STAGE_START: Final[str] = "start"
STAGE_RESOLVE: Final[str] = "resolve_analyzer"
STAGE_FINGERPRINT: Final[str] = "fingerprint"
STAGE_ANALYZE: Final[str] = "analyze"
STAGE_DOCUMENTS: Final[str] = "documents"
STAGE_CHUNKS: Final[str] = "chunks"
STAGE_FACTS: Final[str] = "facts"
STAGE_GRAPH: Final[str] = "graph"
STAGE_FINALIZE: Final[str] = "finalize"
STAGE_SKIPPED: Final[str] = "skipped"
STAGE_FAILED: Final[str] = "failed"

#: Every stage, in pipeline order, so a client can render a progress bar with a known
#: denominator instead of guessing.
INDEX_STAGES: Final[tuple[str, ...]] = (
    STAGE_START,
    STAGE_RESOLVE,
    STAGE_FINGERPRINT,
    STAGE_ANALYZE,
    STAGE_DOCUMENTS,
    STAGE_CHUNKS,
    STAGE_FACTS,
    STAGE_GRAPH,
    STAGE_FINALIZE,
)

#: Type of the optional progress hook. Async because the real implementation publishes to
#: the WebSocket :class:`~app.api.events.EventBus`.
ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


# ======================================================================================
# Errors
# ======================================================================================


class IndexerError(RuntimeError):
    """Base class for failures raised by :class:`KnowledgeIndexer` itself.

    Analyzer failures are *reported* (on :attr:`IndexReport.errors`) rather than raised, so
    everything in this hierarchy is a caller error: an unknown source, a source kind nothing
    can read.
    """


class SourceNotFoundError(IndexerError, LookupError):
    """No :class:`~app.models.knowledge.KnowledgeSource` has the requested id.

    Also a :class:`LookupError`, so an API layer that maps ``LookupError`` to ``404`` needs
    no special case for it.

    Attributes:
        source_id: The id that could not be resolved.
    """

    def __init__(self, source_id: uuid.UUID) -> None:
        """Name the missing source.

        Args:
            source_id: The id that could not be resolved.
        """
        super().__init__(f"knowledge source {source_id} does not exist")
        self.source_id = source_id


class UnsupportedSourceError(IndexerError, ValueError):
    """No registered analyzer can read this source.

    Raised by :meth:`KnowledgeIndexer.add_source` *before* anything is written, so a typo in
    a source kind is rejected at the point of entry instead of becoming a row that fails to
    index forever. The message lists the kinds that *are* supported, which turns the problem
    into a one-line fix.

    Attributes:
        kind: The rejected source kind.
        supported: Every source kind some registered analyzer claims.
    """

    def __init__(self, kind: SourceKind, supported: Sequence[SourceKind]) -> None:
        """Describe the rejection and the alternatives.

        Args:
            kind: The rejected source kind.
            supported: The kinds a registered analyzer supports.
        """
        names = ", ".join(sorted(item.value for item in supported)) or "none registered"
        super().__init__(
            f"no registered analyzer supports source kind {kind.value!r}; supported kinds: {names}"
        )
        self.kind = kind
        self.supported = tuple(supported)


# ======================================================================================
# Report
# ======================================================================================


@dataclass(slots=True)
class IndexReport:
    """The outcome of one indexing pass over one source (``docs/CONTRACTS.md`` §8.3).

    ``skipped`` is the common — and desirable — case: the fingerprint had not moved, so
    nothing was re-extracted, re-chunked or re-embedded. A report with entries in
    :attr:`errors` may still have indexed successfully; analyzers degrade rather than
    explode, and a crawl that lost one page still returns the other thirty-nine.

    Attributes:
        source_id: The source this pass covered.
        documents: Documents present in the source after the pass.
        chunks: Chunk rows written during the pass. Unchanged documents contribute zero —
            that is the point of the content-hash check.
        facts: Claims that reached the fact store, inserted or merged.
        entities: Graph nodes upserted.
        edges: Graph relationships upserted.
        skipped: Whether the pass short-circuited because nothing had changed.
        duration_seconds: Wall-clock time of the whole pass.
        errors: Non-fatal problems, plus the fatal one when the pass failed.
        failed: Whether the pass ended in the failure path. Appended after the fields
            ``docs/CONTRACTS.md`` §8.3 fixes, and ignored by
            :class:`~app.schemas.knowledge.IndexReportRead`; it exists so a caller can tell
            "indexed, with one page missing" from "did not index".
    """

    source_id: uuid.UUID
    documents: int = 0
    chunks: int = 0
    facts: int = 0
    entities: int = 0
    edges: int = 0
    skipped: bool = False
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    failed: bool = False

    @property
    def ok(self) -> bool:
        """Whether the pass completed without a fatal error.

        A report carrying only analyzer-level warnings is still ``ok``; only
        :meth:`KnowledgeIndexer._fail` produces a report that is not, and it always sets
        :attr:`failed`.
        """
        return not self.failed

    def counts(self) -> dict[str, int]:
        """Return the numeric fields as a mapping, for logging and progress events.

        Returns:
            The five write counts, keyed by field name.
        """
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "facts": self.facts,
            "entities": self.entities,
            "edges": self.edges,
        }

    def record_error(self, message: str) -> None:
        """Append a problem description, ignoring blanks.

        Args:
            message: What went wrong, phrased for the operator reading it in the desktop
                app.
        """
        text = str(message).strip()
        if text:
            self.errors.append(text[:MAX_ERROR_CHARS])


# ======================================================================================
# Analyzer inventory
# ======================================================================================


def supported_source_kinds() -> list[SourceKind]:
    """Return every source kind some registered analyzer claims.

    Resolved through :mod:`app.plugins.registry` rather than by importing analyzers, per
    golden rule #5, and triggers plugin discovery when it has not run yet — so this is
    correct from a script, a worker, or a request handler alike.

    Returns:
        The supported kinds, sorted by value.
    """
    from app.plugins.loader import is_loaded, load_all
    from app.plugins.registry import registry

    if not is_loaded():
        load_all()

    kinds: set[SourceKind] = set()
    for candidate in registry.all(PluginKind.ANALYZER):
        if isinstance(candidate, Analyzer):
            kinds.update(type(candidate).source_kinds)
    return sorted(kinds, key=lambda kind: kind.value)


# ======================================================================================
# The indexer
# ======================================================================================


class KnowledgeIndexer:
    """Runs knowledge sources through the pipeline and keeps their state honest.

    Unlike the three stores in this package, the indexer **owns its transaction**: every
    public method commits before it returns. That is deliberate. A pass writes documents,
    chunks, facts, entities and edges, and a caller composing that into a wider unit of work
    would either hold a write transaction open across a network crawl or lose the whole pass
    to an unrelated failure. Committing here is also what lets :meth:`index_all` guarantee
    that one broken source never costs the others their results.

    Attributes:
        session: The session single-source operations run on.
        settings: Chunk sizes, refresh interval, scan limits.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        cache: Cache | None = None,
        vector_store: VectorStore | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Bind the indexer to a session and its collaborators.

        Args:
            session: The async session single-source operations run on.
            settings: Application settings.
            embedder: Embedder override; resolved from settings on first use when omitted.
            cache: Cache override; resolved from settings on first use when omitted. Used
                for the short-lived fingerprint probe memo (see :meth:`_fingerprint`).
            vector_store: Vector index override; resolved from settings on first use when
                omitted. Pass an
                :class:`~app.knowledge.vector.memory_store.InMemoryVectorStore` in tests.
            progress_callback: Awaited once per pipeline stage with
                ``(stage, payload)``. The API layer publishes these as
                ``knowledge.index_progress`` so the desktop app can show a live indexing
                run. A callback that raises is logged and ignored — telemetry never fails an
                index.
        """
        self.session = session
        self.settings = settings
        self._embedder = embedder
        self._cache = cache
        self._vector_store = vector_store
        self._progress = progress_callback

        self._store = KnowledgeStore(session, embedder=embedder, vector_store=vector_store)
        self._graph = KnowledgeGraph(session, embedder=embedder, vector_store=vector_store)
        self._facts = FactStore(session, embedder=embedder, vector_store=vector_store)

    # ----------------------------------------------------------------------------------
    # Collaborators
    # ----------------------------------------------------------------------------------

    @property
    def graph(self) -> KnowledgeGraph:
        """The graph store this indexer writes entities and edges through."""
        return self._graph

    @property
    def facts(self) -> FactStore:
        """The fact store this indexer merges claims through."""
        return self._facts

    @property
    def cache(self) -> Cache:
        """The cache backing the fingerprint probe memo, resolved on first use."""
        if self._cache is None:
            from app.cache import get_cache

            self._cache = get_cache()
        return self._cache

    def _clone_for(self, session: AsyncSession) -> KnowledgeIndexer:
        """Return an indexer with this one's configuration bound to *session*.

        :meth:`index_all` gives every source its own session — an
        :class:`~sqlalchemy.ext.asyncio.AsyncSession` is not safe to use from two
        concurrent tasks, and each source has to commit independently anyway.

        Args:
            session: The session the clone should use.

        Returns:
            A new indexer sharing this one's settings, embedder, cache, vector store and
            progress callback.
        """
        return KnowledgeIndexer(
            session,
            self.settings,
            embedder=self._embedder,
            cache=self._cache,
            vector_store=self._vector_store,
            progress_callback=self._progress,
        )

    # ----------------------------------------------------------------------------------
    # Progress
    # ----------------------------------------------------------------------------------

    async def _emit(self, stage: str, **payload: Any) -> None:
        """Report *stage* to the progress callback, if one was supplied.

        Args:
            stage: One of :data:`INDEX_STAGES`, :data:`STAGE_SKIPPED` or
                :data:`STAGE_FAILED`.
            **payload: Stage-specific counts and identifiers.
        """
        if self._progress is None:
            return
        try:
            await self._progress(stage, dict(payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("knowledge.progress_callback_failed", stage=stage, error=str(exc))

    # ----------------------------------------------------------------------------------
    # Sources
    # ----------------------------------------------------------------------------------

    async def add_source(self, user_id: uuid.UUID, ref: SourceRef) -> KnowledgeSource:
        """Register *ref* for indexing, or return the row that already covers it.

        Identity is ``(user_id, kind, uri)``, matching the table's unique constraint, so
        pointing the app at the same repository twice updates one row rather than creating a
        second one that indexes the same content into the same graph.

        The source kind is validated against the registered analyzers *first*: a source
        nothing can read is rejected here, where the user can act on the message, instead of
        becoming a row that fails on every pass forever.

        Args:
            user_id: Owning user.
            ref: What to index. ``label`` and ``config`` are copied onto the row; a changed
                ``config`` marks an existing source :attr:`~app.models.enums.IndexStatus.STALE`
                because it changes what the analyzer will read.

        Returns:
            The stored source, committed.

        Raises:
            UnsupportedSourceError: If no registered analyzer supports *ref*.
        """
        self._require_analyzer(ref)

        existing = await self._find_source(user_id, ref.kind, ref.uri)
        if existing is not None:
            return await self._update_source(existing, ref)

        source = KnowledgeSource(
            user_id=user_id,
            kind=ref.kind,
            uri=ref.uri[:URI_MAX_LENGTH],
            label=ref.label[:TITLE_MAX_LENGTH] if ref.label else None,
            config=dict(ref.config or {}),
            enabled=True,
            index_status=IndexStatus.PENDING,
            auto_refresh=True,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(source)
                await self.session.flush()
        except IntegrityError:
            # Lost a race with a concurrent registration of the same (user, kind, uri).
            if source in self.session:  # pragma: no cover - savepoint usually expunges it
                self.session.expunge(source)
            winner = await self._find_source(user_id, ref.kind, ref.uri)
            if winner is None:
                raise
            logger.debug("knowledge.source_insert_race", user_id=str(user_id), uri=ref.uri)
            return await self._update_source(winner, ref)

        await self.session.commit()
        logger.info(
            "knowledge.source_added",
            user_id=str(user_id),
            source_id=str(source.id),
            kind=ref.kind.value,
            uri=ref.uri,
        )
        return source

    def _require_analyzer(self, ref: SourceRef) -> Analyzer:
        """Return the analyzer that will handle *ref*, or explain why there is none.

        Args:
            ref: The candidate source.

        Returns:
            The resolved analyzer.

        Raises:
            UnsupportedSourceError: If no registered analyzer supports *ref*, listing the
                kinds that are supported.
        """
        try:
            return analyzer_for(ref)
        except PluginNotFound as exc:
            raise UnsupportedSourceError(ref.kind, supported_source_kinds()) from exc

    async def _update_source(self, source: KnowledgeSource, ref: SourceRef) -> KnowledgeSource:
        """Fold a re-registration into an existing source row.

        Args:
            source: The incumbent row.
            ref: The incoming registration.

        Returns:
            *source*, committed.
        """
        config = dict(ref.config or {})
        if config != dict(source.config or {}):
            source.config = config
            # The analyzer will read something different next time — branch, depth, globs —
            # so what is stored no longer reflects the source. Mark it for the refresh
            # worker instead of silently keeping a fingerprint that now means nothing.
            source.index_status = IndexStatus.STALE
            source.content_hash = None
        if ref.label:
            source.label = ref.label[:TITLE_MAX_LENGTH]
        await self.session.commit()
        logger.info(
            "knowledge.source_updated",
            source_id=str(source.id),
            kind=SourceKind(source.kind).value,
            uri=source.uri,
            status=IndexStatus(source.index_status).value,
        )
        return source

    async def _find_source(
        self, user_id: uuid.UUID, kind: SourceKind, uri: str
    ) -> KnowledgeSource | None:
        """Return the source identified by the table's unique key, or ``None``.

        Args:
            user_id: Owning user.
            kind: Source kind.
            uri: Source uri.

        Returns:
            The matching row, or ``None``.
        """
        statement = (
            select(KnowledgeSource)
            .where(
                KnowledgeSource.user_id == user_id,
                KnowledgeSource.kind == kind,
                KnowledgeSource.uri == uri,
            )
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def remove_source(self, source_id: uuid.UUID) -> None:
        """Delete a source, its documents, its chunks and its vector records.

        **Knowledge outlives its source.** Facts and graph nodes learned from this source
        stay in the database — a verified accomplishment does not stop being true because
        the user unregistered the repository it was first read from. What changes is that
        facts sourced from it are marked ``is_active=False`` and their vectors removed, so
        they stop being cited by generated resumes while remaining a full audit trail.

        Idempotent: removing an unknown source is a no-op.

        Args:
            source_id: The source to remove.
        """
        source = await self.session.get(KnowledgeSource, source_id)
        if source is None:
            logger.debug("knowledge.remove_source_noop", source_id=str(source_id))
            return

        document_ids = list(
            (
                await self.session.execute(
                    select(KnowledgeDocument.id).where(KnowledgeDocument.source_id == source_id)
                )
            )
            .scalars()
            .all()
        )
        chunk_ids = await self._chunk_ids(document_ids)
        fact_ids = await self._fact_ids_for_documents(document_ids)

        deactivated = 0
        for chunk in chunked(fact_ids):
            result = await self.session.execute(
                update(KnowledgeFact)
                .where(KnowledgeFact.id.in_(chunk))
                .values(is_active=False, updated_at=utcnow())
                .execution_options(synchronize_session="fetch")
            )
            # ``execute`` is annotated as returning the general ``Result``, but a DML
            # statement always produces a ``CursorResult`` — the class carrying ``rowcount``.
            deactivated += int(cast("CursorResult[Any]", result).rowcount or 0)

        await self.session.delete(source)
        await self.session.commit()

        # Vector records last: the database is the source of truth, and a stale vector that
        # survives a failure here is invisible (its row is gone, so retrieval drops it)
        # whereas a deleted vector whose row survived would silently degrade search.
        removed_chunks = await self._store.vector_delete(
            CHUNK_COLLECTION, [str(value) for value in chunk_ids]
        )
        removed_facts = await self._store.vector_delete(
            FACT_COLLECTION, [str(value) for value in fact_ids]
        )

        logger.info(
            "knowledge.source_removed",
            source_id=str(source_id),
            documents=len(document_ids),
            chunks=len(chunk_ids),
            facts_deactivated=deactivated,
            vectors_removed=removed_chunks + removed_facts,
        )

    async def _chunk_ids(self, document_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        """Return every chunk id belonging to *document_ids*.

        Args:
            document_ids: The documents to collect chunks for.

        Returns:
            The chunk ids, in no particular order.
        """
        collected: list[uuid.UUID] = []
        for chunk in chunked(list(document_ids)):
            statement = select(KnowledgeChunk.id).where(KnowledgeChunk.document_id.in_(chunk))
            collected.extend((await self.session.execute(statement)).scalars().all())
        return collected

    async def _fact_ids_for_documents(self, document_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        """Return the ids of active facts whose provenance is one of *document_ids*.

        Collected **before** the documents are deleted: ``source_document_id`` is
        ``ON DELETE SET NULL``, so afterwards the link no longer exists.

        Args:
            document_ids: The documents whose facts should be found.

        Returns:
            The fact ids.
        """
        collected: list[uuid.UUID] = []
        for chunk in chunked(list(document_ids)):
            statement = select(KnowledgeFact.id).where(
                KnowledgeFact.source_document_id.in_(chunk),
                KnowledgeFact.is_active.is_(True),
            )
            collected.extend((await self.session.execute(statement)).scalars().all())
        return collected

    # ----------------------------------------------------------------------------------
    # The pipeline
    # ----------------------------------------------------------------------------------

    async def index_source(self, source_id: uuid.UUID, *, force: bool = False) -> IndexReport:
        """Run one source through the whole pipeline.

        See the module docstring for the eight steps and why step 3 is the one that makes
        continuous indexing affordable.

        Produces both knowledge series of ``docs/CONTRACTS.md`` §16:
        ``applicantos_knowledge_documents_total{kind}``, one increment per document
        upserted at step 5, and ``applicantos_knowledge_index_duration_seconds{analyzer}``
        over the whole pass, recorded however it ends.

        Args:
            source_id: The source to index.
            force: Re-analyze, re-chunk and re-embed even when nothing changed. Also
                bypasses the fingerprint probe memo.

        Returns:
            A populated :class:`IndexReport`. Never raises for an analyzer or database
            failure: the source is marked ``failed`` with its error and the report carries
            it, because one unreachable repository must not abort a batch.

        Raises:
            SourceNotFoundError: If no such source exists — a caller error, not an indexing
                failure.
        """
        started = time.perf_counter()
        report = IndexReport(source_id=source_id)

        source = await self.session.get(KnowledgeSource, source_id)
        if source is None:
            raise SourceNotFoundError(source_id)

        user_id = source.user_id
        ref = self._source_ref(source)
        log = logger.bind(
            source_id=str(source_id),
            user_id=str(user_id),
            kind=ref.kind.value,
            uri=ref.uri,
        )
        # Labels ``applicantos_knowledge_index_duration_seconds`` in the ``finally`` below.
        # Rebound as soon as step 2 resolves the plugin; a pass that fails before that is
        # still worth timing, and reports itself as the unknown analyzer.
        analyzer_name = _UNKNOWN_ANALYZER

        try:
            # 1 -- claim the source ------------------------------------------------------
            source.index_status = IndexStatus.INDEXING
            await self.session.commit()
            await self._emit(STAGE_START, source_id=str(source_id), uri=ref.uri, force=force)

            # 2 -- resolve the analyzer --------------------------------------------------
            analyzer = analyzer_for(ref)
            analyzer_name = analyzer.name
            await self._emit(STAGE_RESOLVE, source_id=str(source_id), analyzer=analyzer.name)

            # 3 -- cheap change probe ----------------------------------------------------
            probe = await self._fingerprint(analyzer, ref, source_id, force=force)
            await self._emit(
                STAGE_FINGERPRINT,
                source_id=str(source_id),
                fingerprint=probe,
                stored=source.content_hash,
            )
            if not force and probe and source.content_hash and probe == source.content_hash:
                report.skipped = True
                await self._finalize(source, fingerprint=source.content_hash)
                report.duration_seconds = time.perf_counter() - started
                await self._emit(
                    STAGE_SKIPPED,
                    source_id=str(source_id),
                    duration_seconds=round(report.duration_seconds, 4),
                )
                log.info(
                    "knowledge.index_skipped",
                    analyzer=analyzer.name,
                    duration_seconds=round(report.duration_seconds, 4),
                )
                return report

            # 4 -- analyze ---------------------------------------------------------------
            analyze_started = time.perf_counter()
            result = await analyzer.analyze(ref)
            result.deduplicate()
            for message in result.errors:
                report.record_error(message)
            await self._emit(
                STAGE_ANALYZE,
                source_id=str(source_id),
                duration_seconds=round(time.perf_counter() - analyze_started, 4),
                **result.counts(),
            )
            log.info(
                "knowledge.analyzed",
                analyzer=analyzer.name,
                duration_seconds=round(time.perf_counter() - analyze_started, 4),
                **result.counts(),
            )

            # 5 -- documents -------------------------------------------------------------
            documents, pending = await self._upsert_documents(source, result.documents, force=force)
            report.documents = len(documents)
            # ``applicantos_knowledge_documents_total{kind}`` (§16): one increment per
            # document this pass upserted, grouped so the recorder is called once per kind
            # rather than once per document. The kind is the document's own — an analyzer
            # may refine it away from the source's declared kind.
            for document_kind, kind_count in Counter(
                row.kind for row in documents.values()
            ).items():
                record_knowledge_document(document_kind, kind_count)
            await self._emit(
                STAGE_DOCUMENTS,
                source_id=str(source_id),
                documents=report.documents,
                changed=len(pending),
            )

            # 6 -- chunk, embed, index ---------------------------------------------------
            chunk_started = time.perf_counter()
            report.chunks = await self._rechunk(pending)
            await self._emit(
                STAGE_CHUNKS,
                source_id=str(source_id),
                chunks=report.chunks,
                duration_seconds=round(time.perf_counter() - chunk_started, 4),
            )

            # 7a -- facts ----------------------------------------------------------------
            report.facts = await self._merge_facts(user_id, result.facts, documents)
            await self._emit(STAGE_FACTS, source_id=str(source_id), facts=report.facts)

            # 7b -- graph ----------------------------------------------------------------
            report.entities, report.edges = await self._merge_graph(user_id, result, report)
            await self._emit(
                STAGE_GRAPH,
                source_id=str(source_id),
                entities=report.entities,
                edges=report.edges,
            )

            # 8 -- publish ---------------------------------------------------------------
            await self._finalize(source, fingerprint=result.fingerprint)
            report.duration_seconds = time.perf_counter() - started
            await self._emit(
                STAGE_FINALIZE,
                source_id=str(source_id),
                duration_seconds=round(report.duration_seconds, 4),
                **report.counts(),
            )
            log.info(
                "knowledge.indexed",
                analyzer=analyzer.name,
                duration_seconds=round(report.duration_seconds, 4),
                errors=len(report.errors),
                **report.counts(),
            )
            return report

        except asyncio.CancelledError:
            # Cancellation is a failure like any other as far as the *row* is concerned: a
            # source left in `indexing` is invisible to every refresh query, which is
            # exactly the stuck state this method must never produce.
            await self._fail(source_id, "indexing was cancelled")
            report.failed = True
            report.record_error("indexing was cancelled")
            report.duration_seconds = time.perf_counter() - started
            await self._emit(STAGE_FAILED, source_id=str(source_id), error="cancelled")
            log.warning("knowledge.index_cancelled")
            return report
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            await self._fail(source_id, message)
            report.failed = True
            report.record_error(message)
            report.duration_seconds = time.perf_counter() - started
            await self._emit(STAGE_FAILED, source_id=str(source_id), error=message)
            log.warning("knowledge.index_failed", error=message, exc_info=True)
            return report
        finally:
            # ``applicantos_knowledge_index_duration_seconds{analyzer}`` (§16) spans the
            # whole analyze-through-embed pass. In the ``finally`` because every exit
            # matters: a fingerprint skip is the millisecond case the bucket ladder was
            # built for, and a pass that failed after five minutes is the one an operator
            # most wants to see.
            observe_knowledge_index(analyzer_name, time.perf_counter() - started)

    @staticmethod
    def _source_ref(source: KnowledgeSource) -> SourceRef:
        """Return the database-free pointer an analyzer takes.

        Args:
            source: The stored source.

        Returns:
            The equivalent :class:`~app.knowledge.analyzers.base.SourceRef`.
        """
        return SourceRef(
            kind=SourceKind(source.kind),
            uri=source.uri,
            label=source.label,
            config=dict(source.config or {}),
        )

    async def _fingerprint(
        self,
        analyzer: Analyzer,
        ref: SourceRef,
        source_id: uuid.UUID,
        *,
        force: bool,
    ) -> str:
        """Probe *ref* for changes, reusing a very recent probe when there is one.

        The memo exists for one situation and is sized for it: launching the desktop app
        fires an explicit reindex, the scheduler's ``refresh_stale`` beat, and often a user
        click, all within a few seconds, and each would otherwise walk the same directory or
        call the same API. :data:`FINGERPRINT_PROBE_TTL_SECONDS` is far shorter than any
        realistic edit-then-reindex cycle, and ``force`` bypasses it entirely.

        Args:
            analyzer: The resolved analyzer.
            ref: The source being probed.
            source_id: Owning source, part of the memo key.
            force: When ``True``, always probe upstream and do not read the memo.

        Returns:
            The probe digest, or ``""`` when the analyzer could not produce one — which
            never equals a stored fingerprint, so the source is re-analyzed.
        """
        key = make_key(
            PROBE_CACHE_NAMESPACE,
            "fingerprint",
            str(source_id),
            analyzer.name,
            ref.kind.value,
            ref.uri,
            ref.config,
        )
        if not force:
            try:
                memoized = await self.cache.get(key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("knowledge.probe_cache_unavailable", error=str(exc))
                memoized = None
            if isinstance(memoized, str) and memoized:
                logger.debug("knowledge.fingerprint_memo_hit", source_id=str(source_id))
                return memoized

        probe = str(await analyzer.fingerprint(ref) or "")
        if probe:
            try:
                await self.cache.set(key, probe, ttl=FINGERPRINT_PROBE_TTL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("knowledge.probe_cache_write_failed", error=str(exc))
        return probe

    # ----------------------------------------------------------------------------------
    # Step 5 — documents
    # ----------------------------------------------------------------------------------

    async def _upsert_documents(
        self,
        source: KnowledgeSource,
        documents: Sequence[ExtractedDocument],
        *,
        force: bool,
    ) -> tuple[dict[str, KnowledgeDocument], list[KnowledgeDocument]]:
        """Reconcile the source's document rows with what the analyzer just produced.

        Three outcomes per document, keyed on ``(source_id, uri)``:

        * **new** — inserted and queued for chunking;
        * **changed** — updated in place and queued for chunking, because its
          ``content_hash`` moved;
        * **unchanged** — updated in place (title, metadata) but **not** re-chunked, so its
          existing embeddings survive. This is where the money is: a project folder with two
          hundred documents and one edited README re-embeds one document.

        Documents the analyzer no longer produces are deleted, along with their chunks
        (database cascade) and their vector records. Facts extracted from them survive with
        ``source_document_id`` set to ``NULL``.

        Args:
            source: The owning source.
            documents: The analyzer's documents.
            force: Re-chunk every document regardless of its content hash.

        Returns:
            ``(by_uri, pending)`` — every document row that now belongs to the source keyed
            by uri, and the subset that needs chunking.
        """
        existing = {row.uri: row for row in await self._existing_documents(source.id)}
        by_uri: dict[str, KnowledgeDocument] = {}
        pending: list[KnowledgeDocument] = []
        unchanged: list[KnowledgeDocument] = []

        for document in documents:
            uri = document.uri[:URI_MAX_LENGTH]
            row = existing.get(uri)
            changed = force or row is None or row.content_hash != document.content_hash
            if row is None:
                row = KnowledgeDocument(user_id=source.user_id, source_id=source.id, uri=uri)
                self.session.add(row)
            self._apply_document(row, document)
            by_uri[uri] = row
            (pending if changed else unchanged).append(row)

        await self.session.flush()

        # A document that was never chunked — the embedding step failed last time, or the
        # process died between the two commits — must be picked up even though its hash is
        # unchanged, or it would stay invisible to semantic search forever.
        if unchanged:
            counts = await self._chunk_counts([row.id for row in unchanged])
            pending.extend(row for row in unchanged if counts.get(row.id, 0) == 0)

        removed = [row for uri, row in existing.items() if uri not in by_uri]
        if removed:
            await self._delete_documents(removed)

        logger.debug(
            "knowledge.documents_upserted",
            source_id=str(source.id),
            total=len(by_uri),
            pending=len(pending),
            removed=len(removed),
        )
        return by_uri, pending

    @staticmethod
    def _apply_document(row: KnowledgeDocument, document: ExtractedDocument) -> None:
        """Copy an extracted document onto its row.

        Args:
            row: The row to write, new or existing.
            document: The analyzer's output.
        """
        row.kind = SourceKind(document.kind)
        row.title = (document.title or document.uri)[:TITLE_MAX_LENGTH]
        row.raw_text = document.text or ""
        row.content_hash = document.content_hash[:CONTENT_HASH_LENGTH]
        row.metadata_json = dict(document.metadata or {})
        row.token_count = estimate_tokens(row.raw_text)

    async def _existing_documents(self, source_id: uuid.UUID) -> list[KnowledgeDocument]:
        """Return every document currently stored for *source_id*.

        Args:
            source_id: The owning source.

        Returns:
            The document rows.
        """
        statement = select(KnowledgeDocument).where(KnowledgeDocument.source_id == source_id)
        return list((await self.session.execute(statement)).scalars().all())

    async def _chunk_counts(self, document_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Count the chunk rows each of *document_ids* currently has.

        Args:
            document_ids: The documents to count for.

        Returns:
            Document id to chunk count; documents with none are absent.
        """
        counts: dict[uuid.UUID, int] = {}
        for chunk in chunked(list(document_ids)):
            statement = (
                select(KnowledgeChunk.document_id, func.count(KnowledgeChunk.id))
                .where(KnowledgeChunk.document_id.in_(chunk))
                .group_by(KnowledgeChunk.document_id)
            )
            for document_id, count in (await self.session.execute(statement)).all():
                counts[document_id] = int(count)
        return counts

    async def _delete_documents(self, rows: Sequence[KnowledgeDocument]) -> None:
        """Delete documents that no longer exist upstream, and their vector records.

        Args:
            rows: The document rows to remove.
        """
        document_ids = [row.id for row in rows]
        chunk_ids = await self._chunk_ids(document_ids)
        for row in rows:
            await self.session.delete(row)
        await self.session.flush()
        await self._store.vector_delete(CHUNK_COLLECTION, [str(value) for value in chunk_ids])
        logger.info(
            "knowledge.documents_removed",
            documents=len(rows),
            chunks=len(chunk_ids),
        )

    # ----------------------------------------------------------------------------------
    # Step 6 — chunks and embeddings
    # ----------------------------------------------------------------------------------

    async def _rechunk(self, documents: Sequence[KnowledgeDocument]) -> int:
        """Re-chunk, re-embed and re-index every document in *documents*.

        The one place chunking happens. Text is split by
        :func:`~app.knowledge.analyzers.base.chunk_text` (the system's only chunker, so
        retrieval behaves identically whatever produced the text), embedded in batches of
        :data:`EMBEDDING_BATCH_SIZE`, and then the rows are swapped inside a **savepoint**:
        the old chunks are deleted and the new ones inserted as one atomic step, so a
        failure can never leave a document half-chunked and therefore half-searchable.

        Args:
            documents: Documents whose chunks must be rebuilt.

        Returns:
            The number of chunk rows written.
        """
        if not documents:
            return 0

        planned: list[tuple[KnowledgeDocument, list[str]]] = []
        texts: list[str] = []
        for document in documents:
            pieces = chunk_text(
                document.raw_text or "",
                max_tokens=self.settings.knowledge_chunk_tokens,
                overlap=self.settings.knowledge_chunk_overlap,
            )
            planned.append((document, pieces))
            texts.extend(pieces)

        vectors = await self._embed_batched(texts) if texts else []
        if vectors is None:
            logger.warning(
                "knowledge.chunks_unembedded",
                documents=len(documents),
                chunks=len(texts),
                detail="chunks stored without vectors; run the embedding backlog",
            )
            vectors = []

        stale_ids = await self._chunk_ids([document.id for document in documents])

        written: list[tuple[KnowledgeDocument, KnowledgeChunk]] = []
        cursor = 0
        moment = utcnow()

        async with self.session.begin_nested():
            for chunk in chunked([document.id for document in documents]):
                await self.session.execute(
                    delete(KnowledgeChunk)
                    .where(KnowledgeChunk.document_id.in_(chunk))
                    .execution_options(synchronize_session="fetch")
                )
            await self.session.flush()

            for document, pieces in planned:
                for ordinal, text in enumerate(pieces):
                    position = cursor + ordinal
                    row = KnowledgeChunk(
                        document_id=document.id,
                        ordinal=ordinal,
                        text=text,
                        token_count=estimate_tokens(text),
                        embedding=(list(vectors[position]) if position < len(vectors) else None),
                        metadata_json={
                            "document_uri": document.uri,
                            "kind": SourceKind(document.kind).value,
                        },
                    )
                    self.session.add(row)
                    written.append((document, row))
                cursor += len(pieces)
                document.indexed_at = moment
            # Inside the savepoint: the delete and the insert land together, so a failure
            # can never leave a document half-chunked and therefore half-searchable.
            await self.session.flush()

        # Primary keys exist only after that flush, so the records are built from the rows
        # themselves rather than re-queried.
        records = [
            VectorRecord(
                id=str(row.id),
                embedding=list(row.embedding or []),
                text=row.text,
                metadata={
                    "user_id": str(document.user_id),
                    "kind": SourceKind(document.kind).value,
                    "document_id": str(document.id),
                    "source_id": str(document.source_id),
                    "ordinal": int(row.ordinal),
                    "title": document.title,
                    "uri": document.uri,
                },
            )
            for document, row in written
            if row.embedding is not None
        ]

        if stale_ids:
            await self._store.vector_delete(CHUNK_COLLECTION, [str(value) for value in stale_ids])
        if records:
            await self._store.vector_upsert(CHUNK_COLLECTION, records)

        logger.debug(
            "knowledge.chunks_written",
            documents=len(documents),
            chunks=len(written),
            vectors=len(records),
        )
        return len(written)

    async def _embed_batched(self, texts: Sequence[str]) -> list[list[float]] | None:
        """Embed *texts* in bounded batches, preserving order.

        Args:
            texts: Every chunk of every changed document, in order.

        Returns:
            One vector per input, or ``None`` when embedding is unavailable or returned the
            wrong arity — in which case chunks are stored unembedded for the backlog worker
            rather than the whole pass being lost.
        """
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            window = list(texts[start : start + EMBEDDING_BATCH_SIZE])
            batch = await self._store.embed(window)
            if batch is None:
                return None
            if len(batch) != len(window):
                logger.warning(
                    "knowledge.embedding_arity_mismatch",
                    expected=len(window),
                    received=len(batch),
                )
                return None
            vectors.extend(list(vector) for vector in batch)
        return vectors

    # ----------------------------------------------------------------------------------
    # Step 7 — facts and graph
    # ----------------------------------------------------------------------------------

    async def _merge_facts(
        self,
        user_id: uuid.UUID,
        facts: Sequence[ExtractedFact],
        documents: dict[str, KnowledgeDocument],
    ) -> int:
        """Merge extracted claims into the fact store, grouped by their provenance.

        Facts are grouped by :attr:`~app.knowledge.analyzers.base.ExtractedFact.source_uri`
        so that each group can be written with the right ``source_document_id``. Provenance
        is not decoration here — golden rule #7 is checkable only because every fact points
        at the document it was read from.

        Args:
            user_id: Owning user.
            facts: The analyzer's claims.
            documents: The source's documents, keyed by uri.

        Returns:
            How many claims reached the store, inserted or merged.
        """
        if not facts:
            return 0

        groups: dict[str | None, list[ExtractedFact]] = {}
        for fact in facts:
            uri = fact.source_uri if fact.source_uri in documents else None
            groups.setdefault(uri, []).append(fact)

        total = 0
        for uri, group in groups.items():
            document_id = documents[uri].id if uri is not None else None
            total += await self._facts.upsert_many(user_id, group, source_document_id=document_id)
        return total

    async def _merge_graph(
        self, user_id: uuid.UUID, result: AnalysisResult, report: IndexReport
    ) -> tuple[int, int]:
        """Upsert every entity, then every edge, recording per-item failures.

        Entities are written before edges on purpose: edge endpoints are resolved by name,
        and resolving one that has not been upserted yet creates a low-confidence stub node
        instead of the fully-described entity the analyzer actually produced.

        A single malformed item — a name that normalises to nothing — is recorded on the
        report and skipped. Losing an entire index run to one punctuation-only string would
        be a poor trade.

        Args:
            user_id: Owning user.
            result: The analysis to persist.
            report: The report collecting non-fatal problems.

        Returns:
            ``(entities, edges)`` upserted.
        """
        entities = 0
        for entity in result.entities:
            try:
                await self._graph.upsert_entity(user_id, entity)
            except ValueError as exc:
                report.record_error(f"entity {entity.name!r} skipped: {exc}")
                continue
            entities += 1

        edges = 0
        for edge in result.edges:
            try:
                await self._graph.upsert_edge(user_id, edge)
            except ValueError as exc:
                report.record_error(f"edge {edge.relation.value!r} skipped: {exc}")
                continue
            edges += 1

        return entities, edges

    # ----------------------------------------------------------------------------------
    # Step 8 — publication, and the failure path
    # ----------------------------------------------------------------------------------

    async def _finalize(self, source: KnowledgeSource, *, fingerprint: str | None) -> None:
        """Mark a source indexed and store the fingerprint the next probe compares against.

        Args:
            source: The source just indexed.
            fingerprint: The analysis fingerprint. An empty value stores ``None``, which no
                probe can equal, so an analyzer that declares no fingerprint is re-analyzed
                every pass — see the module docstring.
        """
        source.index_status = IndexStatus.INDEXED
        source.last_indexed_at = utcnow()
        source.content_hash = (fingerprint or "")[:CONTENT_HASH_LENGTH] or None
        source.last_error = None
        await self.session.commit()

    async def _fail(self, source_id: uuid.UUID, message: str) -> None:
        """Record a failed pass, defensively enough to work after almost anything.

        The session may be in a failed transaction (a flush blew up) or holding expired
        instances (something rolled back), so this rolls back first and then writes with a
        Core ``UPDATE`` rather than through the ORM identity map. The whole thing is wrapped
        because the only outcome worse than a failed index is a failed index that also left
        the source stuck in ``indexing``, invisible to every refresh query.

        Args:
            source_id: The source to mark failed.
            message: The error to store on ``last_error``.
        """
        try:
            await self.session.rollback()
            await self.session.execute(
                update(KnowledgeSource)
                .where(KnowledgeSource.id == source_id)
                .values(
                    index_status=IndexStatus.FAILED,
                    last_error=message[:MAX_ERROR_CHARS],
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            await self.session.commit()
        except asyncio.CancelledError:
            logger.error(
                "knowledge.index_status_not_recorded",
                source_id=str(source_id),
                detail="cancelled while marking the source failed",
            )
        except Exception as exc:
            logger.error(
                "knowledge.index_status_not_recorded",
                source_id=str(source_id),
                error=str(exc),
            )

    # ----------------------------------------------------------------------------------
    # Batches
    # ----------------------------------------------------------------------------------

    async def index_all(self, user_id: uuid.UUID, *, force: bool = False) -> list[IndexReport]:
        """Index every enabled source the user has, a bounded number at a time.

        Args:
            user_id: Owning user.
            force: Passed through to :meth:`index_source`.

        Returns:
            One report per source, in registration order. A source that failed contributes a
            report with :attr:`IndexReport.failed` set — **one failure never aborts the
            rest**, which is the whole reason each source commits independently.
        """
        statement = (
            select(KnowledgeSource.id)
            .where(
                KnowledgeSource.user_id == user_id,
                KnowledgeSource.enabled.is_(True),
            )
            .order_by(KnowledgeSource.created_at.asc(), KnowledgeSource.id.asc())
        )
        source_ids = list((await self.session.execute(statement)).scalars().all())
        return await self._run_batch(source_ids, force=force, reason="index_all")

    async def refresh_stale(self, user_id: uuid.UUID) -> list[IndexReport]:
        """Re-index the user's sources that are due, honouring the auto-index settings.

        A source is due when any of these holds:

        * it has never been indexed;
        * its status is ``pending``, ``stale`` or ``failed``;
        * its last successful index is older than
          ``settings.knowledge_reindex_interval_minutes``;
        * it has been stuck in ``indexing`` for
          :data:`STALE_INDEXING_GRACE_MULTIPLIER` × that interval, which means the process
          that claimed it died.

        Sources with ``auto_refresh=False`` are never touched, and the whole method is a
        no-op when ``settings.knowledge_autoindex`` is off — that switch is how a user says
        "index only when I ask", and a background pass that ignored it would be a breach of
        that.

        Args:
            user_id: Owning user.

        Returns:
            One report per refreshed source; ``[]`` when nothing was due.
        """
        if not self.settings.knowledge_autoindex:
            logger.debug("knowledge.refresh_disabled", user_id=str(user_id))
            return []

        now = utcnow()
        interval = timedelta(minutes=max(1, int(self.settings.knowledge_reindex_interval_minutes)))
        due_before = now - interval
        abandoned_before = now - interval * STALE_INDEXING_GRACE_MULTIPLIER

        statement = (
            select(KnowledgeSource.id)
            .where(
                KnowledgeSource.user_id == user_id,
                KnowledgeSource.enabled.is_(True),
                KnowledgeSource.auto_refresh.is_(True),
                or_(
                    KnowledgeSource.last_indexed_at.is_(None),
                    KnowledgeSource.last_indexed_at < due_before,
                    KnowledgeSource.index_status.in_(
                        [IndexStatus.PENDING, IndexStatus.STALE, IndexStatus.FAILED]
                    ),
                    (KnowledgeSource.index_status == IndexStatus.INDEXING)
                    & (KnowledgeSource.updated_at < abandoned_before),
                ),
            )
            .order_by(KnowledgeSource.created_at.asc(), KnowledgeSource.id.asc())
        )
        source_ids = list((await self.session.execute(statement)).scalars().all())
        return await self._run_batch(source_ids, force=False, reason="refresh_stale")

    async def _run_batch(
        self, source_ids: Sequence[uuid.UUID], *, force: bool, reason: str
    ) -> list[IndexReport]:
        """Index *source_ids* concurrently, each on its own session.

        Every source gets a fresh session because an
        :class:`~sqlalchemy.ext.asyncio.AsyncSession` is not safe to share between
        concurrent tasks, and because each pass has to commit on its own — that independence
        is what makes "one failure never aborts the rest" true rather than aspirational.

        Args:
            source_ids: The sources to index, in the order results should come back.
            force: Passed through to :meth:`index_source`.
            reason: Log label identifying which batch entry point ran.

        Returns:
            One report per id, in the order given.
        """
        if not source_ids:
            return []

        limit = self._concurrency()
        semaphore = asyncio.Semaphore(limit)
        started = time.perf_counter()

        async def run(source_id: uuid.UUID) -> IndexReport:
            """Index one source under the concurrency limit, on its own session."""
            from app.database.session import session_scope

            async with semaphore:
                try:
                    async with session_scope() as session:
                        return await self._clone_for(session).index_source(source_id, force=force)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "knowledge.index_source_unhandled",
                        source_id=str(source_id),
                        error=str(exc),
                        exc_info=True,
                    )
                    report = IndexReport(source_id=source_id, failed=True)
                    report.record_error(f"{type(exc).__name__}: {exc}")
                    return report

        reports = await asyncio.gather(*(run(source_id) for source_id in source_ids))
        logger.info(
            "knowledge.batch_indexed",
            reason=reason,
            sources=len(reports),
            failed=sum(1 for report in reports if report.failed),
            skipped=sum(1 for report in reports if report.skipped),
            concurrency=limit,
            duration_seconds=round(time.perf_counter() - started, 4),
        )
        return list(reports)

    def _concurrency(self) -> int:
        """Return how many sources may be indexed at once on this installation.

        Returns:
            :data:`SQLITE_INDEX_CONCURRENCY` when the bound database is SQLite — which
            permits one writer at a time — and :data:`DEFAULT_INDEX_CONCURRENCY` otherwise.
        """
        try:
            dialect = self.session.get_bind().dialect.name
        except Exception as exc:
            logger.debug("knowledge.dialect_unknown", error=str(exc))
            return SQLITE_INDEX_CONCURRENCY
        return SQLITE_INDEX_CONCURRENCY if dialect == "sqlite" else DEFAULT_INDEX_CONCURRENCY
