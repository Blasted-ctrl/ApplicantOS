"""Knowledge-engine schemas — sources, documents, chunks, graph, facts, and search.

These models are the API surface over ``docs/CONTRACTS.md`` §8: the ingestion chain
(source → document → chunk), the graph (entities and edges), the fact store that resumes are
generated from, and the statistics and search results the desktop knowledge screen renders.

Two conventions run through the whole module:

**Embeddings never leave the process.** Every vector-bearing read model declares
``embedding`` with ``exclude=True`` and exposes a derived ``has_embedding`` flag instead. A
1536-float array per row would dominate a list response, and no client has any use for it —
similarity is computed server-side by the vector store. Declaring the field (rather than
omitting it) is deliberate: it lets ``model_validate(orm_row)`` populate the flag correctly
while keeping the vector out of the serialised output.

**Identifiers, not nested relationships.** Read models carry ``source_id``,
``source_document_id``, ``entity_id`` rather than nested objects, because the corresponding
SQLAlchemy relationships are lazily loaded and touching one during validation inside an
async session raises ``MissingGreenlet``. The graph view (:class:`GraphView`) is the one
place a nested structure is returned, and it is assembled explicitly by
:meth:`app.knowledge.graph.KnowledgeGraph.subgraph`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, computed_field, field_validator, model_validator

from app.models.enums import EntityKind, FactKind, IndexStatus, RelationKind, SourceKind
from app.schemas.common import Schema

__all__ = [
    "ChunkRead",
    "DocumentRead",
    "EdgeRead",
    "EntityRead",
    "FactRead",
    "FactUpdate",
    "GraphEdge",
    "GraphNode",
    "GraphView",
    "IndexReportRead",
    "KnowledgeSearchKind",
    "KnowledgeSearchResult",
    "KnowledgeStats",
    "SourceCreate",
    "SourceRead",
]

#: Which store a search hit came from. Mirrors the four collections
#: :class:`app.knowledge.retrieval.RetrievalResult` fuses.
KnowledgeSearchKind = Literal["fact", "chunk", "entity", "memory"]

#: Confidence and similarity scores are unit-interval floats everywhere in the engine.
_UNIT_FLOOR: float = 0.0
_UNIT_CEILING: float = 1.0

#: Impact scores are a 0-100 heuristic prominence band used to rank bullets when a resume
#: has to shrink to fit one page.
_IMPACT_FLOOR: int = 0
_IMPACT_CEILING: int = 100


# ======================================================================================
# Sources
# ======================================================================================


class SourceCreate(Schema):
    """Register something for the indexer to read.

    Identity is ``(user_id, kind, uri)``, so re-posting the same pair is an update rather
    than a duplicate — the unique constraint on ``knowledge_sources`` guarantees it.
    """

    kind: SourceKind = Field(description="Which analyzer handles this source.")
    uri: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "URL, absolute filesystem path, or provider-specific identifier. Supply this or "
            "`file_id`, not both."
        ),
    )
    file_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "An `UploadedFile` to index instead of a location. The server resolves it to the "
            "stored path, so a resume, a cover letter or a LinkedIn export can be uploaded "
            "rather than typed out as an absolute path the user has to go and find."
        ),
    )
    label: str | None = Field(default=None, description="Human name shown in the app.")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Analyzer options: branch, depth, include/exclude globs.",
    )
    enabled: bool = Field(
        default=True,
        description="Disabled sources keep their data but are skipped by every pass.",
    )
    auto_refresh: bool = Field(
        default=True,
        description="Whether the periodic refresh worker may re-index this source.",
    )

    @field_validator("uri")
    @classmethod
    def _strip_uri(cls, value: str | None) -> str | None:
        """Trim surrounding whitespace, rejecting a URI that is only whitespace.

        The URI is half of a unique key, so a stray trailing space would create a second
        row for the same source.
        """
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("uri must not be blank")
        return trimmed

    @model_validator(mode="after")
    def _exactly_one_location(self) -> SourceCreate:
        """Require exactly one of ``uri`` and ``file_id``.

        Both would be ambiguous about which one identity is keyed on, and neither leaves the
        indexer with nothing to read.

        Returns:
            This model, unchanged.

        Raises:
            ValueError: If both or neither were supplied.
        """
        if (self.uri is None) == (self.file_id is None):
            raise ValueError("supply exactly one of `uri` or `file_id`")
        return self


class SourceRead(Schema):
    """A registered knowledge source with its indexing state.

    ``index_status``, ``last_indexed_at`` and ``last_error`` together are what the desktop
    knowledge screen renders as a per-source health badge.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    kind: SourceKind
    uri: str
    label: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    index_status: IndexStatus = IndexStatus.PENDING
    last_indexed_at: datetime | None = None
    last_error: str | None = Field(
        default=None,
        description="Message from the last failure; cleared on the next success.",
    )
    etag: str | None = Field(default=None, description="HTTP validator for web sources.")
    content_hash: str | None = Field(
        default=None,
        description="Digest of the last-seen content, for non-HTTP sources.",
    )
    auto_refresh: bool = True
    created_at: datetime
    updated_at: datetime


# ======================================================================================
# Documents and chunks
# ======================================================================================


class DocumentRead(Schema):
    """One extracted text artifact: a repository, a crawled page, a file, a parsed resume.

    ``raw_text`` is the full extracted text and can be large. List endpoints leave it unset
    (it defaults to ``None``); the document detail endpoint populates it.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    source_id: uuid.UUID
    kind: SourceKind
    uri: str
    title: str
    raw_text: str | None = Field(
        default=None,
        description="Full extracted text. Omitted from list responses; large.",
    )
    summary: str | None = None
    content_hash: str = Field(description="Digest of raw_text; the change-detection key.")
    metadata_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Analyzer extras: stars, languages, headers, file counts.",
    )
    token_count: int = 0
    indexed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ChunkRead(Schema):
    """A contiguous, embedded slice of a document — the unit of semantic retrieval."""

    id: uuid.UUID
    document_id: uuid.UUID
    ordinal: int = Field(description="Zero-based position within the document.")
    text: str
    token_count: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="Populated on validation, never serialised; see has_embedding.",
    )
    created_at: datetime
    updated_at: datetime

    @computed_field(description="Whether this chunk has been embedded yet.")  # type: ignore[prop-decorator]
    @property
    def has_embedding(self) -> bool:
        """Whether the embedding backlog has reached this chunk."""
        return self.embedding is not None


# ======================================================================================
# Graph
# ======================================================================================


class EntityRead(Schema):
    """A node in the user's knowledge graph — a skill, technology, project, employer, …"""

    id: uuid.UUID
    user_id: uuid.UUID
    kind: EntityKind
    name: str = Field(description="Display name, in the casing the source used.")
    normalized_name: str = Field(description="Deduplication key; identity within a kind.")
    summary: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    confidence: float = Field(default=_UNIT_FLOOR, ge=_UNIT_FLOOR, le=_UNIT_CEILING)
    mention_count: int = Field(
        default=0,
        description="Observations across all sources; a cheap prominence signal.",
    )
    first_seen_at: datetime
    last_seen_at: datetime
    embedding: list[float] | None = Field(default=None, exclude=True, repr=False)
    created_at: datetime
    updated_at: datetime

    @computed_field(description="Whether this entity has been embedded yet.")  # type: ignore[prop-decorator]
    @property
    def has_embedding(self) -> bool:
        """Whether an embedding exists for similarity expansion."""
        return self.embedding is not None


class EdgeRead(Schema):
    """A typed, weighted relationship between two entities."""

    id: uuid.UUID
    user_id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    relation: RelationKind
    weight: float = Field(default=1.0, description="Strength; ranks graph expansion.")
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Provenance: document ids, quoted spans, extractor name.",
    )
    created_at: datetime
    updated_at: datetime


class GraphNode(Schema):
    """A node as rendered by the desktop graph view.

    Deliberately smaller than :class:`EntityRead`: a graph canvas draws hundreds of these
    at once and needs a label, a colour key (``kind``) and a size key (``mention_count``),
    not the full row.
    """

    id: uuid.UUID
    kind: EntityKind
    label: str = Field(description="Text drawn on the node.")
    summary: str | None = Field(default=None, description="Tooltip body.")
    mention_count: int = Field(default=0, description="Drives node size.")
    confidence: float = Field(default=_UNIT_FLOOR, ge=_UNIT_FLOOR, le=_UNIT_CEILING)
    fact_count: int = Field(default=0, description="Facts anchored to this entity.")


class GraphEdge(Schema):
    """An edge as rendered by the desktop graph view."""

    id: uuid.UUID
    source: uuid.UUID = Field(description="Id of the subject node.")
    target: uuid.UUID = Field(description="Id of the object node.")
    relation: RelationKind
    weight: float = Field(default=1.0, description="Drives edge thickness.")


class GraphView(Schema):
    """A renderable slice of the knowledge graph.

    Returned by ``GET /knowledge/graph`` and by
    :meth:`app.knowledge.graph.KnowledgeGraph.subgraph`. ``stats`` carries the counts the
    view is summarised by — total nodes, total edges, and per-kind breakdowns — so the UI
    can label a truncated view honestly ("showing 500 of 3,214 nodes").
    """

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    stats: dict[str, int] = Field(
        default_factory=dict,
        description="Counts describing the full graph, not just the returned slice.",
    )


# ======================================================================================
# Facts
# ======================================================================================


class FactRead(Schema):
    """One atomic, source-attributed claim about the user.

    Every resume bullet ApplicantOS produces traces back to one of these rows, which is
    what makes golden rule #7 — nothing is fabricated — checkable rather than aspirational.
    Dates are returned exactly as the source wrote them ("2024-06", "Summer 2023",
    "Present"); they are never parsed, because parsing would invent precision.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    kind: FactKind
    text: str = Field(description="The claim, in the source's own words.")
    normalized_text: str = Field(
        default="",
        description="Matching form of text; the deduplication and hashing input.",
    )
    organization: str | None = None
    role: str | None = None
    location: str | None = None
    date_start: str | None = Field(default=None, description="As written; never parsed.")
    date_end: str | None = Field(default=None, description="As written; 'Present' is valid.")
    skills: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(
        default_factory=list,
        description='Quantified outcomes quoted verbatim ("cut p95 latency 43%").',
    )
    impact_score: int = Field(
        default=_IMPACT_FLOOR,
        ge=_IMPACT_FLOOR,
        le=_IMPACT_CEILING,
        description="Heuristic prominence used to rank bullets when a resume must shrink.",
    )
    confidence: float = Field(default=_UNIT_FLOOR, ge=_UNIT_FLOOR, le=_UNIT_CEILING)
    source_document_id: uuid.UUID | None = Field(
        default=None,
        description="Document this was extracted from; survives that document's deletion.",
    )
    entity_id: uuid.UUID | None = None
    user_verified: bool = Field(
        default=False,
        description="Confirmed by a human; a later index never silently rewrites it.",
    )
    is_active: bool = Field(
        default=True,
        description="False retires the fact from generation without deleting evidence.",
    )
    content_hash: str = Field(description="Stable deduplication digest.")
    embedding: list[float] | None = Field(default=None, exclude=True, repr=False)
    created_at: datetime
    updated_at: datetime

    @computed_field(description="Whether this fact has been embedded yet.")  # type: ignore[prop-decorator]
    @property
    def has_embedding(self) -> bool:
        """Whether an embedding exists for semantic retrieval."""
        return self.embedding is not None


class FactUpdate(Schema):
    """Partial update to a fact — the body of ``PATCH /knowledge/facts/{id}``.

    This is the user-correction path. Editing ``text``, ``organization``, ``role`` or the
    dates changes the fact's identity, so the service must recompute ``normalized_text`` and
    ``content_hash`` through
    :meth:`app.models.knowledge.KnowledgeFact.refresh_derived_fields` after applying the
    change, or deduplication silently stops working for that row.

    Apply with ``model_dump(exclude_unset=True)``.
    """

    kind: FactKind | None = None
    text: str | None = Field(default=None, min_length=1)
    organization: str | None = None
    role: str | None = None
    location: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    skills: list[str] | None = None
    technologies: list[str] | None = None
    metrics: list[str] | None = None
    impact_score: int | None = Field(default=None, ge=_IMPACT_FLOOR, le=_IMPACT_CEILING)
    confidence: float | None = Field(default=None, ge=_UNIT_FLOOR, le=_UNIT_CEILING)
    user_verified: bool | None = None
    is_active: bool | None = None


# ======================================================================================
# Search, statistics, and indexing reports
# ======================================================================================


class KnowledgeSearchResult(Schema):
    """One scored hit from ``GET /knowledge/search``.

    The retriever fuses four stores — facts, chunks, entities and memories — with
    reciprocal-rank fusion, so results arrive already interleaved. ``kind`` says which store
    a hit came from and ``id`` is that row's primary key, letting the client deep-link to
    the underlying record.
    """

    id: uuid.UUID
    kind: KnowledgeSearchKind = Field(description="Which store this hit came from.")
    score: float = Field(description="Fused relevance score; higher is better.")
    title: str | None = Field(default=None, description="Entity name or document title.")
    text: str = Field(description="The matched text, or a snippet of it.")
    source_uri: str | None = Field(default=None, description="Where the text came from.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Store-specific extras: fact kind, chunk ordinal, entity kind.",
    )


class KnowledgeStats(Schema):
    """Aggregate counts behind ``GET /knowledge/stats`` and the knowledge dashboard."""

    sources: int = 0
    sources_enabled: int = 0
    sources_failed: int = Field(
        default=0,
        description="Sources whose last indexing pass ended in IndexStatus.FAILED.",
    )
    documents: int = 0
    chunks: int = 0
    chunks_embedded: int = Field(
        default=0,
        description="Chunks with a vector; the gap to `chunks` is the embedding backlog.",
    )
    entities: int = 0
    edges: int = 0
    facts: int = 0
    facts_active: int = 0
    facts_verified: int = 0
    memories: int = 0
    tokens_indexed: int = Field(
        default=0,
        description="Sum of document token counts; the size of the knowledge base.",
    )
    by_source_kind: dict[str, int] = Field(default_factory=dict)
    by_entity_kind: dict[str, int] = Field(default_factory=dict)
    by_fact_kind: dict[str, int] = Field(default_factory=dict)
    last_indexed_at: datetime | None = Field(
        default=None,
        description="Most recent successful index across every source.",
    )


class IndexReportRead(Schema):
    """Outcome of one indexing pass over one source.

    Mirrors :class:`app.knowledge.indexer.IndexReport`. ``skipped`` is the common and
    desirable case: the fingerprint had not moved, so nothing was re-extracted,
    re-chunked, or re-embedded.
    """

    source_id: uuid.UUID
    documents: int = 0
    chunks: int = 0
    facts: int = 0
    entities: int = 0
    edges: int = 0
    skipped: bool = Field(
        default=False,
        description="True when the source was unchanged and no work was done.",
    )
    duration_seconds: float = 0.0
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal problems; a report with errors may still have indexed.",
    )
