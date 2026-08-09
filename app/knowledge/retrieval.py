"""Hybrid retrieval — turning a question into the knowledge that answers it.

This is the read path of the knowledge engine and the module Phase 4's resume engine sits
directly on top of: :meth:`KnowledgeRetriever.retrieve_for_posting` is what a tailored
resume is generated from, and golden rule #7 (nothing is fabricated) holds only because the
generator is handed *these* rows and nothing else.

Four independent signals are combined, because each one fails in a way the others do not:

**Vector similarity over facts.** Finds claims that *mean* what the query means without
sharing a word — the case that decides this product, because a job description says
"real-time embedded control" and the fact says "tuned a 1 kHz PID loop on an STM32".

**Keyword matching over fact text.** Finds the exact tokens an embedder smooths away: a
product name, an acronym, an employer, a course code. Tokenised with
:func:`~app.ai.embeddings.content_tokens`, so English function words are dropped and
technical identifiers (``c++``, ``node.js``, ``ci/cd``) survive as single tokens.

**Vector similarity over document chunks.** Facts are atomic and short; chunks carry the
surrounding prose that explains *how* something was done. A README paragraph is often the
best evidence for a claim the fact extractor reduced to one line.

**The graph.** One hop out from the entities the query names surfaces work the query never
mentioned — "ROS 2" reaches the quadruped project, which reaches the facts about it — and
those facts are boosted rather than inserted, so a graph link strengthens evidence instead
of manufacturing it.

Their scores are not comparable numbers (a cosine, a keyword count, a hop distance), so
they are fused by **reciprocal rank fusion**: each ranking contributes ``1 / (k + rank)`` to
every id it contains, per :func:`reciprocal_rank_fusion`. Fusing *ranks* rather than scores
is what makes the combination well-defined without calibrating anything.

Finally :class:`~app.knowledge.memory.MemoryStore` contributes what the user already taught
the system — a phrasing they rejected, a company that ghosted them — so a later generation
does not repeat a mistake the user has already corrected.

Everything degrades. With no embedder and no vector store, the keyword arm answers alone,
graph expansion still works (it is pure SQL), memories fall back to their own keyword path,
and the product keeps working offline with zero API keys.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar

import structlog
from sqlalchemy import func, or_, select

from app.ai.embeddings import content_tokens
from app.knowledge.facts import FACT_COLLECTION, FactStore
from app.knowledge.graph import KnowledgeGraph, KnowledgeStore
from app.knowledge.indexer import CHUNK_COLLECTION
from app.knowledge.memory import DEFAULT_MEMORY_K, MemoryStore
from app.models.enums import FactKind
from app.models.knowledge import (
    KnowledgeEntity,
    KnowledgeFact,
    MemoryEntry,
    normalize_for_matching,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ai.embeddings import Embedder
    from app.knowledge.vector.base import VectorHit, VectorStore

__all__ = [
    "CANDIDATE_OVERFETCH",
    "GRAPH_LINK_BOOST",
    "KEYWORD_OVERFETCH",
    "MAX_EXPANDED_ENTITIES",
    "MAX_KEYWORDS",
    "POSTING_BODY_CHARS",
    "POSTING_CHUNK_K",
    "REASON_CHUNK_VECTOR",
    "REASON_GRAPH",
    "REASON_KEYWORD",
    "REASON_MEMORY",
    "REASON_VECTOR",
    "RRF_K",
    "SEED_ENTITY_LIMIT",
    "VECTOR_OVERFETCH",
    "KnowledgeRetriever",
    "RetrievalResult",
    "reciprocal_rank_fusion",
]

logger = structlog.get_logger(__name__)

#: Key type accepted by :func:`reciprocal_rank_fusion`. Any hashable identifier works —
#: fact ids here, but the function is deliberately not tied to them.
_Key = TypeVar("_Key")


# ======================================================================================
# Constants
# ======================================================================================

#: Reciprocal-rank-fusion constant. 60 is the value from Cormack et al. and the de-facto
#: standard: large enough that the top few ranks of each arm count as roughly comparable,
#: small enough that rank still matters. Matches
#: :data:`app.knowledge.facts.RRF_K` so the two fusions behave identically.
RRF_K: Final[int] = 60

#: Extra candidates each arm fetches before fusion. Truncating both arms at *k* and then
#: fusing loses the fact that ranks 41st in one arm and 3rd in the other — which is exactly
#: the case hybrid retrieval exists to catch.
VECTOR_OVERFETCH: Final[int] = 3
KEYWORD_OVERFETCH: Final[int] = 3

#: How many fused candidates are loaded from the database before graph boosting and final
#: ranking. Boosting can promote a candidate from well outside the top *k*, so the window has
#: to be wider than the result.
CANDIDATE_OVERFETCH: Final[int] = 3

#: Score added to a fact the graph expansion linked to the query.
#:
#: Expressed in the same currency as a fusion vote: a first-place appearance in one arm is
#: worth ``1 / (RRF_K + 1)``, so half of ``1 / RRF_K`` makes a graph link worth roughly half
#: an extra arm agreeing. Large enough to lift a fact that no arm ranked highly but the
#: graph says is about the right project; far too small to outrank a fact both arms put
#: first. The graph corroborates evidence — it never manufactures it.
GRAPH_LINK_BOOST: Final[float] = 0.5 / RRF_K

#: Entities used as the starting points of graph expansion, highest ``mention_count`` first.
#: More than a handful and the one-hop frontier becomes most of the graph, which boosts
#: everything and therefore ranks nothing.
SEED_ENTITY_LIMIT: Final[int] = 8

#: Hard cap on the entities one retrieval may return, seeds plus neighbours.
MAX_EXPANDED_ENTITIES: Final[int] = 40

#: Most keywords taken from a query. Past this the ``OR`` chain stops discriminating and
#: starts matching every fact the user has.
MAX_KEYWORDS: Final[int] = 12

#: Shortest linked entity name allowed to match inside a fact's text. Two-character names
#: ("ML", "AI") match inside unrelated words often enough to make the boost noise; they are
#: still matched exactly against a fact's ``skills`` and ``technologies`` lists, where the
#: comparison is whole-value and safe.
MIN_LINK_NAME_CHARS: Final[int] = 3

#: Characters of a job posting's body folded into the retrieval query by
#: :meth:`KnowledgeRetriever.retrieve_for_posting`. Enough to carry the responsibilities and
#: requirements sections; short enough that the boilerplate footer every posting ends with
#: does not dominate the embedding.
POSTING_BODY_CHARS: Final[int] = 4000

#: Chunks retrieved alongside the facts for a posting. Chunks are supporting evidence for
#: the resume engine's rewriting step, not the material it selects from, so a handful is
#: enough and a longer list only costs prompt budget.
POSTING_CHUNK_K: Final[int] = 8

#: Longest title line lifted from a posting body.
_MAX_TITLE_CHARS: Final[int] = 200

# -- reason labels ---------------------------------------------------------------------
#
# Recorded per item on :attr:`RetrievalResult.reasons` and rendered by
# :meth:`KnowledgeRetriever.explain`. Constants because they are read by tests and by the
# desktop app's "why am I seeing this?" affordance.

REASON_VECTOR: Final[str] = "semantic match"
REASON_KEYWORD: Final[str] = "keyword match"
REASON_CHUNK_VECTOR: Final[str] = "semantic match in document text"
REASON_GRAPH: Final[str] = "linked in the knowledge graph"
REASON_MEMORY: Final[str] = "remembered from earlier feedback"

#: Item-kind prefixes used to key :attr:`RetrievalResult.reasons` and
#: :attr:`RetrievalResult.scores`, matching
#: :data:`app.schemas.knowledge.KnowledgeSearchKind`. Typed as literals rather than ``str``
#: because that is the whole point of them: they are handed straight to
#: :class:`~app.schemas.knowledge.KnowledgeSearchResult`, whose ``kind`` accepts exactly
#: these four values, so a typo becomes an error here instead of a 500 at serialisation.
ITEM_FACT: Final[Literal["fact"]] = "fact"
ITEM_CHUNK: Final[Literal["chunk"]] = "chunk"
ITEM_ENTITY: Final[Literal["entity"]] = "entity"
ITEM_MEMORY: Final[Literal["memory"]] = "memory"

#: ``LIKE`` escape character and the characters that need escaping under it.
_LIKE_ESCAPE: Final[str] = "\\"
_LIKE_SPECIALS: Final[tuple[str, ...]] = (_LIKE_ESCAPE, "%", "_")


# ======================================================================================
# Fusion
# ======================================================================================


def reciprocal_rank_fusion(rankings: Sequence[Sequence[_Key]], k: int = RRF_K) -> dict[_Key, float]:
    """Fuse several ranked lists into one score per identifier.

    Each list contributes ``1 / (k + rank)`` to every id it contains, where ``rank`` is
    one-based, and the contributions are summed::

        score(id) = Σ  1 / (k + rank_i(id))
                    i

    The method needs no score calibration between arms, which is precisely why it is used
    here: a cosine similarity and a keyword overlap count are not comparable numbers, but
    their *rankings* are. The constant *k* damps the head of each list so that being first
    in one arm does not automatically beat being third in two.

    This is the scoring form. :func:`app.knowledge.facts.reciprocal_rank_fusion` is the
    ordering-only form used inside the fact store, which needs a sorted list and nothing
    else; the retriever needs the scores themselves so that graph expansion can add to them
    and :meth:`KnowledgeRetriever.explain` can report them.

    Args:
        rankings: The ranked lists, best-first. Empty lists contribute nothing. An id may
            appear in any number of lists but should appear at most once per list; a repeat
            simply adds its (weaker) contribution again.
        k: The fusion constant; see :data:`RRF_K`. Must be positive.

    Returns:
        Every id seen, mapped to its fused score. Higher is better.

    Raises:
        ValueError: If *k* is not positive, which would divide by zero at rank ``-k``.
    """
    if k <= 0:
        raise ValueError(f"the RRF constant must be positive, got {k}")
    scores: dict[_Key, float] = {}
    for ranking in rankings:
        for rank, identifier in enumerate(ranking, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (k + rank)
    return scores


def _like_pattern(term: str) -> str:
    """Return a ``LIKE`` pattern matching *term* anywhere, with wildcards escaped.

    Args:
        term: A query keyword, which may legitimately contain ``%`` or ``_`` — ``ci_cd``
              and ``100%`` both occur in real technical text.

    Returns:
        The pattern, to be used with ``escape="\\\\"``.
    """
    escaped = term
    for special in _LIKE_SPECIALS:
        escaped = escaped.replace(special, f"{_LIKE_ESCAPE}{special}")
    return f"%{escaped}%"


def _keywords(query: str) -> list[str]:
    """Return the distinct content keywords of *query*, capped at :data:`MAX_KEYWORDS`.

    Args:
        query: The raw query text.

    Returns:
        Case-folded keywords in first-appearance order, stopwords already removed by
        :func:`~app.ai.embeddings.content_tokens`.
    """
    seen: set[str] = set()
    keywords: list[str] = []
    for token in content_tokens(query or ""):
        if token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= MAX_KEYWORDS:
            break
    return keywords


# ======================================================================================
# Result
# ======================================================================================


@dataclass(slots=True)
class RetrievalResult:
    """Everything one retrieval found, per ``docs/CONTRACTS.md`` §8.3.

    Attributes:
        facts: Atomic claims, most relevant first. **The only material a resume may be
            generated from.**
        chunks: Document passages, as vector hits — their ``metadata`` carries the document
            title and uri, so a caller can cite them without a database round trip.
        entities: Graph nodes the query named, plus their one-hop neighbours.
        memories: What the user already taught the system about work like this.
        query: The query these results answer. Appended after the contract's fields so that
            :meth:`KnowledgeRetriever.explain` can render a self-contained report.
        scores: Fused relevance per item, keyed ``"<kind>:<id>"``.
        reasons: Why each item was retrieved, keyed the same way. This is what makes the
            retrieval auditable rather than a black box.
    """

    facts: list[KnowledgeFact] = field(default_factory=list)
    chunks: list[VectorHit] = field(default_factory=list)
    entities: list[KnowledgeEntity] = field(default_factory=list)
    memories: list[MemoryEntry] = field(default_factory=list)
    query: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Whether nothing at all was retrieved."""
        return not (self.facts or self.chunks or self.entities or self.memories)

    def counts(self) -> dict[str, int]:
        """Return the size of each collection, for logging.

        Returns:
            A mapping with the keys ``facts``, ``chunks``, ``entities`` and ``memories``.
        """
        return {
            "facts": len(self.facts),
            "chunks": len(self.chunks),
            "entities": len(self.entities),
            "memories": len(self.memories),
        }

    def fact_ids(self) -> list[uuid.UUID]:
        """Return the retrieved fact ids, in rank order.

        The resume engine keeps this set and drops any id the model returns that is not in
        it (``resume_engine.hallucinated_fact``), which is how golden rule #7 is enforced.
        """
        return [fact.id for fact in self.facts]

    def score_of(self, kind: str, identifier: Any) -> float:
        """Return one item's fused score.

        Args:
            kind: One of ``fact``, ``chunk``, ``entity``, ``memory``.
            identifier: The item's id.

        Returns:
            The score, or ``0.0`` when the item carries none.
        """
        return self.scores.get(_item_key(kind, identifier), 0.0)

    def reasons_for(self, kind: str, identifier: Any) -> list[str]:
        """Return why one item was retrieved.

        Args:
            kind: One of ``fact``, ``chunk``, ``entity``, ``memory``.
            identifier: The item's id.

        Returns:
            The recorded reasons, or ``[]``.
        """
        return list(self.reasons.get(_item_key(kind, identifier), ()))

    def note(self, kind: str, identifier: Any, reason: str) -> None:
        """Record why an item was retrieved, without duplicating a reason.

        Args:
            kind: One of ``fact``, ``chunk``, ``entity``, ``memory``.
            identifier: The item's id.
            reason: One of the ``REASON_*`` constants.
        """
        bucket = self.reasons.setdefault(_item_key(kind, identifier), [])
        if reason not in bucket:
            bucket.append(reason)


def _item_key(kind: str, identifier: Any) -> str:
    """Return the ``"<kind>:<id>"`` key used by :attr:`RetrievalResult.scores`.

    Args:
        kind: The item kind.
        identifier: The item's id.

    Returns:
        The composite key.
    """
    return f"{kind}:{identifier}"


# ======================================================================================
# The retriever
# ======================================================================================


class KnowledgeRetriever:
    """Answers "what does this user know that is relevant here?" (§8.3).

    Scoped to one ``user_id`` on every method. Read-only: nothing here writes, so it is safe
    to run inside a request handler on a session that will never commit.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        """Bind the retriever to a session and its collaborators.

        Args:
            session: The async session every query runs on.
            embedder: Embedder override; resolved from settings on first use when omitted.
            vector_store: Vector index override; resolved from settings on first use when
                omitted.
        """
        self.session = session
        self._store = KnowledgeStore(session, embedder=embedder, vector_store=vector_store)
        self._facts = FactStore(session, embedder=embedder, vector_store=vector_store)
        self._graph = KnowledgeGraph(session, embedder=embedder, vector_store=vector_store)
        self._memory = MemoryStore(session, embedder=embedder, vector_store=vector_store)

    # ----------------------------------------------------------------------------------
    # Collaborators
    # ----------------------------------------------------------------------------------

    @property
    def facts(self) -> FactStore:
        """The fact store this retriever reads through."""
        return self._facts

    @property
    def graph(self) -> KnowledgeGraph:
        """The graph store used for one-hop expansion."""
        return self._graph

    @property
    def memory(self) -> MemoryStore:
        """The memory store whose entries are merged into every result."""
        return self._memory

    # ----------------------------------------------------------------------------------
    # Retrieval
    # ----------------------------------------------------------------------------------

    async def retrieve(
        self,
        user_id: uuid.UUID,
        query: str,
        *,
        k_facts: int = 40,
        k_chunks: int = 12,
        kinds: Sequence[FactKind] | None = None,
        expand_graph: bool = True,
    ) -> RetrievalResult:
        """Return the knowledge most relevant to *query*.

        The query is embedded **once** (through :func:`app.ai.embeddings.embed_texts`, so a
        repeated query is a cache hit), the fact and chunk vector searches run concurrently,
        and the keyword arm runs against the database. The two fact rankings are fused by
        :func:`reciprocal_rank_fusion`; graph expansion then boosts the facts that the
        user's own graph links to the query, by :data:`GRAPH_LINK_BOOST`.

        Args:
            user_id: Owning user. Enforced again on every row after retrieval — one
                person's facts appearing in another person's resume is the worst failure
                this system could have, so the check is not left to the vector index alone.
            query: Free text: a job description, a section heading, a question.
            k_facts: Maximum facts to return.
            k_chunks: Maximum document chunks to return.
            kinds: Restrict the **fact** arm to these
                :class:`~app.models.enums.FactKind` values, matching
                :meth:`app.knowledge.facts.FactStore.search`. Chunks, entities and memories
                are unaffected — they have no fact kind.
            expand_graph: Whether to pull one-hop neighbours of the entities the query names
                and boost the facts they link to.

        Returns:
            A populated :class:`RetrievalResult`. An empty query returns an empty result
            rather than the user's entire knowledge base.
        """
        started = time.perf_counter()
        result = RetrievalResult(query=query or "")
        text = (query or "").strip()
        if not text or k_facts < 1:
            return result

        resolved_kinds = [FactKind(kind) for kind in kinds] if kinds else None
        embedded = await self._store.embed([text])
        vector = list(embedded[0]) if embedded else None

        vector_fact_ids, chunk_hits = await asyncio.gather(
            self._vector_fact_ids(user_id, vector, k=k_facts, kinds=resolved_kinds),
            self._vector_chunks(user_id, vector, k=k_chunks),
        )
        keyword_fact_ids = await self._keyword_fact_ids(
            user_id, text, k=k_facts, kinds=resolved_kinds
        )

        scores = reciprocal_rank_fusion([vector_fact_ids, keyword_fact_ids])
        rows = await self._load_candidates(
            user_id, scores, limit=k_facts * CANDIDATE_OVERFETCH, kinds=resolved_kinds
        )

        for identifier in vector_fact_ids:
            result.note(ITEM_FACT, identifier, REASON_VECTOR)
        for identifier in keyword_fact_ids:
            result.note(ITEM_FACT, identifier, REASON_KEYWORD)

        entities: list[KnowledgeEntity] = []
        if expand_graph:
            entities = await self._expand_graph(user_id, text, rows)
            boosted = self._boost_linked(rows, entities, scores)
            for identifier in boosted:
                result.note(ITEM_FACT, identifier, REASON_GRAPH)
            for entity in entities:
                result.note(ITEM_ENTITY, entity.id, REASON_GRAPH)
                result.scores[_item_key(ITEM_ENTITY, entity.id)] = float(entity.confidence or 0.0)

        rows.sort(
            key=lambda row: (
                -scores.get(row.id, 0.0),
                -int(row.impact_score or 0),
                -float(row.confidence or 0.0),
                str(row.id),
            )
        )
        result.facts = rows[:k_facts]
        for row in result.facts:
            result.scores[_item_key(ITEM_FACT, row.id)] = scores.get(row.id, 0.0)

        result.chunks = chunk_hits[: max(0, k_chunks)]
        for hit in result.chunks:
            result.note(ITEM_CHUNK, hit.id, REASON_CHUNK_VECTOR)
            result.scores[_item_key(ITEM_CHUNK, hit.id)] = float(hit.score)

        result.entities = entities
        result.memories = await self._memory.relevant(user_id, text, k=DEFAULT_MEMORY_K)
        for memory in result.memories:
            result.note(ITEM_MEMORY, memory.id, REASON_MEMORY)
            result.scores[_item_key(ITEM_MEMORY, memory.id)] = float(memory.weight or 0.0)

        logger.debug(
            "knowledge.retrieved",
            user_id=str(user_id),
            characters=len(text),
            vector_hits=len(vector_fact_ids),
            keyword_hits=len(keyword_fact_ids),
            candidates=len(rows),
            duration_seconds=round(time.perf_counter() - started, 4),
            **result.counts(),
        )
        return result

    async def retrieve_for_posting(
        self, user_id: uuid.UUID, posting_text: str, *, k_facts: int = 60
    ) -> RetrievalResult:
        """Retrieve everything relevant to one job posting.

        The entry point Phase 4's resume engine calls. The query is assembled from three
        parts rather than passed as raw text, because a posting is mostly boilerplate and
        embedding all of it buries the signal:

        * the **title** line, which carries the role;
        * the **skills** the posting names, recovered by
          :func:`app.knowledge.extractors.extract_skills` and appended so their canonical
          spellings ("C++", "FreeRTOS") match the fact store's own vocabulary;
        * a bounded prefix of the **body** (:data:`POSTING_BODY_CHARS`), which is where
          responsibilities and requirements live.

        Args:
            user_id: Owning user.
            posting_text: The posting, typically ``title + "\\n" + description``.
            k_facts: Maximum facts to return. Higher than
                :meth:`retrieve`'s default because the resume engine prefilters this set
                again before it selects bullets.

        Returns:
            A populated :class:`RetrievalResult`.
        """
        from app.knowledge.extractors import extract_skills

        text = (posting_text or "").strip()
        if not text:
            return RetrievalResult(query="")

        title = next((line.strip() for line in text.splitlines() if line.strip()), "")[
            :_MAX_TITLE_CHARS
        ]
        skills = extract_skills(text)
        body = " ".join(text.split())[:POSTING_BODY_CHARS]

        parts = [part for part in (title, " ".join(skills), body) if part]
        query = "\n".join(parts)

        logger.debug(
            "knowledge.retrieve_for_posting",
            user_id=str(user_id),
            title=title,
            skills=len(skills),
            characters=len(query),
        )
        return await self.retrieve(user_id, query, k_facts=k_facts, k_chunks=POSTING_CHUNK_K)

    # ----------------------------------------------------------------------------------
    # Arms
    # ----------------------------------------------------------------------------------

    async def _vector_fact_ids(
        self,
        user_id: uuid.UUID,
        vector: Sequence[float] | None,
        *,
        k: int,
        kinds: Sequence[FactKind] | None,
    ) -> list[uuid.UUID]:
        """Return fact ids from the vector index, best first.

        Args:
            user_id: Owning user, applied as an index filter.
            vector: The query embedding, or ``None`` when embedding is unavailable.
            k: Target result count; the index is asked for :data:`VECTOR_OVERFETCH` times
                more so fusion has something to work with.
            kinds: Restrict to these fact kinds.

        Returns:
            Fact ids, best first; ``[]`` when there is no usable vector or index.
        """
        if not vector:
            return []
        filters: dict[str, Any] = {"user_id": str(user_id)}
        if kinds:
            filters["kind"] = [FactKind(kind).value for kind in kinds]
        hits = await self._store.vector_query(
            FACT_COLLECTION, vector, k=max(1, k * VECTOR_OVERFETCH), filters=filters
        )
        return [
            identifier
            for identifier in (_as_uuid(hit.id) for hit in hits)
            if identifier is not None
        ]

    async def _vector_chunks(
        self, user_id: uuid.UUID, vector: Sequence[float] | None, *, k: int
    ) -> list[VectorHit]:
        """Return document-chunk hits from the vector index, best first.

        Args:
            user_id: Owning user, applied as an index filter.
            vector: The query embedding, or ``None`` when embedding is unavailable.
            k: Maximum hits.

        Returns:
            The hits; ``[]`` when there is no usable vector or index. Chunk retrieval is
            purely semantic — a chunk has no keyword arm because the fact store already
            covers exact-token matching over the same corpus, in a form that carries
            provenance.
        """
        if not vector or k < 1:
            return []
        return await self._store.vector_query(
            CHUNK_COLLECTION, vector, k=k, filters={"user_id": str(user_id)}
        )

    async def _keyword_fact_ids(
        self,
        user_id: uuid.UUID,
        query: str,
        *,
        k: int,
        kinds: Sequence[FactKind] | None,
    ) -> list[uuid.UUID]:
        """Return fact ids matching *query*'s keywords, best first.

        The SQL prefilter is a plain ``OR`` of substring matches over the normalised text,
        the organization and the role. Ranking then happens in Python over the bounded
        result set, by how many *distinct* keywords a row actually contains and then by
        impact — an ordering portable SQL cannot express and that costs nothing once the
        candidate set is capped.

        Only the ranking columns are selected, not whole ORM rows: the winners are loaded
        properly by :meth:`_load_candidates` afterwards, and materialising every candidate
        twice would be wasted work.

        Args:
            user_id: Owning user.
            query: The raw query text.
            k: Target result count; the query fetches :data:`KEYWORD_OVERFETCH` times more
                before ranking.
            kinds: Restrict to these fact kinds.

        Returns:
            Fact ids, best first.
        """
        keywords = _keywords(query)
        if not keywords or k < 1:
            return []

        conditions = []
        for keyword in keywords:
            pattern = _like_pattern(keyword)
            conditions.append(KnowledgeFact.normalized_text.like(pattern, escape=_LIKE_ESCAPE))
            conditions.append(
                func.lower(KnowledgeFact.organization).like(pattern, escape=_LIKE_ESCAPE)
            )
            conditions.append(func.lower(KnowledgeFact.role).like(pattern, escape=_LIKE_ESCAPE))

        statement = select(
            KnowledgeFact.id,
            KnowledgeFact.normalized_text,
            KnowledgeFact.organization,
            KnowledgeFact.role,
            KnowledgeFact.skills,
            KnowledgeFact.technologies,
            KnowledgeFact.impact_score,
            KnowledgeFact.confidence,
        ).where(
            KnowledgeFact.user_id == user_id,
            KnowledgeFact.is_active.is_(True),
            or_(*conditions),
        )
        if kinds:
            statement = statement.where(KnowledgeFact.kind.in_([FactKind(kind) for kind in kinds]))
        statement = statement.order_by(
            KnowledgeFact.impact_score.desc(),
            KnowledgeFact.confidence.desc(),
            KnowledgeFact.created_at.asc(),
        ).limit(max(k * KEYWORD_OVERFETCH, k))

        rows = (await self.session.execute(statement)).all()
        ranked = sorted(
            rows,
            key=lambda row: (
                -_keyword_overlap(row, keywords),
                -int(row.impact_score or 0),
                -float(row.confidence or 0.0),
                str(row.id),
            ),
        )
        return [row.id for row in ranked]

    async def _load_candidates(
        self,
        user_id: uuid.UUID,
        scores: dict[uuid.UUID, float],
        *,
        limit: int,
        kinds: Sequence[FactKind] | None,
    ) -> list[KnowledgeFact]:
        """Load the best-scoring fused candidates and re-check every invariant on them.

        Args:
            user_id: Owning user.
            scores: The fused scores.
            limit: How many candidates to load.
            kinds: Restrict to these fact kinds.

        Returns:
            The candidate rows, with ``entity`` and ``source_document`` eagerly loaded.
        """
        if not scores:
            return []
        ordered = sorted(scores, key=lambda item: (-scores[item], str(item)))[: max(1, limit)]
        rows = await self._facts.by_ids(ordered)
        resolved = set(kinds or ())
        return [
            row
            for row in rows
            if row.user_id == user_id
            and row.is_active
            and (not resolved or FactKind(row.kind) in resolved)
        ]

    # ----------------------------------------------------------------------------------
    # Graph expansion
    # ----------------------------------------------------------------------------------

    async def _expand_graph(
        self, user_id: uuid.UUID, query: str, rows: Sequence[KnowledgeFact]
    ) -> list[KnowledgeEntity]:
        """Return the entities the query names, plus their one-hop neighbours.

        Seeds come from two places: entities whose name matches a query keyword, and the
        entities the top-ranked facts are already anchored to. Each seed is then walked one
        hop in :meth:`app.knowledge.graph.KnowledgeGraph.neighbors`, which traverses edges in
        both directions — "PyTorch *used_in* PoseNet" is the same knowledge whichever end the
        question started from.

        Args:
            user_id: Owning user.
            query: The raw query text.
            rows: The candidate facts, used as a second source of seeds.

        Returns:
            Seeds first, then neighbours, deduplicated and capped at
            :data:`MAX_EXPANDED_ENTITIES`.
        """
        seeds = await self._seed_entities(user_id, query, rows)
        if not seeds:
            return []

        collected: dict[uuid.UUID, KnowledgeEntity] = {entity.id: entity for entity in seeds}
        for seed in seeds:
            if len(collected) >= MAX_EXPANDED_ENTITIES:
                break
            for neighbor in await self._graph.neighbors(seed.id, depth=1):
                if neighbor.id not in collected:
                    collected[neighbor.id] = neighbor
                if len(collected) >= MAX_EXPANDED_ENTITIES:
                    break

        expanded = list(collected.values())[:MAX_EXPANDED_ENTITIES]
        logger.debug(
            "knowledge.graph_expanded",
            user_id=str(user_id),
            seeds=len(seeds),
            entities=len(expanded),
        )
        return expanded

    async def _seed_entities(
        self, user_id: uuid.UUID, query: str, rows: Sequence[KnowledgeFact]
    ) -> list[KnowledgeEntity]:
        """Return the entities the expansion starts from, most prominent first.

        Args:
            user_id: Owning user.
            query: The raw query text.
            rows: The candidate facts.

        Returns:
            At most :data:`SEED_ENTITY_LIMIT` entities.
        """
        keywords = _keywords(query)
        seeds: dict[uuid.UUID, KnowledgeEntity] = {}

        if keywords:
            conditions = [
                KnowledgeEntity.normalized_name.like(_like_pattern(keyword), escape=_LIKE_ESCAPE)
                for keyword in keywords
            ]
            statement = (
                select(KnowledgeEntity)
                .where(KnowledgeEntity.user_id == user_id, or_(*conditions))
                .order_by(
                    KnowledgeEntity.mention_count.desc(),
                    KnowledgeEntity.confidence.desc(),
                    KnowledgeEntity.normalized_name.asc(),
                )
                .limit(SEED_ENTITY_LIMIT)
            )
            for entity in (await self.session.execute(statement)).scalars().all():
                seeds[entity.id] = entity

        anchored = [row.entity_id for row in rows if row.entity_id is not None]
        for entity_id in anchored:
            if len(seeds) >= SEED_ENTITY_LIMIT:
                break
            if entity_id in seeds:
                continue
            anchor = await self.session.get(KnowledgeEntity, entity_id)
            if anchor is not None and anchor.user_id == user_id:
                seeds[anchor.id] = anchor

        return list(seeds.values())[:SEED_ENTITY_LIMIT]

    @staticmethod
    def _boost_linked(
        rows: Sequence[KnowledgeFact],
        entities: Sequence[KnowledgeEntity],
        scores: dict[uuid.UUID, float],
    ) -> list[uuid.UUID]:
        """Add :data:`GRAPH_LINK_BOOST` to every fact the expanded entities link to.

        A fact counts as linked when it is anchored to one of the entities, names one in its
        ``skills`` or ``technologies``, or mentions one in its normalised text. The text
        check is restricted to names of at least :data:`MIN_LINK_NAME_CHARS` characters and
        matched on whole tokens for single-word names, so "AI" cannot fire inside
        "maintained".

        Args:
            rows: The candidate facts. Scored in *scores*, mutated in place.
            entities: The expanded entity set.
            scores: The fused scores, mutated in place.

        Returns:
            The ids of the facts that were boosted.
        """
        if not entities or not rows:
            return []

        anchors = {entity.id for entity in entities}
        single: set[str] = set()
        phrases: set[str] = set()
        for entity in entities:
            for surface in (entity.name, *(entity.aliases or ())):
                normalized = normalize_for_matching(str(surface))
                if len(normalized) < MIN_LINK_NAME_CHARS:
                    continue
                (phrases if " " in normalized else single).add(normalized)

        boosted: list[uuid.UUID] = []
        for row in rows:
            if not _is_linked(row, anchors, single, phrases):
                continue
            scores[row.id] = scores.get(row.id, 0.0) + GRAPH_LINK_BOOST
            boosted.append(row.id)
        return boosted

    # ----------------------------------------------------------------------------------
    # Explanation
    # ----------------------------------------------------------------------------------

    @staticmethod
    def explain(result: RetrievalResult) -> str:
        """Render *result* as a human-readable account of why each item is there.

        The debugging tool for the hardest question this subsystem raises — "why did the
        resume engine never see my best project?" — and the data behind the desktop app's
        per-item provenance popover. Cheap enough to log on demand and never called on a hot
        path.

        Args:
            result: A retrieval to describe.

        Returns:
            A multi-line report. Never empty: an empty retrieval says so explicitly, because
            silence is the one answer that cannot be debugged.
        """
        lines = [f'Retrieval for: "{_one_line(result.query)}"']
        counts = result.counts()
        lines.append(
            "  facts={facts} chunks={chunks} entities={entities} memories={memories}".format(
                **counts
            )
        )
        if result.is_empty():
            lines.append("  (nothing matched)")
            return "\n".join(lines)

        if result.facts:
            lines.append("  Facts:")
            lines.extend(
                f"    {index:>2}. [{score:.4f}] {_one_line(fact.text)}  <- {reasons}"
                for index, fact, score, reasons in _rendered(
                    result, ITEM_FACT, result.facts, lambda item: item.text
                )
            )
        if result.chunks:
            lines.append("  Chunks:")
            lines.extend(
                f"    {index:>2}. [{score:.4f}] "
                f"{hit.metadata.get('title') or hit.metadata.get('uri') or hit.id}: "
                f"{_one_line(text)}  <- {reasons}"
                for index, hit, score, reasons, text in _rendered_chunks(result)
            )
        if result.entities:
            lines.append("  Entities:")
            lines.extend(
                f"    {index:>2}. [{score:.4f}] {entity.kind.value}/{entity.name} "
                f"(mentions={entity.mention_count})  <- {reasons}"
                for index, entity, score, reasons in _rendered(
                    result, ITEM_ENTITY, result.entities, lambda item: item.name
                )
            )
        if result.memories:
            lines.append("  Memories:")
            lines.extend(
                f"    {index:>2}. [{score:.4f}] {memory.kind.value}: "
                f"{_one_line(memory.text)}  <- {reasons}"
                for index, memory, score, reasons in _rendered(
                    result, ITEM_MEMORY, result.memories, lambda item: item.text
                )
            )
        return "\n".join(lines)


# ======================================================================================
# Module-private helpers
# ======================================================================================

#: Characters of an item's text shown by :meth:`KnowledgeRetriever.explain`.
_EXPLAIN_TEXT_CHARS: Final[int] = 110


def _one_line(text: str | None) -> str:
    """Collapse *text* to a single truncated line for :meth:`KnowledgeRetriever.explain`.

    Args:
        text: Any text, possibly ``None``.

    Returns:
        A whitespace-collapsed excerpt of at most :data:`_EXPLAIN_TEXT_CHARS` characters.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= _EXPLAIN_TEXT_CHARS:
        return collapsed
    return f"{collapsed[:_EXPLAIN_TEXT_CHARS].rstrip()}…"


def _rendered(
    result: RetrievalResult,
    kind: str,
    items: Sequence[Any],
    _text: Any,
) -> Iterable[tuple[int, Any, float, str]]:
    """Yield ``(position, item, score, reasons)`` for one collection of a result.

    Args:
        result: The retrieval being explained.
        kind: The item kind, for score and reason lookup.
        items: The collection to render.
        _text: Unused; kept so each call site reads uniformly.

    Yields:
        One tuple per item, numbered from one.
    """
    for position, item in enumerate(items, start=1):
        yield (
            position,
            item,
            result.score_of(kind, item.id),
            ", ".join(result.reasons_for(kind, item.id)) or "unranked",
        )


def _rendered_chunks(
    result: RetrievalResult,
) -> Iterable[tuple[int, Any, float, str, str]]:
    """Yield ``(position, hit, score, reasons, text)`` for a result's chunks.

    Args:
        result: The retrieval being explained.

    Yields:
        One tuple per chunk hit, numbered from one.
    """
    for position, hit in enumerate(result.chunks, start=1):
        yield (
            position,
            hit,
            result.score_of(ITEM_CHUNK, hit.id),
            ", ".join(result.reasons_for(ITEM_CHUNK, hit.id)) or "unranked",
            hit.text,
        )


def _keyword_overlap(row: Any, keywords: Sequence[str]) -> int:
    """Count how many of *keywords* a keyword-arm row mentions anywhere.

    Args:
        row: A result row carrying the ranking columns selected by
            :meth:`KnowledgeRetriever._keyword_fact_ids`.
        keywords: Case-folded query keywords.

    Returns:
        The number of distinct keywords present in the normalised text, the organization,
        the role, the skills or the technologies.
    """
    haystack = " ".join(
        part
        for part in (
            row.normalized_text or "",
            (row.organization or "").casefold(),
            (row.role or "").casefold(),
            " ".join(row.skills or []).casefold(),
            " ".join(row.technologies or []).casefold(),
        )
        if part
    )
    return sum(1 for keyword in keywords if keyword in haystack)


def _is_linked(
    row: KnowledgeFact,
    anchors: set[uuid.UUID],
    single: set[str],
    phrases: set[str],
) -> bool:
    """Return whether *row* is connected to the expanded entity set.

    Args:
        row: A candidate fact.
        anchors: Ids of the expanded entities.
        single: Normalised single-word entity names and aliases.
        phrases: Normalised multi-word entity names and aliases.

    Returns:
        ``True`` when the fact is anchored to one of the entities, names one among its
        skills or technologies, or mentions one in its text.
    """
    if row.entity_id is not None and row.entity_id in anchors:
        return True

    named = {
        normalize_for_matching(str(value))
        for value in (*(row.skills or ()), *(row.technologies or ()))
    }
    if named & single or named & phrases:
        return True

    normalized = row.normalized_text or ""
    if not normalized:
        return False
    if single and set(normalized.split()) & single:
        return True
    return any(phrase in normalized for phrase in phrases)


def _as_uuid(value: str) -> uuid.UUID | None:
    """Parse a vector record id back into a UUID.

    Args:
        value: The record id, which is a row's primary key rendered as a string.

    Returns:
        The parsed UUID, or ``None`` for an id that did not come from this schema. Never
        raises: a stale record in the index must not fail a retrieval.
    """
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None
