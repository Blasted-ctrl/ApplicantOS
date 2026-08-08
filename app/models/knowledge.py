"""The knowledge engine's storage layer — sources, documents, chunks, graph, facts, memory.

This module is the foundation of golden rules #6 and #7 (``docs/CONTRACTS.md`` §18):
knowledge is the source of truth, resumes are generated *views* over it, and nothing is
ever fabricated. Seven tables cooperate to make that auditable.

**Ingestion chain.** A :class:`KnowledgeSource` is something the user pointed us at — a
GitHub account, a personal site, a project folder, a resume PDF, a LinkedIn export. An
analyzer turns it into :class:`KnowledgeDocument` rows (one per repo, page, or file), each
of which is split into ordered :class:`KnowledgeChunk` rows carrying the embeddings that
back semantic retrieval. Deleting a source deletes its documents, which deletes their
chunks, at both the ORM and the database level.

**Graph.** :class:`KnowledgeEntity` nodes (skills, technologies, projects, organizations,
roles, …) are deduplicated on :attr:`KnowledgeEntity.normalized_name`, and
:class:`KnowledgeEdge` rows connect them with a typed
:class:`~app.models.enums.RelationKind`. Both edge endpoints are foreign keys into the same
table, so every relationship between the two models is disambiguated with an explicit
``foreign_keys=``.

**Facts.** :class:`KnowledgeFact` replaces the old "master resume bullet". Each row is one
atomic, source-attributed claim — an accomplishment, a responsibility, a metric, a skill
usage — with the organization, role and dates it belongs to. The resume engine may only
select and rewrite these; a fact id it did not receive is dropped and logged. Facts
deduplicate on :attr:`KnowledgeFact.content_hash`, a stable digest over the normalised text
plus the organization, role and dates, so re-indexing the same source a hundred times
produces one row.

**Memory.** :class:`MemoryEntry` closes the loop: user corrections, application outcomes,
feedback, and stated preferences are retrieved alongside facts so later generations reflect
what the user already taught the system.

Two design notes worth carrying into every consumer of this module:

* **Provenance is a foreign key, not a comment.** ``KnowledgeFact.source_document_id`` and
  ``entity_id`` are ``ON DELETE SET NULL`` rather than ``CASCADE``. Re-indexing churns
  documents; a verified fact must outlive the document it was first seen in, losing only
  its pointer, never itself.
* **``__repr__`` never touches the database.** Each one renders from ``self.__dict__``,
  which holds only loaded values, so it cannot emit SQL on an expired instance or raise
  ``DetachedInstanceError`` on a detached one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database.base import Base
from app.database.types import GUID, EmbeddingType, JSONType, utcnow
from app.models.enums import (
    EntityKind,
    FactKind,
    IndexStatus,
    MemoryKind,
    RelationKind,
    SourceKind,
    StrEnum,
)
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for runtime, kept for typing
    from app.models.user import User

__all__ = [
    "CONTENT_HASH_LENGTH",
    "DEFAULT_CONFIDENCE",
    "DEFAULT_EDGE_WEIGHT",
    "DEFAULT_MEMORY_WEIGHT",
    "URI_MAX_LENGTH",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeEdge",
    "KnowledgeEntity",
    "KnowledgeFact",
    "KnowledgeSource",
    "MemoryEntry",
    "normalize_for_matching",
]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Column sizing and defaults
# --------------------------------------------------------------------------------------

#: Hex digest width of SHA-256, used by every ``content_hash`` column.
CONTENT_HASH_LENGTH: Final[int] = 64

#: Ceiling for URIs, which here may be a URL, an absolute filesystem path, or a
#: ``repo#path`` style composite. Generous enough for real inputs while staying inside
#: PostgreSQL's B-tree index tuple limit for the composite unique constraints below.
URI_MAX_LENGTH: Final[int] = 2048

#: Ceiling for human-facing labels and titles.
TITLE_MAX_LENGTH: Final[int] = 512

#: Ceiling for short free-text attributes attached to a fact (organization, role, location).
ATTRIBUTE_MAX_LENGTH: Final[int] = 255

#: Dates on facts are stored as the source wrote them ("2024-06", "Summer 2023",
#: "Present"), because normalising them would fabricate precision the source never had.
DATE_TEXT_MAX_LENGTH: Final[int] = 64

#: HTTP validators are bounded by convention rather than by specification.
ETAG_MAX_LENGTH: Final[int] = 255

#: Entity display names are short; the normalised form is derived from them.
ENTITY_NAME_MAX_LENGTH: Final[int] = 512

#: Width of the VARCHAR backing every enum column. See :func:`_enum_column_type`.
ENUM_COLUMN_LENGTH: Final[int] = 64

#: Confidence assigned when an extractor does not state one. Matches the defaults on
#: ``ExtractedFact`` and ``ExtractedEntity`` in ``docs/CONTRACTS.md`` §8.1.
DEFAULT_CONFIDENCE: Final[float] = 0.5

#: Weight of a graph edge whose extractor did not state one.
DEFAULT_EDGE_WEIGHT: Final[float] = 1.0

#: Weight of a memory entry whose writer did not state one. Retrieval multiplies relevance
#: by this, so 1.0 is "counts normally".
DEFAULT_MEMORY_WEIGHT: Final[float] = 1.0

#: Field separator used when building a content hash. ASCII UNIT SEPARATOR never occurs in
#: extracted text, so no combination of field values can collide with a different split.
HASH_FIELD_SEPARATOR: Final[str] = "\x1f"

#: Encoding used for every digest input, pinned so hashes are stable across platforms.
HASH_ENCODING: Final[str] = "utf-8"

#: Characters of a long-text column shown in a ``__repr__``. Long enough to identify the
#: row, short enough to keep a debugger view or log line readable.
REPR_TEXT_PREVIEW: Final[int] = 60

#: Unicode normal form applied before matching. NFKC folds compatibility variants — full
#: width Latin, ligatures, non-breaking spaces — onto their canonical equivalents.
UNICODE_NORMAL_FORM: Final[str] = "NFKC"

#: Punctuation removed outright rather than replaced with a space, because it sits *inside*
#: a token: "Node.js" → "nodejs" (which then matches "NodeJS"), "O'Brien" → "obrien". The
#: three escapes are the typographic apostrophes that word processors and web pages emit
#: (U+2018, U+2019, U+02BC), written as escapes so the pattern stays pure ASCII.
_INTRA_WORD_PUNCTUATION: Final[re.Pattern[str]] = re.compile("[.'‘’ʼ]")

#: Everything that is not a word character, whitespace, ``+`` or ``#`` becomes a space.
#: ``+`` and ``#`` are preserved deliberately: stripping them would collapse "C", "C++"
#: and "C#" into one entity, which is a real and damaging false merge in this domain.
#: Underscore is listed explicitly because ``\w`` includes it.
_SEPARATOR_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[^\w\s+#]|_", re.UNICODE)

#: Any run of whitespace collapses to a single space.
_COLLAPSIBLE_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_for_matching(value: str) -> str:
    """Reduce *value* to a stable key for deduplication and content hashing.

    The transformation is, in order: NFKC normalisation, case folding, removal of
    intra-word punctuation, replacement of all other punctuation with a space, whitespace
    collapse, and trim. ``+`` and ``#`` survive so that ``C``, ``C++`` and ``C#`` remain
    three distinct entities.

    Examples:
        ``"Node.js"`` → ``"nodejs"``; ``"React / Redux"`` → ``"react redux"``;
        ``"C++"`` → ``"c++"``; ``"O'Brien  Labs"`` → ``"obrien labs"``.

    This is the single normalisation used by both :meth:`KnowledgeEntity.normalize` and
    :meth:`KnowledgeFact.normalize_text`, so an entity name and the fact text that mentions
    it are reduced the same way and no caller can drift.

    Args:
        value: Raw text. ``None``-like input is not accepted; pass ``""``.

    Returns:
        The normalised key, possibly empty.
    """
    normalized = unicodedata.normalize(UNICODE_NORMAL_FORM, value).casefold()
    normalized = _INTRA_WORD_PUNCTUATION.sub("", normalized)
    normalized = _SEPARATOR_CHARACTERS.sub(" ", normalized)
    return _COLLAPSIBLE_WHITESPACE.sub(" ", normalized).strip()


def _enum_column_type(enum_class: type[StrEnum], *, name: str) -> SAEnum:
    """Build a portable, value-persisted column type for a :class:`StrEnum`.

    ``native_enum=False`` keeps the column a plain ``VARCHAR`` on every backend instead of
    creating a PostgreSQL ``TYPE`` that would need a migration whenever an enum gains a
    member. ``values_callable`` persists the member *value* (``"github_repo"``) rather than
    SQLAlchemy's default of the member *name* (``"GITHUB_REPO"``), keeping the database,
    the JSON API and ``desktop/src/lib/api/types.ts`` byte-identical.

    Args:
        enum_class: The enumeration to store.
        name: Type name, used for the (unemitted) constraint name and by Alembic.

    Returns:
        A configured :class:`sqlalchemy.Enum` instance.
    """
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        length=ENUM_COLUMN_LENGTH,
        values_callable=lambda members: [str(member.value) for member in members],
        validate_strings=True,
    )


# ======================================================================================
# Ingestion chain: source -> document -> chunk
# ======================================================================================


class KnowledgeSource(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """Something the user pointed the indexer at, plus its indexing state.

    A source is identified by ``(user_id, kind, uri)``: the same GitHub account cannot be
    registered twice, and the same path cannot be both a ``project_folder`` and a
    ``readme`` by accident.

    Change detection uses three independent signals so re-indexing is cheap:
    :attr:`etag` for HTTP sources, :attr:`content_hash` for local ones, and the analyzer's
    own ``fingerprint()`` probe. When none of them moved, ``index_source`` skips the work
    entirely.

    Attributes:
        kind: Which analyzer handles this source.
        uri: URL, filesystem path, or provider-specific identifier.
        label: Optional human name shown in the desktop app.
        config: Analyzer-specific options (branch, depth, include/exclude globs).
        enabled: Disabled sources are skipped by every indexing pass but keep their data.
        index_status: Current indexing state; ``stale`` marks a detected upstream change.
        last_indexed_at: Completion time of the last successful index.
        last_error: Message from the last failure, retained until the next success.
        etag: HTTP entity tag from the last fetch, when the source is web-hosted.
        content_hash: Digest of the last-seen content, for non-HTTP sources.
        auto_refresh: Whether the periodic refresh worker may re-index this source.
    """

    __tablename__ = "knowledge_sources"
    __table_args__ = (UniqueConstraint("user_id", "kind", "uri"),)

    kind: Mapped[SourceKind] = mapped_column(
        _enum_column_type(SourceKind, name="source_kind"),
        nullable=False,
        index=True,
    )
    uri: Mapped[str] = mapped_column(String(URI_MAX_LENGTH), nullable=False)
    label: Mapped[str | None] = mapped_column(String(TITLE_MAX_LENGTH), nullable=True, default=None)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    index_status: Mapped[IndexStatus] = mapped_column(
        _enum_column_type(IndexStatus, name="index_status"),
        nullable=False,
        default=IndexStatus.PENDING,
        index=True,
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True, default=None)
    etag: Mapped[str | None] = mapped_column(String(ETAG_MAX_LENGTH), nullable=True, default=None)
    content_hash: Mapped[str | None] = mapped_column(
        String(CONTENT_HASH_LENGTH), nullable=True, default=None
    )
    auto_refresh: Mapped[bool] = mapped_column(nullable=False, default=True)

    user: Mapped[User] = relationship("User", back_populates="sources")

    documents: Mapped[list[KnowledgeDocument]] = relationship(
        "KnowledgeDocument",
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        return (
            f"<KnowledgeSource id={loaded.get('id')!s} kind={loaded.get('kind')!s} "
            f"uri={loaded.get('uri')!r} status={loaded.get('index_status')!s}>"
        )


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """One extracted text artifact: a repo, a crawled page, a file, a parsed resume.

    Unique on ``(source_id, uri)``, so re-indexing updates the existing row rather than
    accumulating duplicates. :attr:`content_hash` is indexed because the indexer's first
    question about every re-extracted document is "did this change?", and answering it from
    an index avoids re-chunking and re-embedding unchanged text — by far the most expensive
    step in the pipeline.

    Attributes:
        source_id: Owning :class:`KnowledgeSource`; deleting it deletes this row.
        kind: Provenance classification, copied from the source or refined by the analyzer.
        uri: Location of this specific artifact within the source.
        title: Human-facing name (repo name, page title, filename).
        raw_text: Full extracted plain text — the input to chunking.
        summary: Optional model-generated abstract used for cheap relevance filtering.
        content_hash: Digest of :attr:`raw_text`, the change-detection key.
        metadata_json: Analyzer-specific structured extras (stars, languages, headers).
        token_count: Approximate token length of :attr:`raw_text`, for budget accounting.
        indexed_at: When chunking and embedding last completed for this document.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("source_id", "uri"),)

    source_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[SourceKind] = mapped_column(
        _enum_column_type(SourceKind, name="source_kind"),
        nullable=False,
        index=True,
    )
    uri: Mapped[str] = mapped_column(String(URI_MAX_LENGTH), nullable=False)
    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True, default=None)
    content_hash: Mapped[str] = mapped_column(
        String(CONTENT_HASH_LENGTH),
        nullable=False,
        index=True,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    token_count: Mapped[int] = mapped_column(nullable=False, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)

    source: Mapped[KnowledgeSource] = relationship(
        "KnowledgeSource",
        back_populates="documents",
    )

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        "KnowledgeChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="KnowledgeChunk.ordinal",
    )

    #: Facts first extracted from this document. Not a cascade: a fact outlives the
    #: document that produced it (``ON DELETE SET NULL``), because re-indexing replaces
    #: documents while verified facts must persist.
    facts: Mapped[list[KnowledgeFact]] = relationship(
        "KnowledgeFact",
        back_populates="source_document",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        return (
            f"<KnowledgeDocument id={loaded.get('id')!s} kind={loaded.get('kind')!s} "
            f"title={loaded.get('title')!r} tokens={loaded.get('token_count')!r}>"
        )


class KnowledgeChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A contiguous, embedded slice of a document — the unit of semantic retrieval.

    Chunks are the only rows in the schema that exist purely for machine consumption, and
    the only ones with no ``user_id``: they are reachable exclusively through their
    document, which is user-owned, and denormalising ownership here would double the write
    volume of the hottest table in the system for no query benefit.

    ``(document_id, ordinal)`` is unique so re-chunking is an idempotent upsert, and
    ``document_id`` is indexed because deleting or re-embedding a document scans by it.

    Attributes:
        document_id: Owning :class:`KnowledgeDocument`; deleting it deletes this row.
        ordinal: Zero-based position within the document, which restores reading order.
        text: The chunk's plain text, as embedded.
        token_count: Token length of :attr:`text`.
        embedding: Vector representation, or ``None`` until the embedding backlog runs.
        metadata_json: Chunk-scoped extras (section heading, char offsets, language).
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document_id", "ordinal"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    token_count: Mapped[int] = mapped_column(nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingType(), nullable=True, default=None
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)

    document: Mapped[KnowledgeDocument] = relationship(
        "KnowledgeDocument",
        back_populates="chunks",
    )

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        return (
            f"<KnowledgeChunk id={loaded.get('id')!s} "
            f"document_id={loaded.get('document_id')!s} "
            f"ordinal={loaded.get('ordinal')!r} tokens={loaded.get('token_count')!r}>"
        )


# ======================================================================================
# Graph: entities and edges
# ======================================================================================


class KnowledgeEntity(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """A node in the user's knowledge graph — a skill, technology, project, employer, …

    Identity is ``(user_id, kind, normalized_name)``. The normalised form is what makes
    "Node.js", "NodeJS" and "node.js" one node instead of three, while
    :meth:`normalize` deliberately preserves ``+`` and ``#`` so "C", "C++" and "C#" stay
    apart. :attr:`normalized_name` is indexed because every upsert during indexing looks a
    candidate entity up by it.

    Attributes:
        kind: Node type.
        name: Display name, in the casing the source used.
        normalized_name: Deduplication key produced by :meth:`normalize`.
        summary: Optional short description used when the graph is rendered or retrieved.
        attributes: Kind-specific structured extras (proficiency, years, repo URL).
        aliases: Other surface forms seen for this entity, for retrieval recall.
        confidence: Extractor confidence, 0.0-1.0.
        mention_count: How many times this entity has been observed across all sources —
            a cheap, effective prominence signal for resume bullet selection.
        first_seen_at: When the entity was first created.
        last_seen_at: When it was last observed; drives staleness in the graph view.
        embedding: Vector representation of the entity for similarity expansion.
    """

    __tablename__ = "knowledge_entities"
    __table_args__ = (UniqueConstraint("user_id", "kind", "normalized_name"),)

    kind: Mapped[EntityKind] = mapped_column(
        _enum_column_type(EntityKind, name="entity_kind"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(ENTITY_NAME_MAX_LENGTH), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(ENTITY_NAME_MAX_LENGTH),
        nullable=False,
        index=True,
    )
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True, default=None)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    aliases: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float(), nullable=False, default=DEFAULT_CONFIDENCE)
    mention_count: Mapped[int] = mapped_column(nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingType(), nullable=True, default=None
    )

    #: Edges where this entity is the subject. ``foreign_keys`` is mandatory: both endpoint
    #: columns on ``knowledge_edges`` point at this table, so SQLAlchemy cannot infer which
    #: one joins this relationship.
    outgoing_edges: Mapped[list[KnowledgeEdge]] = relationship(
        "KnowledgeEdge",
        back_populates="source_entity",
        foreign_keys="KnowledgeEdge.source_entity_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    #: Edges where this entity is the object. See :attr:`outgoing_edges` on ``foreign_keys``.
    incoming_edges: Mapped[list[KnowledgeEdge]] = relationship(
        "KnowledgeEdge",
        back_populates="target_entity",
        foreign_keys="KnowledgeEdge.target_entity_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    #: Facts anchored to this entity. ``ON DELETE SET NULL``, so merging or dropping an
    #: entity never destroys evidence.
    facts: Mapped[list[KnowledgeFact]] = relationship(
        "KnowledgeFact",
        back_populates="entity",
        passive_deletes=True,
    )

    @staticmethod
    def normalize(name: str) -> str:
        """Return the deduplication key for an entity display name.

        Lowercases, strips punctuation and collapses whitespace via
        :func:`normalize_for_matching`, which also preserves ``+`` and ``#`` so that
        language names like ``C++`` and ``C#`` do not collapse into ``c``.

        A name composed entirely of punctuation ("---", "///") normalises to the empty
        string, which would make every such entity collide on the unique constraint. That
        is a defect in the extractor, not something to paper over here, so the condition is
        logged and the empty key is returned for the caller to reject.

        Args:
            name: The display name as the source wrote it.

        Returns:
            The value to store in :attr:`normalized_name`.
        """
        normalized = normalize_for_matching(name or "")
        if not normalized and (name or "").strip():
            logger.warning("knowledge.entity_name_normalized_empty", name=name)
        return normalized

    @validates("name")
    def _sync_normalized_name(self, _key: str, value: str) -> str:
        """Derive :attr:`normalized_name` from every assignment to :attr:`name`.

        :attr:`normalized_name` is ``NOT NULL`` with no default, so a caller who set only
        :attr:`name` used to hit an ``IntegrityError`` at flush time. Keeping the pair in
        step on the model means no caller can populate half of it, exactly as
        :meth:`KnowledgeFact.refresh_derived_fields` does for the ``normalized_text`` /
        ``content_hash`` pair.

        Assigning :attr:`normalized_name` explicitly *after* :attr:`name` still wins, so an
        upsert that already computed the key with :meth:`normalize` keeps its value.
        Loading a row from the database does not fire this hook, so stored keys are never
        rewritten on read.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The display name being assigned.

        Returns:
            *value* unchanged — only the derived column is written here.
        """
        self.normalized_name = self.normalize(value)
        return value

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        return (
            f"<KnowledgeEntity id={loaded.get('id')!s} kind={loaded.get('kind')!s} "
            f"name={loaded.get('name')!r} mentions={loaded.get('mention_count')!r}>"
        )


class KnowledgeEdge(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """A typed, weighted relationship between two :class:`KnowledgeEntity` nodes.

    ``(source_entity_id, target_entity_id, relation)`` is unique, so repeated observations
    of the same relationship strengthen one row (via :attr:`weight` and :attr:`evidence`)
    instead of multiplying rows.

    Both endpoints are foreign keys into ``knowledge_entities``, which makes this a
    self-referential association: every relationship touching it must name its
    ``foreign_keys`` explicitly.

    Attributes:
        source_entity_id: Subject of the relation.
        target_entity_id: Object of the relation.
        relation: Relationship type.
        weight: Strength, used to rank graph expansion during retrieval.
        evidence: Provenance for this edge — document ids, quoted spans, extractor name.
    """

    __tablename__ = "knowledge_edges"
    __table_args__ = (UniqueConstraint("source_entity_id", "target_entity_id", "relation"),)

    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("knowledge_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("knowledge_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation: Mapped[RelationKind] = mapped_column(
        _enum_column_type(RelationKind, name="relation_kind"),
        nullable=False,
        index=True,
    )
    weight: Mapped[float] = mapped_column(Float(), nullable=False, default=DEFAULT_EDGE_WEIGHT)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)

    source_entity: Mapped[KnowledgeEntity] = relationship(
        "KnowledgeEntity",
        back_populates="outgoing_edges",
        foreign_keys=[source_entity_id],
    )

    target_entity: Mapped[KnowledgeEntity] = relationship(
        "KnowledgeEntity",
        back_populates="incoming_edges",
        foreign_keys=[target_entity_id],
    )

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        return (
            f"<KnowledgeEdge id={loaded.get('id')!s} "
            f"{loaded.get('source_entity_id')!s} -[{loaded.get('relation')!s}]-> "
            f"{loaded.get('target_entity_id')!s} weight={loaded.get('weight')!r}>"
        )


# ======================================================================================
# Facts and memory
# ======================================================================================


class KnowledgeFact(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """One atomic, source-attributed claim about the user.

    This table replaces the old "master resume bullet": a resume is a generated *view* over
    these rows, never a stored document. Every bullet the resume engine emits carries the
    id of the fact it came from, which is what makes golden rule #7 — nothing is fabricated
    — checkable rather than aspirational.

    Two indexes carry the load. :attr:`content_hash` deduplicates on ingest: re-indexing a
    source that has not changed produces the same digests and updates rows in place.
    ``(user_id, is_active)`` serves the query the resume engine runs on every single
    tailoring pass — "give me this user's live facts" — which is the hottest read in the
    application.

    Attributes:
        kind: Category of claim.
        text: The claim as it should be read by a human, in the source's own words.
        normalized_text: :func:`normalize_for_matching` applied to :attr:`text`; the
            deduplication and hashing input.
        organization: Employer, school, or project the claim belongs to, if any.
        role: Title held while the claim was true, if any.
        location: Where it happened, if stated.
        date_start: Start of the period, as written by the source ("2024-06", "Summer
            2023"). Never parsed into a date — that would invent precision.
        date_end: End of the period, as written. ``"Present"`` is a legitimate value.
        skills: Skills evidenced by this claim.
        technologies: Technologies used.
        metrics: Quantified outcomes quoted verbatim ("cut p95 latency 43%").
        impact_score: Heuristic 0-100 prominence used to rank bullets when a resume must
            shrink to fit one page.
        confidence: Extractor confidence, 0.0-1.0.
        embedding: Vector representation for semantic retrieval.
        source_document_id: Document the claim was extracted from. Nullable and
            ``SET NULL`` on delete, so a fact survives its source being re-indexed away.
        entity_id: Primary graph node this claim attaches to, if any.
        user_verified: ``True`` once a human has confirmed the claim. Verified facts are
            never silently rewritten by a later index.
        is_active: ``False`` retires a fact from generation without deleting the evidence.
        content_hash: Stable digest from :meth:`compute_hash`; the deduplication key.
    """

    __tablename__ = "knowledge_facts"
    __table_args__ = (Index("ix_knowledge_facts_user_id_is_active", "user_id", "is_active"),)

    kind: Mapped[FactKind] = mapped_column(
        _enum_column_type(FactKind, name="fact_kind"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    organization: Mapped[str | None] = mapped_column(
        String(ATTRIBUTE_MAX_LENGTH), nullable=True, default=None
    )
    role: Mapped[str | None] = mapped_column(
        String(ATTRIBUTE_MAX_LENGTH), nullable=True, default=None
    )
    location: Mapped[str | None] = mapped_column(
        String(ATTRIBUTE_MAX_LENGTH), nullable=True, default=None
    )
    date_start: Mapped[str | None] = mapped_column(
        String(DATE_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    date_end: Mapped[str | None] = mapped_column(
        String(DATE_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    skills: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)
    technologies: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)
    metrics: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)
    impact_score: Mapped[int] = mapped_column(nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float(), nullable=False, default=DEFAULT_CONFIDENCE)
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingType(), nullable=True, default=None
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("knowledge_entities.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )
    user_verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    content_hash: Mapped[str] = mapped_column(
        String(CONTENT_HASH_LENGTH),
        nullable=False,
        index=True,
    )

    user: Mapped[User] = relationship("User", back_populates="facts")

    source_document: Mapped[KnowledgeDocument | None] = relationship(
        "KnowledgeDocument",
        back_populates="facts",
        foreign_keys=[source_document_id],
    )

    entity: Mapped[KnowledgeEntity | None] = relationship(
        "KnowledgeEntity",
        back_populates="facts",
        foreign_keys=[entity_id],
    )

    # ----------------------------------------------------------------------------------
    # Derived values
    # ----------------------------------------------------------------------------------

    @staticmethod
    def normalize_text(text: str) -> str:
        """Return the matching form of a fact's text.

        Delegates to :func:`normalize_for_matching` so a fact and the entity names inside
        it are reduced identically. Owned by this class rather than by the indexer because
        :meth:`compute_hash` depends on it: if two call sites normalised differently, the
        same claim would produce two hashes and dedupe would silently stop working.

        Args:
            text: The claim as written.

        Returns:
            The value to store in :attr:`normalized_text`.
        """
        return normalize_for_matching(text or "")

    @staticmethod
    def build_content_hash(
        *,
        normalized_text: str,
        organization: str | None = None,
        role: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
    ) -> str:
        """Compute the stable deduplication digest for a fact's identifying fields.

        Exposed as a static method so the indexer can hash an ``ExtractedFact`` *before*
        deciding whether to insert a row, using exactly the arithmetic the stored row will
        use. Stability comes from three choices: a fixed field order, an explicit
        :data:`HASH_FIELD_SEPARATOR` that cannot occur in extracted text (so
        ``("ab", "c")`` and ``("a", "bc")`` cannot collide), and a pinned UTF-8 encoding.
        Organization and role are normalised, dates are only trimmed — their surface form
        is meaningful.

        Args:
            normalized_text: Output of :meth:`normalize_text`.
            organization: Employer/school/project, if any.
            role: Title held, if any.
            date_start: Period start as written, if any.
            date_end: Period end as written, if any.

        Returns:
            A 64-character lowercase SHA-256 hex digest.
        """
        components = (
            normalized_text,
            normalize_for_matching(organization or ""),
            normalize_for_matching(role or ""),
            (date_start or "").strip(),
            (date_end or "").strip(),
        )
        payload = HASH_FIELD_SEPARATOR.join(components).encode(HASH_ENCODING)
        return hashlib.sha256(payload).hexdigest()

    def compute_hash(self) -> str:
        """Return this fact's content hash without mutating the instance.

        Falls back to normalising :attr:`text` when :attr:`normalized_text` has not been
        populated yet, so the method is safe to call on a freshly constructed object.

        Returns:
            A 64-character lowercase SHA-256 hex digest.
        """
        normalized = self.normalized_text or self.normalize_text(self.text or "")
        return self.build_content_hash(
            normalized_text=normalized,
            organization=self.organization,
            role=self.role,
            date_start=self.date_start,
            date_end=self.date_end,
        )

    def refresh_derived_fields(self) -> str:
        """Recompute :attr:`normalized_text` and :attr:`content_hash` from current values.

        Call after constructing or editing a fact so the deduplication key always matches
        the claim it identifies. Keeping this on the model means no caller can forget half
        of the pair.

        Returns:
            The freshly computed :attr:`content_hash`.
        """
        self.normalized_text = self.normalize_text(self.text or "")
        self.content_hash = self.compute_hash()
        return self.content_hash

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        text = loaded.get("text")
        preview = text if not isinstance(text, str) else text[:REPR_TEXT_PREVIEW]
        return (
            f"<KnowledgeFact id={loaded.get('id')!s} kind={loaded.get('kind')!s} "
            f"active={loaded.get('is_active')!r} text={preview!r}>"
        )


class MemoryEntry(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """A remembered correction, outcome, preference, or note — the system's feedback loop.

    Memories are retrieved alongside facts (``RetrievalResult.memories``) so that a later
    generation reflects what the user already taught the system: a phrasing they rejected,
    a company that ghosted them, a stated preference that is not policy. Unlike facts they
    may expire, which is how a transient signal ("do not apply to X this week") stops
    influencing output without being deleted from the audit trail.

    Attributes:
        kind: Category of memory.
        text: The remembered content, in natural language.
        context: Structured provenance — application id, field name, before/after strings.
        embedding: Vector representation for relevance retrieval.
        weight: Multiplier applied to relevance, so an emphatic correction outranks a note.
        expires_at: When this memory stops being retrieved, or ``None`` for permanent.
            Indexed because the maintenance worker sweeps by it.
    """

    __tablename__ = "memory_entries"

    kind: Mapped[MemoryKind] = mapped_column(
        _enum_column_type(MemoryKind, name="memory_kind"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingType(), nullable=True, default=None
    )
    weight: Mapped[float] = mapped_column(Float(), nullable=False, default=DEFAULT_MEMORY_WEIGHT)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None, index=True)

    def is_expired(self, at: datetime | None = None) -> bool:
        """Return whether this memory should no longer be retrieved.

        Args:
            at: Instant to evaluate against. Defaults to now (timezone-aware UTC).

        Returns:
            ``False`` when :attr:`expires_at` is ``None`` (permanent), otherwise whether
            the deadline has passed.
        """
        if self.expires_at is None:
            return False
        moment = at if at is not None else utcnow()
        return self.expires_at <= moment

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        text = loaded.get("text")
        preview = text if not isinstance(text, str) else text[:REPR_TEXT_PREVIEW]
        return (
            f"<MemoryEntry id={loaded.get('id')!s} kind={loaded.get('kind')!s} "
            f"weight={loaded.get('weight')!r} text={preview!r}>"
        )
