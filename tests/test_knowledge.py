"""The knowledge engine's indexing pipeline (``docs/CONTRACTS.md`` §8).

The engine's whole economic argument is step 3 of ``index_source``: a **cheap fingerprint
probe** that lets an unchanged source skip analysis, chunking and embedding entirely. That is
what makes "continuously re-index everything the user has ever written" affordable rather
than a per-hour API bill. So the central test is the three-call sequence:

    index → index again (skipped) → index with ``force=True``

and the assertion that matters across all three is **no doubling**. Re-indexing must converge
on the same rows, not accumulate them: facts dedupe by ``content_hash``, entities upsert by
``normalized_name``, edges upsert on their triple, and chunks are keyed
``UNIQUE(document_id, ordinal)``. A pipeline that appended instead would grow the graph
without bound and progressively poison retrieval with duplicates.

The analyzer is a stub returning a fixed :class:`AnalysisResult`, because what is under test
is the indexer's convergence, not any particular source's parsing. Its ``fingerprint`` is
controllable so the skip path can be entered and left deliberately.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.knowledge.analyzers.base import (
    AnalysisResult,
    ExtractedDocument,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedFact,
    SourceRef,
)
from app.knowledge.indexer import KnowledgeIndexer, SourceNotFoundError
from app.knowledge.vector.memory_store import InMemoryVectorStore
from app.models.enums import (
    EntityKind,
    FactKind,
    IndexStatus,
    RelationKind,
    SourceKind,
)
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeFact,
)

SOURCE_URI = "https://github.com/ada/analytical-engine"


def _analysis(fingerprint: str = "fp-1") -> AnalysisResult:
    """A fixed, realistic analysis result."""
    return AnalysisResult(
        documents=[
            ExtractedDocument(
                uri=SOURCE_URI,
                title="analytical-engine",
                text=(
                    "A Python implementation of the analytical engine. "
                    "Built with FastAPI and PostgreSQL. Handles 12000 requests per second. "
                    "Includes a Redis cache layer and a comprehensive test suite."
                ),
                kind=SourceKind.GITHUB_REPO,
            )
        ],
        facts=[
            ExtractedFact(
                kind=FactKind.ACCOMPLISHMENT,
                text="Built an analytical engine handling 12000 requests per second.",
                skills=["Python"],
                technologies=["Python", "FastAPI"],
                metrics=["12000"],
                organization="Personal",
                confidence=0.8,
            ),
            ExtractedFact(
                kind=FactKind.SKILL_USAGE,
                text="Used Redis for a read-through cache layer.",
                skills=["Redis"],
                technologies=["Redis"],
                confidence=0.7,
            ),
        ],
        entities=[
            ExtractedEntity(kind=EntityKind.TECHNOLOGY, name="Python", confidence=0.9),
            ExtractedEntity(kind=EntityKind.TECHNOLOGY, name="FastAPI", confidence=0.8),
            ExtractedEntity(kind=EntityKind.PROJECT, name="Analytical Engine", confidence=0.9),
        ],
        edges=[
            ExtractedEdge(
                source=(EntityKind.TECHNOLOGY, "Python"),
                target=(EntityKind.PROJECT, "Analytical Engine"),
                relation=RelationKind.USED_IN,
            ),
            ExtractedEdge(
                source=(EntityKind.TECHNOLOGY, "FastAPI"),
                target=(EntityKind.PROJECT, "Analytical Engine"),
                relation=RelationKind.USED_IN,
            ),
        ],
        fingerprint=fingerprint,
    )


class StubAnalyzer:
    """An analyzer whose fingerprint and result the test controls.

    Records how many times it was actually asked to analyze, which is how the skip path is
    proven rather than inferred from timing.
    """

    name = "stub"
    source_kinds = frozenset({SourceKind.GITHUB_REPO})

    def __init__(self, fingerprint: str = "fp-1") -> None:
        self._fingerprint = fingerprint
        self.analyze_calls = 0
        self.fingerprint_calls = 0

    def set_fingerprint(self, value: str) -> None:
        """Simulate the upstream source changing."""
        self._fingerprint = value

    async def analyze(self, source: SourceRef) -> AnalysisResult:
        self.analyze_calls += 1
        return _analysis(self._fingerprint)

    async def fingerprint(self, source: SourceRef) -> str:
        self.fingerprint_calls += 1
        return self._fingerprint

    def supports(self, source: SourceRef) -> bool:
        return True

    async def healthcheck(self) -> bool:
        return True


@pytest.fixture
def analyzer() -> StubAnalyzer:
    """The stub analyzer, shared by the indexer and the assertions."""
    return StubAnalyzer()


@pytest.fixture
def probe_cache():
    """A private cache for the fingerprint-probe memo, so tests cannot leak into each other."""
    from app.cache.memory import MemoryCache

    return MemoryCache()


@pytest.fixture
def indexer(session, settings, analyzer, probe_cache, monkeypatch) -> KnowledgeIndexer:
    """An indexer wired to the stub analyzer, an in-memory vector store and a private cache.

    ``analyzer_for`` is patched in the indexer's own namespace so the plugin registry stays
    untouched — this is a test of the indexer, not of plugin resolution.
    """
    import app.knowledge.indexer as indexer_module
    from app.ai.embeddings import HashingEmbedder

    monkeypatch.setattr(indexer_module, "analyzer_for", lambda _ref: analyzer)

    return KnowledgeIndexer(
        session,
        settings,
        embedder=HashingEmbedder(),
        vector_store=InMemoryVectorStore(),
        cache=probe_cache,
    )


@pytest.fixture
async def source(indexer, user):
    """A registered GitHub source, not yet indexed."""
    return await indexer.add_source(
        user.id,
        SourceRef(kind=SourceKind.GITHUB_REPO, uri=SOURCE_URI, label="analytical-engine"),
    )


async def _counts(session, user_id) -> dict[str, int]:
    """Row counts for everything indexing writes."""

    async def count(model, *where) -> int:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0

    documents = await count(KnowledgeDocument, KnowledgeDocument.user_id == user_id)
    facts = await count(KnowledgeFact, KnowledgeFact.user_id == user_id)
    entities = await count(KnowledgeEntity, KnowledgeEntity.user_id == user_id)
    edges = await count(KnowledgeEdge, KnowledgeEdge.user_id == user_id)
    chunks = await session.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0
    return {
        "documents": documents,
        "facts": facts,
        "entities": entities,
        "edges": edges,
        "chunks": chunks,
    }


# ======================================================================================
# The first index
# ======================================================================================


async def test_a_first_index_writes_everything(indexer, session, source, user) -> None:
    """The baseline the convergence tests are measured against."""
    report = await indexer.index_source(source.id)

    assert report.skipped is False
    assert report.documents == 1
    assert report.facts >= 1
    assert report.entities >= 1
    assert report.errors == []

    counts = await _counts(session, user.id)
    assert counts["documents"] == 1
    assert counts["facts"] == 2
    assert counts["entities"] == 3
    assert counts["edges"] == 2
    assert counts["chunks"] >= 1


async def test_the_source_is_marked_indexed(indexer, session, source) -> None:
    """A completed index leaves the source in ``indexed`` with a stored fingerprint."""
    await indexer.index_source(source.id)
    await session.refresh(source)

    assert source.index_status is IndexStatus.INDEXED
    assert source.content_hash
    assert source.last_indexed_at is not None
    assert source.last_error is None


async def test_chunks_are_embedded(indexer, session, source) -> None:
    """Retrieval is vector-first; unembedded chunks are invisible to it."""
    await indexer.index_source(source.id)

    chunks = (await session.execute(select(KnowledgeChunk))).scalars().all()
    assert chunks
    assert all(chunk.embedding is not None for chunk in chunks)


# ======================================================================================
# The skip path
# ======================================================================================


async def test_an_unchanged_source_is_skipped(indexer, source, analyzer) -> None:
    """**The economic argument.** An unchanged source costs one fingerprint probe."""
    await indexer.index_source(source.id)
    assert analyzer.analyze_calls == 1

    report = await indexer.index_source(source.id)

    assert report.skipped is True
    assert analyzer.analyze_calls == 1, "an unchanged source was re-analyzed"


async def test_a_skipped_index_writes_nothing_new(indexer, session, source, user) -> None:
    """Skipping must be a true no-op, not a cheaper way of writing the same rows again."""
    await indexer.index_source(source.id)
    before = await _counts(session, user.id)

    await indexer.index_source(source.id)
    after = await _counts(session, user.id)

    assert after == before


async def test_a_changed_fingerprint_re_analyzes(indexer, source, analyzer, probe_cache) -> None:
    """The other half: a source that really changed must not be skipped.

    The probe memo is cleared first to stand in for its TTL expiring. That memo is a
    deliberate optimisation for the burst of reindex requests a desktop launch produces —
    it is far shorter than any realistic edit-then-reindex cycle — so simulating its
    expiry is the honest way to test the path underneath it.
    """
    await indexer.index_source(source.id)
    analyzer.set_fingerprint("fp-2")
    await probe_cache.clear()

    report = await indexer.index_source(source.id)

    assert report.skipped is False
    assert analyzer.analyze_calls == 2


async def test_the_probe_memo_absorbs_a_burst_of_reindex_requests(
    indexer, source, analyzer
) -> None:
    """Three reindexes in a row cost one upstream probe, which is what the memo is for."""
    await indexer.index_source(source.id)
    probes_after_first = analyzer.fingerprint_calls

    await indexer.index_source(source.id)
    await indexer.index_source(source.id)

    assert analyzer.fingerprint_calls == probes_after_first


# ======================================================================================
# Force, and the no-doubling guarantee
# ======================================================================================


async def test_force_re_analyzes_but_does_not_double(
    indexer, session, source, user, analyzer
) -> None:
    """**The test this file exists for.**

    ``force=True`` bypasses the fingerprint memo and runs the whole pipeline again. Every
    row count must be *identical* afterwards: facts dedupe on ``content_hash``, entities
    upsert on ``normalized_name``, edges upsert on their triple, chunks on
    ``(document_id, ordinal)``. A pipeline that appended would double the graph on every
    scheduled refresh.
    """
    await indexer.index_source(source.id)
    before = await _counts(session, user.id)

    report = await indexer.index_source(source.id, force=True)

    assert report.skipped is False
    assert analyzer.analyze_calls == 2, "force did not re-analyze"

    after = await _counts(session, user.id)
    assert after == before, f"re-indexing doubled rows: {before} -> {after}"


async def test_repeated_forced_indexing_converges(indexer, session, source, user) -> None:
    """Five forced passes, still the same counts. Drift shows up here if anywhere does."""
    await indexer.index_source(source.id)
    baseline = await _counts(session, user.id)

    for _ in range(5):
        await indexer.index_source(source.id, force=True)

    assert await _counts(session, user.id) == baseline


async def test_facts_dedupe_by_content_hash(indexer, session, source, user) -> None:
    """The same fact seen twice is one row, and it keeps one content hash."""
    await indexer.index_source(source.id)
    await indexer.index_source(source.id, force=True)

    hashes = (
        (
            await session.execute(
                select(KnowledgeFact.content_hash).where(KnowledgeFact.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(hashes) == len(set(hashes)), "duplicate facts survived the merge"


async def test_entities_upsert_by_normalized_name(indexer, session, source, user) -> None:
    """One technology, one entity — however many sources mention it."""
    await indexer.index_source(source.id)
    await indexer.index_source(source.id, force=True)

    names = (
        (
            await session.execute(
                select(KnowledgeEntity.normalized_name).where(KnowledgeEntity.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(names) == len(set(names)), "duplicate entities survived the upsert"


async def test_edges_do_not_multiply(indexer, session, source, user) -> None:
    """``UNIQUE(source, target, relation)`` is what keeps graph traversal finite."""
    await indexer.index_source(source.id)
    await indexer.index_source(source.id, force=True)

    edges = (
        (await session.execute(select(KnowledgeEdge).where(KnowledgeEdge.user_id == user.id)))
        .scalars()
        .all()
    )
    triples = {(e.source_entity_id, e.target_entity_id, e.relation) for e in edges}
    assert len(triples) == len(edges)


async def test_chunk_ordinals_are_not_duplicated(indexer, session, source) -> None:
    """Re-chunking replaces rather than appends, or a document reads twice over."""
    await indexer.index_source(source.id)
    await indexer.index_source(source.id, force=True)

    pairs = [
        (chunk.document_id, chunk.ordinal)
        for chunk in (await session.execute(select(KnowledgeChunk))).scalars().all()
    ]
    assert len(pairs) == len(set(pairs))


# ======================================================================================
# Failure handling
# ======================================================================================


async def test_an_analyzer_failure_marks_the_source_and_does_not_raise(
    indexer, session, source, analyzer, monkeypatch
) -> None:
    """One unreachable repository must not abort a batch of twenty."""

    async def _explode(_ref):
        raise RuntimeError("github is down")

    monkeypatch.setattr(analyzer, "analyze", _explode)

    report = await indexer.index_source(source.id)

    assert report.errors
    assert report.ok is False
    await session.refresh(source)
    assert source.index_status is IndexStatus.FAILED
    assert "github is down" in (source.last_error or "")


async def test_an_unknown_source_raises(indexer) -> None:
    """A missing source is a caller error, not an indexing failure."""
    import uuid

    with pytest.raises(SourceNotFoundError):
        await indexer.index_source(uuid.uuid4())


async def test_adding_the_same_source_twice_returns_one_row(indexer, user) -> None:
    """``UNIQUE(user_id, kind, uri)`` — adding a GitHub profile twice is one source."""
    ref = SourceRef(kind=SourceKind.GITHUB_REPO, uri=SOURCE_URI)
    first = await indexer.add_source(user.id, ref)
    second = await indexer.add_source(user.id, ref)
    assert first.id == second.id


async def test_removing_a_source_cascades(indexer, session, source, user) -> None:
    """Deleting a source removes its documents and chunks; facts outlive it (SET NULL)."""
    await indexer.index_source(source.id)
    assert (await _counts(session, user.id))["documents"] == 1

    await indexer.remove_source(source.id)

    counts = await _counts(session, user.id)
    assert counts["documents"] == 0
    assert counts["chunks"] == 0
