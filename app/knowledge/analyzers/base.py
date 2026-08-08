"""The analyzer contract — how any knowledge source becomes structured knowledge.

Every source the user points ApplicantOS at (``docs/CONTRACTS.md`` §8.1) — a GitHub
account, a personal site, a project folder on disk, a resume PDF, a LinkedIn export — is
turned into the same five-part shape by an :class:`Analyzer` plugin:

``documents``
    Extracted plain text, one :class:`ExtractedDocument` per repo, page, or file. These
    become :class:`~app.models.knowledge.KnowledgeDocument` rows and are chunked and
    embedded for semantic retrieval.
``facts``
    Atomic, source-attributed claims about the user. These become
    :class:`~app.models.knowledge.KnowledgeFact` rows — the *only* material a resume may be
    generated from (golden rules #6 and #7).
``entities`` / ``edges``
    Nodes and typed relationships in the user's knowledge graph.
``fingerprint``
    A content digest of everything above, so re-indexing an unchanged source is free.

Two rules make the whole subsystem composable:

* **An analyzer never touches the database.** It receives a :class:`SourceRef` and returns
  an :class:`AnalysisResult` of plain dataclasses. Persistence, chunking, embedding,
  deduplication and graph merging all belong to :mod:`app.knowledge.indexer`. This is what
  makes an analyzer trivially testable and safely runnable in a worker.
* **An analyzer is resolved, never imported** (golden rule #5). Callers ask
  :func:`analyzer_for` or :func:`get_analyzer`, which interrogate
  :mod:`app.plugins.registry`; nothing outside ``app/knowledge/analyzers/`` may import a
  concrete analyzer module.

The module also owns three pieces of shared machinery that would otherwise be re-invented
(inconsistently) by each analyzer:

* :func:`compute_fingerprint` — the stable digest used for change detection.
* :func:`http_client` / :func:`close_http_client` — one polite, redirect-following
  ``httpx.AsyncClient`` per event loop, so a crawl reuses connections and identifies itself
  honestly.
* :func:`chunk_text` — **the** canonical chunker for the entire system. Every embedding in
  ApplicantOS is produced from a window this function emitted, so retrieval behaviour is
  uniform no matter which analyzer produced the text.

``httpx`` is imported lazily inside the functions that need it: the application must import
cleanly on a machine where no analyzer that talks HTTP will ever run.
"""

from __future__ import annotations

import abc
import asyncio
import re
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog

from app.cache.keys import hash_payload
from app.models.enums import EntityKind, FactKind, PluginKind, RelationKind, SourceKind
from app.plugins.base import BasePlugin, PluginMeta, PluginNotFound
from app.plugins.registry import registry

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    import httpx

    from app.config.settings import Settings

__all__ = [
    "CHARS_PER_TOKEN",
    "HTTP_USER_AGENT",
    "MAX_CONFIDENCE",
    "MAX_IMPACT_SCORE",
    "MIN_CONFIDENCE",
    "MIN_IMPACT_SCORE",
    "AnalysisResult",
    "Analyzer",
    "AnalyzerError",
    "ExtractedDocument",
    "ExtractedEdge",
    "ExtractedEntity",
    "ExtractedFact",
    "SourceAccessDenied",
    "SourceRef",
    "SourceUnavailableError",
    "analyzer_for",
    "chunk_text",
    "close_http_client",
    "compute_fingerprint",
    "estimate_tokens",
    "get_analyzer",
    "http_client",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Characters per token in the approximation used everywhere text is measured. Real
#: tokenizers disagree with each other anyway, and a model-specific one would make chunk
#: boundaries depend on which LLM happens to be configured — which would silently
#: invalidate every stored embedding when the user switches provider.
CHARS_PER_TOKEN: Final[int] = 4

#: Smallest window a chunker may be asked for. Below this the notion of a "chunk" stops
#: being meaningful and the packing loop cannot make progress.
MIN_CHUNK_TOKENS: Final[int] = 8

#: Lower/upper bounds for every ``confidence`` field. Values outside are clamped rather
#: than rejected: an extractor emitting 1.2 is expressing certainty, not an error worth
#: failing an entire index run over.
MIN_CONFIDENCE: Final[float] = 0.0
MAX_CONFIDENCE: Final[float] = 1.0

#: Bounds for :attr:`ExtractedFact.impact_score`, mirroring ``KnowledgeFact.impact_score``.
MIN_IMPACT_SCORE: Final[int] = 0
MAX_IMPACT_SCORE: Final[int] = 100

#: Domain-separation tag mixed into the default :meth:`Analyzer.fingerprint` probe. It is
#: what guarantees the default can never collide with a *content* fingerprint, so an
#: analyzer that has not implemented a cheap change probe is always re-analyzed instead of
#: being wrongly skipped. See :meth:`Analyzer.fingerprint`.
IDENTITY_FINGERPRINT_TAG: Final[str] = "analyzer.identity.v1"

#: Domain-separation tag for :meth:`ExtractedDocument.derive_content_hash`.
DOCUMENT_FINGERPRINT_TAG: Final[str] = "knowledge.document.v1"

#: How ApplicantOS identifies itself to every site it fetches. Honest identification is a
#: precondition for polite crawling: a site owner who wants to block us must be able to.
HTTP_USER_AGENT: Final[str] = "ApplicantOS/0.1 (+knowledge-indexer)"

#: Overall request timeout when :class:`~app.config.settings.Settings` declares none.
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Connect-phase timeout. Shorter than the overall budget so an unreachable host fails fast
#: while a slow-but-alive page still gets its full read window.
HTTP_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0

#: Settings fields consulted, in order, for the outbound HTTP timeout. ``docs/CONTRACTS.md``
#: §1 defines no HTTP timeout for the knowledge crawler; this list lets one be added later
#: without touching this module, and falls back to
#: :data:`DEFAULT_HTTP_TIMEOUT_SECONDS` today. Recorded in ``docs/OPEN_QUESTIONS.md``.
HTTP_TIMEOUT_SETTING_NAMES: Final[tuple[str, ...]] = (
    "http_timeout_seconds",
    "website_crawl_timeout_seconds",
)

#: Connection-pool sizing for the shared client. Small on purpose — the crawler is rate
#: limited to roughly one request per second per host and the GitHub analyzer is bounded by
#: the API's own rate limit, so a large pool would buy nothing and look like an attack.
HTTP_MAX_CONNECTIONS: Final[int] = 10
HTTP_MAX_KEEPALIVE_CONNECTIONS: Final[int] = 5

#: Paragraph break: a blank line, plus any further whitespace around it.
_PARAGRAPH_BREAK: Final[re.Pattern[str]] = re.compile(r"\n[ \t]*\n\s*")

#: Sentence break: terminal punctuation (optionally followed by a closing quote or
#: bracket) then whitespace. The lookbehind keeps the punctuation attached to the sentence
#: it ends.
_SENTENCE_BREAK: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?…])[\"'’)\]]*\s+")

#: Line break — the level that matters for bullet lists, which frequently have no terminal
#: punctuation at all and would otherwise be one enormous "sentence".
_LINE_BREAK: Final[re.Pattern[str]] = re.compile(r"\n")

#: Word break, the last structural level before a hard character split.
_WORD_BREAK: Final[re.Pattern[str]] = re.compile(r"[ \t]+")

#: Split levels tried in order by :func:`chunk_text`, coarsest first. Splitting only
#: descends when a unit is still too large, so paragraph and sentence structure survives
#: wherever it fits.
_SPLIT_LEVELS: Final[tuple[re.Pattern[str], ...]] = (
    _PARAGRAPH_BREAK,
    _SENTENCE_BREAK,
    _LINE_BREAK,
    _WORD_BREAK,
)

#: Normalises the three newline conventions to ``\n`` before segmentation, so a chunk
#: boundary never depends on which operating system wrote the file.
_NEWLINE_NORMALIZATION: Final[tuple[tuple[str, str], ...]] = (("\r\n", "\n"), ("\r", "\n"))

#: Sort key standing in for "supports everything", so an analyzer that declares no
#: ``source_kinds`` but overrides :meth:`Analyzer.supports` is considered last rather than
#: first during resolution. See :func:`analyzer_for`.
_UNBOUNDED_SPECIFICITY: Final[int] = 1 << 30


# ======================================================================================
# Errors
# ======================================================================================


class AnalyzerError(Exception):
    """Base class for every failure raised by an analyzer.

    Analyzers are expected to record recoverable problems in
    :attr:`AnalysisResult.errors` and keep going — a crawl that hits one 500 should still
    return the forty pages that worked. This exception hierarchy is for the cases where
    there is nothing to return at all.

    Attributes:
        source: The source that could not be analyzed, when the failure is attributable to
            one.
    """

    def __init__(self, message: str, *, source: SourceRef | None = None) -> None:
        """Record the failure and the source it concerns.

        Args:
            message: Human-readable explanation, surfaced to the operator through
                ``KnowledgeSource.last_error``.
            source: The source being analyzed, if known.
        """
        super().__init__(message)
        self.source = source


class SourceUnavailableError(AnalyzerError):
    """The source could not be reached or no longer exists.

    A deleted repository, a 404, a DNS failure, a project folder the user has since moved.
    Distinct from :class:`SourceAccessDenied` because the remedy is different: this one is
    usually resolved by removing or re-pointing the source, not by supplying a credential.
    """


class SourceAccessDenied(AnalyzerError):
    """The source exists but this installation is not allowed to read it.

    A private repository with no ``github_token``, a 401/403, a login wall, a
    ``robots.txt`` that disallows the path, or a filesystem permission error. The message
    should name the credential or permission that would fix it.
    """


# ======================================================================================
# Fingerprinting
# ======================================================================================


def compute_fingerprint(*parts: Any) -> str:
    """Return a stable SHA-256 digest over *parts*.

    This is the change-detection primitive for the whole knowledge engine. Stability is the
    entire point: the same inputs must produce the same digest in a different process, on a
    different platform, after a restart — otherwise every re-index would look like a change
    and re-embed the user's entire corpus.

    Arity and order are significant (``("a", "b")`` differs from ``("ab",)``), and the
    arguments may be any Python object: mappings are key-sorted, dataclasses and pydantic
    models are dumped, ``Path``/``UUID``/``datetime`` gain canonical string forms, and byte
    strings collapse to their own digest. Delegates to
    :func:`app.cache.keys.hash_payload` so that content addressing is defined exactly once
    in this codebase.

    Args:
        *parts: Everything that identifies the content being fingerprinted.

    Returns:
        A 64-character lowercase hexadecimal digest.
    """
    return hash_payload(list(parts))


def estimate_tokens(text: str) -> int:
    """Return the approximate token length of *text*.

    Uses the system-wide :data:`CHARS_PER_TOKEN` approximation rather than a real
    tokenizer, so the number is identical regardless of which model is configured. Feeds
    ``KnowledgeDocument.token_count``, ``KnowledgeChunk.token_count`` and the chunk packer.

    Args:
        text: The text to measure.

    Returns:
        ``len(text) // CHARS_PER_TOKEN``; zero for text shorter than one token.
    """
    return len(text) // CHARS_PER_TOKEN


def _clamp_confidence(value: float) -> float:
    """Clamp an extractor confidence into ``[0.0, 1.0]``.

    Args:
        value: A confidence from any source, trusted or not.

    Returns:
        The clamped value; :data:`MIN_CONFIDENCE` when *value* is not a real number.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return MIN_CONFIDENCE
    if numeric != numeric:  # NaN is the only value unequal to itself.
        return MIN_CONFIDENCE
    return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, numeric))


def _clamp_impact(value: int) -> int:
    """Clamp an impact score into ``[0, 100]``.

    Args:
        value: A heuristic prominence score from any source.

    Returns:
        The clamped integer; :data:`MIN_IMPACT_SCORE` when *value* is not numeric.
    """
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return MIN_IMPACT_SCORE
    return max(MIN_IMPACT_SCORE, min(MAX_IMPACT_SCORE, numeric))


def _clean_list(values: Any) -> list[str]:
    """Reduce an arbitrary iterable of names to a deduplicated, order-stable string list.

    Args:
        values: Any iterable of name-like objects, or ``None``.

    Returns:
        Trimmed non-empty strings in first-appearance order, case-insensitively deduped.
    """
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


# ======================================================================================
# Data carried between an analyzer and the indexer
# ======================================================================================


@dataclass(slots=True)
class SourceRef:
    """A pointer to something worth indexing — the input to every analyzer.

    The database-free counterpart of :class:`~app.models.knowledge.KnowledgeSource`, so an
    analyzer can be exercised from a test or a script with one line and no session.

    Attributes:
        kind: Which analyzer handles this source.
        uri: URL, absolute filesystem path, or provider-specific identifier (a GitHub
            login, an ``owner/repo`` pair).
        label: Optional human name shown in the desktop app.
        config: Analyzer-specific options — branch, crawl depth, include/exclude globs.
            Copied verbatim from ``KnowledgeSource.config``.
    """

    kind: SourceKind
    uri: str
    label: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Coerce ``kind`` to the enum and normalise the text fields.

        Raises:
            ValueError: If ``kind`` is not a valid :class:`~app.models.enums.SourceKind`.
        """
        self.kind = SourceKind(self.kind)
        self.uri = str(self.uri).strip()
        if self.label is not None:
            self.label = str(self.label).strip() or None
        if self.config is None:  # type: ignore[unreachable]
            self.config = {}

    @property
    def display_name(self) -> str:
        """Human-facing name: :attr:`label` when set, otherwise :attr:`uri`."""
        return self.label or self.uri

    def option(self, name: str, default: Any = None) -> Any:
        """Read one analyzer-specific option out of :attr:`config`.

        Args:
            name: The option key.
            default: Value returned when the key is absent or set to ``None``.

        Returns:
            The configured value, or *default*.
        """
        value = self.config.get(name)
        return default if value is None else value


@dataclass(slots=True)
class ExtractedDocument:
    """One extracted text artifact: a repo, a crawled page, a file, a parsed resume.

    Becomes a :class:`~app.models.knowledge.KnowledgeDocument` row, which is then chunked
    by :func:`chunk_text` and embedded.

    Attributes:
        uri: Location of this artifact *within* the source, unique per source. It is half
            of the ``UNIQUE(source_id, uri)`` key, so re-indexing updates a row rather than
            accumulating duplicates — keep it stable across runs.
        title: Human-facing name (repo name, page title, filename).
        text: Full extracted plain text.
        kind: Provenance classification; may refine the source's own kind (a
            ``project_folder`` source legitimately emits ``readme`` documents).
        metadata: Analyzer-specific structured extras — stars, languages, HTTP headers.
        content_hash: Change-detection digest. Derived from ``uri``, ``title`` and ``text``
            on construction when left empty, so no analyzer can forget it and leave the
            indexer unable to tell an unchanged document from a new one.
    """

    uri: str
    title: str
    text: str
    kind: SourceKind
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""

    def __post_init__(self) -> None:
        """Coerce ``kind``, normalise text fields, and derive ``content_hash`` if unset.

        Raises:
            ValueError: If ``kind`` is not a valid :class:`~app.models.enums.SourceKind`.
        """
        self.kind = SourceKind(self.kind)
        self.uri = str(self.uri).strip()
        self.title = str(self.title).strip()
        self.text = self.text or ""
        if self.metadata is None:  # type: ignore[unreachable]
            self.metadata = {}
        if not self.content_hash:
            self.content_hash = self.derive_content_hash()

    def derive_content_hash(self) -> str:
        """Return the change-detection digest for this document's current content.

        Covers the title and uri as well as the text: a page that keeps its body but gains
        a new title has changed in a way the user will see, and should be re-indexed.

        Returns:
            A 64-character lowercase SHA-256 hex digest.
        """
        return compute_fingerprint(DOCUMENT_FINGERPRINT_TAG, self.uri, self.title, self.text)

    @property
    def token_count(self) -> int:
        """Approximate token length of :attr:`text`, per :func:`estimate_tokens`."""
        return estimate_tokens(self.text)


@dataclass(slots=True)
class ExtractedFact:
    """One atomic, source-attributed claim about the user.

    Becomes a :class:`~app.models.knowledge.KnowledgeFact` row. Golden rule #7 lives here:
    a resume bullet may only ever be a selection or a faithful rewrite of one of these, and
    every one of them traces to a source document.

    Attributes:
        kind: Category of claim.
        text: The claim in the source's own words. Never invented, never embellished.
        skills: Disciplines and capabilities evidenced by the claim ("SLAM", "PCB design").
        technologies: Concrete technologies used ("FreeRTOS", "PyTorch", "STM32").
        metrics: Quantified outcomes quoted verbatim ("cut p95 latency 43%").
        organization: Employer, school, club or project the claim belongs to, if any.
        role: Title held while the claim was true, if any.
        date_start: Period start, normalised to ``YYYY-MM`` or ``YYYY`` when the source was
            explicit enough to allow it, otherwise as written.
        date_end: Period end, or ``None`` for ongoing work.
        impact_score: Heuristic 0-100 prominence used to rank bullets when a resume must
            shrink to one page. Clamped on construction.
        confidence: Extractor confidence in ``[0.0, 1.0]``. Clamped on construction.
        source_uri: The :attr:`ExtractedDocument.uri` this claim came from.
    """

    kind: FactKind
    text: str
    skills: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    organization: str | None = None
    role: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    impact_score: int = 0
    confidence: float = 0.5
    source_uri: str | None = None

    def __post_init__(self) -> None:
        """Coerce ``kind``, deduplicate the name lists, and clamp the numeric fields.

        Raises:
            ValueError: If ``kind`` is not a valid :class:`~app.models.enums.FactKind`.
        """
        self.kind = FactKind(self.kind)
        self.text = (self.text or "").strip()
        self.skills = _clean_list(self.skills)
        self.technologies = _clean_list(self.technologies)
        self.metrics = _clean_list(self.metrics)
        self.impact_score = _clamp_impact(self.impact_score)
        self.confidence = _clamp_confidence(self.confidence)

    @property
    def is_quantified(self) -> bool:
        """Whether this claim carries at least one measured outcome."""
        return bool(self.metrics)


@dataclass(slots=True)
class ExtractedEntity:
    """A node for the user's knowledge graph — a skill, technology, project, employer, …

    Becomes a :class:`~app.models.knowledge.KnowledgeEntity` row, deduplicated on
    ``(user_id, kind, normalized_name)``.

    Attributes:
        kind: Node type.
        name: Display name in the casing the source used. The indexer derives the
            deduplication key from it via ``KnowledgeEntity.normalize``.
        summary: Optional short description.
        attributes: Kind-specific structured extras (proficiency, years, repository URL).
        aliases: Other surface forms observed, which widen retrieval recall.
        confidence: Extractor confidence in ``[0.0, 1.0]``. Clamped on construction.
    """

    kind: EntityKind
    name: str
    summary: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    confidence: float = 0.5

    def __post_init__(self) -> None:
        """Coerce ``kind``, trim the name, deduplicate aliases, and clamp ``confidence``.

        Raises:
            ValueError: If ``kind`` is not a valid :class:`~app.models.enums.EntityKind`.
        """
        self.kind = EntityKind(self.kind)
        self.name = str(self.name).strip()
        if self.summary is not None:
            self.summary = str(self.summary).strip() or None
        if self.attributes is None:  # type: ignore[unreachable]
            self.attributes = {}
        self.aliases = [alias for alias in _clean_list(self.aliases) if alias != self.name]
        self.confidence = _clamp_confidence(self.confidence)

    @property
    def identity(self) -> tuple[EntityKind, str]:
        """The ``(kind, name)`` pair an :class:`ExtractedEdge` endpoint refers to."""
        return (self.kind, self.name)


@dataclass(slots=True)
class ExtractedEdge:
    """A typed, weighted relationship between two entities.

    Becomes a :class:`~app.models.knowledge.KnowledgeEdge` row. Endpoints are
    ``(kind, name)`` pairs rather than ids because an analyzer has no database access; the
    indexer resolves each pair to an entity row, creating it when necessary.

    Attributes:
        source: Subject of the relation, as ``(kind, name)``.
        target: Object of the relation, as ``(kind, name)``.
        relation: Relationship type, read subject-first ("PyTorch *used_in* PoseNet").
        weight: Strength, used to rank graph expansion during retrieval.
        evidence: Provenance — document uri, the quoted span, the extractor's name.
    """

    source: tuple[EntityKind, str]
    target: tuple[EntityKind, str]
    relation: RelationKind
    weight: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Coerce both endpoints and the relation to their enums.

        Raises:
            ValueError: If either endpoint is malformed, or a kind or relation is unknown.
        """
        self.source = _coerce_endpoint(self.source)
        self.target = _coerce_endpoint(self.target)
        self.relation = RelationKind(self.relation)
        self.weight = float(self.weight)
        if self.evidence is None:  # type: ignore[unreachable]
            self.evidence = {}

    @property
    def identity(self) -> tuple[tuple[EntityKind, str], tuple[EntityKind, str], RelationKind]:
        """The triple this edge is unique on, mirroring the table's unique constraint."""
        return (self.source, self.target, self.relation)


def _coerce_endpoint(endpoint: Any) -> tuple[EntityKind, str]:
    """Normalise an edge endpoint into a ``(EntityKind, name)`` tuple.

    Accepts a tuple, a list (which is what JSON round-trips produce), or an
    :class:`ExtractedEntity`, so an analyzer can write the shape that reads best at the
    call site.

    Args:
        endpoint: The endpoint to normalise.

    Returns:
        The canonical endpoint tuple.

    Raises:
        ValueError: If *endpoint* is not a two-element pair, the name is blank, or the kind
            is not a valid :class:`~app.models.enums.EntityKind`.
    """
    if isinstance(endpoint, ExtractedEntity):
        return endpoint.identity
    if not isinstance(endpoint, (tuple, list)) or len(endpoint) != 2:
        raise ValueError(
            "edge endpoint must be a (EntityKind, name) pair or an ExtractedEntity, "
            f"got {endpoint!r}"
        )
    kind, name = endpoint
    resolved_name = str(name).strip()
    if not resolved_name:
        raise ValueError("edge endpoint name must not be empty")
    return (EntityKind(kind), resolved_name)


@dataclass(slots=True)
class AnalysisResult:
    """Everything one analyzer learned from one source.

    The unit of exchange between :class:`Analyzer` and
    :class:`~app.knowledge.indexer.KnowledgeIndexer`. Deliberately additive: an analyzer
    that partly failed returns what it did get plus a line in :attr:`errors`, and the
    indexer persists the good part rather than losing the whole run.

    Attributes:
        documents: Extracted text artifacts.
        facts: Atomic claims about the user.
        entities: Graph nodes.
        edges: Graph relationships.
        fingerprint: Content digest of this analysis, stored on
            ``KnowledgeSource.content_hash`` so the next run can skip unchanged sources.
            An analyzer that leaves this empty is always re-analyzed.
        errors: Human-readable descriptions of recoverable failures — a page that 500'd, a
            file that would not decode. Never used for control flow; the indexer surfaces
            them on ``IndexReport.errors``.
    """

    documents: list[ExtractedDocument] = field(default_factory=list)
    facts: list[ExtractedFact] = field(default_factory=list)
    entities: list[ExtractedEntity] = field(default_factory=list)
    edges: list[ExtractedEdge] = field(default_factory=list)
    fingerprint: str = ""
    errors: list[str] = field(default_factory=list)

    def merge(self, other: AnalysisResult) -> AnalysisResult:
        """Fold *other* into this result, in place.

        The natural accumulator for an analyzer that works page by page or repo by repo::

            result = AnalysisResult()
            for repo in repos:
                result.merge(await self._analyze_repo(repo))
            result.fingerprint = compute_fingerprint(*(d.content_hash for d in result.documents))

        Concatenation is deliberately literal — nothing is deduplicated here, because a
        repeated observation is *evidence* (it drives ``KnowledgeEntity.mention_count`` and
        edge weight) and discarding it would quietly lose signal. Call
        :meth:`deduplicate` when a single flat set is what you want.

        :attr:`fingerprint` is taken from *other* only when this result has none, so an
        outer analyzer that already computed an authoritative digest keeps it.

        Args:
            other: The result to absorb. Left unmodified.

        Returns:
            ``self``, so calls can be chained.
        """
        self.documents.extend(other.documents)
        self.facts.extend(other.facts)
        self.entities.extend(other.entities)
        self.edges.extend(other.edges)
        self.errors.extend(other.errors)
        if not self.fingerprint and other.fingerprint:
            self.fingerprint = other.fingerprint
        return self

    def deduplicate(self) -> AnalysisResult:
        """Collapse repeated documents, entities and edges, in place.

        Documents dedupe on :attr:`ExtractedDocument.uri` (the database is unique on it, so
        keeping two would fail the flush anyway), entities on
        :attr:`ExtractedEntity.identity`, and edges on :attr:`ExtractedEdge.identity`.
        Facts are left untouched: two similarly-worded claims from different periods are
        different facts, and the indexer's content-hash plus cosine dedupe is the component
        qualified to judge that.

        Merging keeps the first occurrence and enriches it: an entity's aliases are unioned
        and the highest confidence wins; an edge's weights are summed and its evidence
        merged, which is exactly the "repeated observation strengthens the edge" behaviour
        ``KnowledgeEdge`` is designed around.

        Returns:
            ``self``, so calls can be chained.
        """
        documents: dict[str, ExtractedDocument] = {}
        for document in self.documents:
            documents.setdefault(document.uri, document)
        self.documents = list(documents.values())

        entities: dict[tuple[EntityKind, str], ExtractedEntity] = {}
        for entity in self.entities:
            existing = entities.get(entity.identity)
            if existing is None:
                entities[entity.identity] = entity
                continue
            existing.aliases = _clean_list([*existing.aliases, *entity.aliases])
            existing.confidence = max(existing.confidence, entity.confidence)
            if existing.summary is None:
                existing.summary = entity.summary
            existing.attributes = {**entity.attributes, **existing.attributes}
        self.entities = list(entities.values())

        edges: dict[tuple[Any, ...], ExtractedEdge] = {}
        for edge in self.edges:
            existing_edge = edges.get(edge.identity)
            if existing_edge is None:
                edges[edge.identity] = edge
                continue
            existing_edge.weight += edge.weight
            existing_edge.evidence = {**edge.evidence, **existing_edge.evidence}
        self.edges = list(edges.values())
        return self

    def counts(self) -> dict[str, int]:
        """Return the size of each collection, for logging and :class:`IndexReport`.

        Returns:
            A mapping with the keys ``documents``, ``facts``, ``entities``, ``edges`` and
            ``errors``.
        """
        return {
            "documents": len(self.documents),
            "facts": len(self.facts),
            "entities": len(self.entities),
            "edges": len(self.edges),
            "errors": len(self.errors),
        }

    def is_empty(self) -> bool:
        """Whether nothing at all was extracted (errors do not count as content)."""
        return not (self.documents or self.facts or self.entities or self.edges)

    def record_error(self, message: str) -> None:
        """Append a recoverable failure description to :attr:`errors`.

        Args:
            message: What went wrong, phrased for the operator reading
                ``KnowledgeSource.last_error`` in the desktop app.
        """
        text = str(message).strip()
        if text:
            self.errors.append(text)

    def content_fingerprint(self) -> str:
        """Compute a fingerprint over everything this result contains.

        The generic fallback for an analyzer with no cheap upstream change probe: it is
        exact (any change to any document, fact, entity or edge changes the digest) but
        only knowable *after* the analysis has run, so it saves re-embedding rather than
        re-fetching. :attr:`errors` are excluded — a transient 500 must not make an
        otherwise-identical crawl look like a content change.

        Returns:
            A 64-character lowercase SHA-256 hex digest.
        """
        return compute_fingerprint(
            [document.content_hash for document in self.documents],
            [(fact.kind.value, fact.text) for fact in self.facts],
            [(entity.kind.value, entity.name) for entity in self.entities],
            [
                (
                    edge.source[0].value,
                    edge.source[1],
                    edge.relation.value,
                    edge.target[0].value,
                    edge.target[1],
                )
                for edge in self.edges
            ],
        )


# ======================================================================================
# The analyzer plugin
# ======================================================================================


class Analyzer(BasePlugin, abc.ABC):
    """Turns one kind of knowledge source into an :class:`AnalysisResult`.

    A registered plugin (``PluginKind.ANALYZER``) resolved through
    :func:`analyzer_for`/:func:`get_analyzer`. Subclasses declare their own ``meta`` and
    the source kinds they handle, and implement :meth:`analyze`::

        @plugin
        class ProjectFolderAnalyzer(Analyzer):
            meta = PluginMeta(kind=PluginKind.ANALYZER, name="project_folder")
            source_kinds = frozenset({SourceKind.PROJECT_FOLDER})

            async def analyze(self, source: SourceRef) -> AnalysisResult:
                ...

    Contract for implementers:

    * **Never touch the database or the filesystem outside the source.** Return plain
      dataclasses; persistence is :mod:`app.knowledge.indexer`'s job.
    * **Degrade, do not explode.** A page that fails belongs in
      :attr:`AnalysisResult.errors`. Raise :class:`SourceUnavailableError` or
      :class:`SourceAccessDenied` only when *nothing* can be produced.
    * **Be idempotent.** Analyzing an unchanged source twice must produce equal
      :attr:`ExtractedDocument.uri` values and an equal
      :attr:`AnalysisResult.fingerprint`, or every re-index will churn the whole corpus.
    * **Set the fingerprint.** :meth:`AnalysisResult.content_fingerprint` is a correct
      default for analyzers with nothing cheaper.

    Attributes:
        source_kinds: The source kinds this analyzer handles. Consulted by the default
            :meth:`supports` and by :func:`analyzer_for` to rank candidates by specificity.
    """

    meta: ClassVar[PluginMeta]
    source_kinds: ClassVar[frozenset[SourceKind]] = frozenset()

    @abc.abstractmethod
    async def analyze(self, source: SourceRef) -> AnalysisResult:
        """Extract everything knowable from *source*.

        Args:
            source: The source to analyze. :meth:`supports` is guaranteed to have returned
                ``True`` for it when the analyzer was obtained through
                :func:`analyzer_for`; an analyzer called directly should check for itself.

        Returns:
            The extracted documents, facts, entities and edges, plus a fingerprint for
            change detection and any recoverable errors.

        Raises:
            SourceUnavailableError: If the source cannot be reached or no longer exists.
            SourceAccessDenied: If reading it requires a credential or permission that is
                not available.
            AnalyzerError: For any other unrecoverable analysis failure.
        """
        raise NotImplementedError

    async def fingerprint(self, source: SourceRef) -> str:
        """Cheaply probe whether *source* has changed, without analyzing it.

        Called before :meth:`analyze`; when the answer equals the stored
        ``KnowledgeSource.content_hash`` the indexer skips the source entirely. A real
        implementation is one HTTP ``HEAD`` (ETag / ``Last-Modified``), one
        ``GET /repos/{owner}/{repo}`` (``pushed_at``), or a walk of directory mtimes —
        always orders of magnitude cheaper than the analysis it might save.

        **The default deliberately never matches.** It digests only the source's *identity*
        under :data:`IDENTITY_FINGERPRINT_TAG`, which no content fingerprint can collide
        with, so an analyzer that has not implemented a probe is always re-analyzed. That
        is the safe direction to fail in: a wasted re-index costs time, whereas a wrongly
        skipped one silently freezes the user's knowledge base — and this engine exists
        precisely so that pushing a commit updates tomorrow's resume.

        Args:
            source: The source to probe.

        Returns:
            A digest comparable with the ``AnalysisResult.fingerprint`` this analyzer
            produces, or an identity-only digest when no cheap probe exists.
        """
        return compute_fingerprint(
            IDENTITY_FINGERPRINT_TAG,
            self.name,
            source.kind.value,
            source.uri,
            source.config,
        )

    def supports(self, source: SourceRef) -> bool:
        """Return whether this analyzer can handle *source*.

        The default answers from :attr:`source_kinds`. Override to inspect the uri as well
        — a ``github_repo`` analyzer that only understands ``github.com`` URLs, or a
        document analyzer that also checks the file extension — and call ``super()`` first
        so the kind check is never skipped.

        Args:
            source: The candidate source.

        Returns:
            ``True`` when :meth:`analyze` may be called with *source*.
        """
        return source.kind in type(self).source_kinds

    def require_supported(self, source: SourceRef) -> None:
        """Raise unless this analyzer supports *source*.

        A guard for the top of :meth:`analyze`, so calling an analyzer directly with the
        wrong source fails loudly instead of producing a confusingly empty result.

        Args:
            source: The source about to be analyzed.

        Raises:
            AnalyzerError: If :meth:`supports` returns ``False``.
        """
        if not self.supports(source):
            supported = ", ".join(sorted(kind.value for kind in type(self).source_kinds))
            raise AnalyzerError(
                f"analyzer {self.name!r} does not support source kind {source.kind.value!r}"
                f" (supports: {supported or 'nothing'})",
                source=source,
            )


# ======================================================================================
# Resolution through the plugin registry
# ======================================================================================


def _registered_analyzers() -> list[Analyzer]:
    """Return every enabled, instantiated analyzer plugin.

    Triggers :func:`app.plugins.loader.load_all` when discovery has not run yet, so a
    script or a test that resolves an analyzer without an application startup still works.
    The import is local to keep the module-import graph acyclic: the loader imports
    ``app.knowledge.analyzers``, which imports this module.

    Returns:
        The analyzer instances, ordered by plugin name.

    Raises:
        PluginLoadError: If an enabled analyzer's constructor raised. Not swallowed —
            silently dropping a broken analyzer would silently narrow the user's knowledge
            base, which is the one thing this subsystem must never do quietly.
    """
    from app.plugins.loader import is_loaded, load_all

    if not is_loaded():
        load_all()
    return [
        candidate
        for candidate in registry.all(PluginKind.ANALYZER)
        if isinstance(candidate, Analyzer)
    ]


def _specificity(analyzer: Analyzer) -> int:
    """Return how narrowly *analyzer* is scoped, for candidate ranking.

    Args:
        analyzer: A candidate analyzer.

    Returns:
        The number of source kinds it declares, or :data:`_UNBOUNDED_SPECIFICITY` when it
        declares none (which means it answers :meth:`Analyzer.supports` some other way and
        should therefore be considered last).
    """
    declared = len(type(analyzer).source_kinds)
    return declared or _UNBOUNDED_SPECIFICITY


def analyzer_for(source: SourceRef) -> Analyzer:
    """Return the analyzer that should handle *source*.

    Every registered analyzer is asked :meth:`Analyzer.supports`. When more than one says
    yes the most specific wins — the one declaring the fewest ``source_kinds`` — with the
    plugin name breaking ties, so resolution is deterministic across runs. That ordering is
    what lets ``DocumentAnalyzer`` act as a catch-all for six source kinds without ever
    shadowing ``ResumeParser`` on ``resume``.

    Args:
        source: The source to analyze.

    Returns:
        The shared instance of the winning analyzer plugin.

    Raises:
        PluginNotFound: If no registered analyzer supports this source. The message lists
            the analyzers that *are* available, which turns a missing-plugin problem into a
            one-line diagnosis.
    """
    candidates = [candidate for candidate in _registered_analyzers() if candidate.supports(source)]
    if not candidates:
        available = ", ".join(registry.names(PluginKind.ANALYZER)) or "none registered"
        raise PluginNotFound(
            f"no analyzer supports source kind {source.kind.value!r} "
            f"(uri={source.uri!r}); available analyzers: {available}",
            kind=PluginKind.ANALYZER,
            name=source.kind.value,
        )
    candidates.sort(key=lambda candidate: (_specificity(candidate), candidate.name.lower()))
    chosen = candidates[0]
    if len(candidates) > 1:
        logger.debug(
            "analyzer.resolved",
            source_kind=source.kind.value,
            chosen=chosen.name,
            candidates=[candidate.name for candidate in candidates],
        )
    return chosen


def get_analyzer(source_kind: SourceKind) -> Analyzer:
    """Return the analyzer that handles *source_kind*.

    The uri-free form of :func:`analyzer_for`, for callers that only know the kind — the
    settings screen listing which sources can be indexed, or a worker routing by kind.
    Resolution goes through :meth:`Analyzer.supports` with a bare probe reference, so an
    analyzer whose ``supports`` additionally inspects the uri may legitimately decline
    here; use :func:`analyzer_for` whenever a real :class:`SourceRef` is available.

    Args:
        source_kind: The kind of source to be analyzed.

    Returns:
        The shared instance of the winning analyzer plugin.

    Raises:
        PluginNotFound: If no registered analyzer handles this kind.
    """
    return analyzer_for(SourceRef(kind=SourceKind(source_kind), uri=""))


# ======================================================================================
# Shared HTTP client
# ======================================================================================

#: One client per event loop. ``httpx`` binds its connection pool to the loop that first
#: uses it, and a Celery worker runs each task on a fresh loop, so a single process-wide
#: client would eventually raise on a loop it was not created for. The mapping is weak on
#: the loop, so a finished loop's entry disappears without anyone remembering to clean up.
_HTTP_CLIENTS: Final[weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]] = (
    weakref.WeakKeyDictionary()
)


def _http_timeout_seconds(settings: Settings) -> float:
    """Return the configured overall HTTP timeout.

    Args:
        settings: Application settings.

    Returns:
        The first positive value found among :data:`HTTP_TIMEOUT_SETTING_NAMES`, otherwise
        :data:`DEFAULT_HTTP_TIMEOUT_SECONDS`.
    """
    for name in HTTP_TIMEOUT_SETTING_NAMES:
        configured = getattr(settings, name, None)
        if isinstance(configured, (int, float)) and configured > 0:
            return float(configured)
    return DEFAULT_HTTP_TIMEOUT_SECONDS


def _running_loop() -> asyncio.AbstractEventLoop:
    """Return the running event loop, with an actionable message when there is none.

    Returns:
        The loop the caller is running on.

    Raises:
        RuntimeError: If called from synchronous context.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError as exc:  # pragma: no cover - programming error, not a runtime path
        raise RuntimeError(
            "http_client() must be called from inside a running event loop; the shared "
            "client is bound to the loop that owns its connection pool"
        ) from exc


def http_client() -> httpx.AsyncClient:
    """Return the shared outbound HTTP client for the current event loop.

    Created on first use and reused afterwards, so a forty-page crawl or a two-hundred-repo
    GitHub scan reuses TLS connections instead of renegotiating each time. The client:

    * identifies itself as :data:`HTTP_USER_AGENT` — honest identification is what lets a
      site owner block us if they want to, which is the precondition for polite crawling;
    * follows redirects, because a portfolio's canonical URL is very often a redirect;
    * uses a connect timeout of :data:`HTTP_CONNECT_TIMEOUT_SECONDS` inside an overall
      budget from settings, so a dead host fails fast while a slow page still completes;
    * keeps a deliberately small connection pool (see :data:`HTTP_MAX_CONNECTIONS`).

    Returns:
        The ``httpx.AsyncClient`` for this loop. Callers must **not** close it — use
        :func:`close_http_client` at shutdown.

    Raises:
        RuntimeError: If called outside a running event loop.
        ImportError: If ``httpx`` is not installed. It is imported lazily so that the
            application starts on an installation where no HTTP-backed analyzer runs.
    """
    import httpx

    loop = _running_loop()
    existing = _HTTP_CLIENTS.get(loop)
    if existing is not None and not existing.is_closed:
        return existing

    from app.config.settings import get_settings

    settings = get_settings()
    overall = _http_timeout_seconds(settings)
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(
            overall,
            connect=min(HTTP_CONNECT_TIMEOUT_SECONDS, overall),
        ),
        limits=httpx.Limits(
            max_connections=HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
        ),
        headers={
            "User-Agent": HTTP_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        },
    )
    _HTTP_CLIENTS[loop] = client
    logger.debug("analyzer.http_client_created", timeout_seconds=overall)
    return client


async def close_http_client() -> None:
    """Close and discard the shared client belonging to the current event loop.

    Call from the application's shutdown path and at the end of a worker task that owns its
    loop. Safe to call when no client was ever created, and safe to call twice. Clients
    bound to *other* loops are left alone — they cannot be closed from here, and the weak
    mapping releases them once their loop is collected.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - defensive; close is always awaited
        return
    client = _HTTP_CLIENTS.pop(loop, None)
    if client is None or client.is_closed:
        return
    await client.aclose()
    logger.debug("analyzer.http_client_closed")


# ======================================================================================
# The canonical chunker
# ======================================================================================


@dataclass(slots=True)
class _Segment:
    """One indivisible piece of text plus the whitespace that preceded it.

    Carrying the separator rather than discarding it is what lets a window be rendered
    with the source's own spacing, so a chunk reads exactly as the document did.

    Attributes:
        separator: Whitespace between the previous segment and this one.
        text: The segment's content, never blank.
        tokens: Cached token estimate, at least one so packing always progresses.
    """

    separator: str
    text: str
    tokens: int


def _split_pieces(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Split *text* on *pattern*, keeping each piece's preceding separator.

    Runs of whitespace that would produce an empty piece are folded into the following
    separator, so no blank segment is ever created.

    Args:
        text: The text to split.
        pattern: The boundary pattern; its match becomes the next piece's separator.

    Returns:
        ``(separator, piece)`` pairs in order. The first pair's separator holds any leading
        whitespace of *text*. Empty when *text* has no content.
    """
    pieces: list[tuple[str, str]] = []
    pending = ""
    cursor = 0
    for match in pattern.finditer(text):
        body = text[cursor : match.start()]
        cursor = match.end()
        if body.strip():
            pieces.append((pending, body))
            pending = match.group(0)
        else:
            pending = f"{pending}{body}{match.group(0)}"
    tail = text[cursor:]
    if tail.strip():
        pieces.append((pending, tail))
    return pieces


def _hard_split(text: str, separator: str, limit: int) -> list[_Segment]:
    """Split an unbreakable run of characters into ``limit``-sized pieces.

    Only reached by a single "word" longer than a whole chunk — a base64 blob, a minified
    line, a URL with an embedded token. Splitting mid-word is wrong but unavoidable; the
    alternative is a chunk that overflows the embedding model's context.

    Args:
        text: The oversized word.
        separator: Whitespace preceding it, kept on the first piece.
        limit: Maximum characters per piece.

    Returns:
        The pieces, in order.
    """
    logger.debug("chunker.hard_split", length=len(text), limit=limit)
    return [
        _Segment(
            separator=separator if offset == 0 else "",
            text=text[offset : offset + limit],
            tokens=max(1, estimate_tokens(text[offset : offset + limit])),
        )
        for offset in range(0, len(text), limit)
    ]


def _segment(text: str, separator: str, limit: int, level: int) -> list[_Segment]:
    """Recursively break *text* into segments no longer than *limit* characters.

    Descends through :data:`_SPLIT_LEVELS` — paragraphs, then sentences, then lines, then
    words — and only as far as necessary, so structure survives wherever it fits.

    Args:
        text: The text to segment.
        separator: Whitespace preceding *text* in the source.
        limit: Maximum characters per segment.
        level: Index into :data:`_SPLIT_LEVELS` to try next.

    Returns:
        The segments, in order.
    """
    body = text.strip()
    if not body:
        return []
    leading = text[: len(text) - len(text.lstrip())]
    effective_separator = f"{separator}{leading}"

    if len(body) <= limit:
        return [_Segment(effective_separator, body, max(1, estimate_tokens(body)))]
    if level >= len(_SPLIT_LEVELS):
        return _hard_split(body, effective_separator, limit)

    pieces = _split_pieces(body, _SPLIT_LEVELS[level])
    if len(pieces) <= 1:
        return _segment(body, effective_separator, limit, level + 1)

    segments: list[_Segment] = []
    for index, (piece_separator, piece) in enumerate(pieces):
        inherited = f"{effective_separator}{piece_separator}" if index == 0 else piece_separator
        segments.extend(_segment(piece, inherited, limit, level + 1))
    return segments


def _render(segments: list[_Segment]) -> str:
    """Join a window of segments back into text.

    The first segment's separator is dropped — it belongs *between* this chunk and the
    previous one, not inside either.

    Args:
        segments: The window, in order.

    Returns:
        The chunk text, stripped.
    """
    return "".join(
        segment.text if index == 0 else f"{segment.separator}{segment.text}"
        for index, segment in enumerate(segments)
    ).strip()


def chunk_text(
    text: str,
    *,
    max_tokens: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """Split *text* into overlapping windows — the one chunker the whole system uses.

    Every embedding in ApplicantOS is produced from a window this function emitted, so
    retrieval behaves the same whether the text came from a GitHub README, a crawled
    portfolio page, or a parsed resume. Nothing else in the codebase may chunk text.

    The algorithm splits on the coarsest boundary that fits and only descends when it must:
    paragraphs, then sentences, then lines, then words. A word is never split unless the
    word alone exceeds a whole chunk. Windows are then packed greedily up to *max_tokens*
    and each new window backs up by roughly *overlap* tokens, so a claim spanning a
    boundary is fully present in at least one window — which is what stops retrieval from
    losing the sentence that mattered.

    Guarantees:

    * No chunk is empty or whitespace-only.
    * No chunk exceeds *max_tokens*, except a single unbreakable word that cannot fit.
    * Concatenating the chunks in order reproduces every word of the source in order (with
      the overlap regions repeated). Only the exact run-length of whitespace at chunk
      boundaries, and the ``\\r\\n``/``\\r`` newline convention, are normalised.

    Args:
        text: The text to chunk. ``None``-like and whitespace-only input yields ``[]``.
        max_tokens: Window size in the :func:`estimate_tokens` approximation. Defaults to
            ``settings.knowledge_chunk_tokens``.
        overlap: Tokens of the previous window repeated at the start of the next. Defaults
            to ``settings.knowledge_chunk_overlap``. Clamped below *max_tokens*, since an
            overlap of a full window could never advance.

    Returns:
        The chunks, in reading order.

    Raises:
        ValueError: If the resolved *max_tokens* is below :data:`MIN_CHUNK_TOKENS`, or the
            resolved *overlap* is negative.
    """
    if not text or not text.strip():
        return []

    if max_tokens is None or overlap is None:
        from app.config.settings import get_settings

        settings = get_settings()
        if max_tokens is None:
            max_tokens = settings.knowledge_chunk_tokens
        if overlap is None:
            overlap = settings.knowledge_chunk_overlap

    if max_tokens < MIN_CHUNK_TOKENS:
        raise ValueError(f"max_tokens must be at least {MIN_CHUNK_TOKENS}, got {max_tokens}")
    if overlap < 0:
        raise ValueError(f"overlap must not be negative, got {overlap}")
    effective_overlap = min(overlap, max_tokens - 1)

    normalized = text
    for source_newline, replacement in _NEWLINE_NORMALIZATION:
        normalized = normalized.replace(source_newline, replacement)

    segments = _segment(normalized, "", max_tokens * CHARS_PER_TOKEN, 0)
    if not segments:
        return []

    chunks: list[str] = []
    total = len(segments)
    start = 0
    while start < total:
        end = start
        budget = 0
        while end < total:
            cost = segments[end].tokens
            if end > start and budget + cost > max_tokens:
                break
            budget += cost
            end += 1

        rendered = _render(segments[start:end])
        if rendered:
            chunks.append(rendered)
        if end >= total:
            break

        # Back up over trailing segments worth at most `effective_overlap` tokens. The
        # `> start + 1` guard is what makes the outer loop terminate: the next window
        # always begins strictly after the current one.
        rewound = end
        carried = 0
        while rewound > start + 1 and carried + segments[rewound - 1].tokens <= effective_overlap:
            carried += segments[rewound - 1].tokens
            rewound -= 1
        start = rewound

    return chunks
