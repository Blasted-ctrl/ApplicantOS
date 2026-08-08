"""The fact store — the rows every resume bullet is generated from, deduplicated twice.

:class:`~app.models.knowledge.KnowledgeFact` replaces the old "master resume bullet"
(``docs/CONTRACTS.md`` §4, §8.3). A resume is a *view* over these rows, every bullet carries
the id of the fact it came from, and golden rule #7 — nothing is fabricated — is checkable
because of it. Which makes this module's real job **keeping the store clean**: a knowledge
base that holds the same accomplishment nine times, once per source that mentioned it, will
put it on a resume nine times.

Deduplication therefore happens on two levels, and they catch different things.

**Exact identity — the content hash.** :func:`~app.knowledge.extractors.fact_content_hash`
digests the normalised text plus organization, role and dates, delegating to
:meth:`~app.models.knowledge.KnowledgeFact.build_content_hash` so the value computed before
the insert is byte-identical to the one the row will store. Re-indexing an unchanged source
a hundred times therefore updates rows rather than multiplying them. This is cheap, exact,
and completely blind to rewording.

**Near identity — cosine similarity.** "…on the quadruped controller, using FreeRTOS" from a
README and "…on the quadruped controller with FreeRTOS" from a resume are the same
accomplishment with different words and different hashes. At cosine ≥
:data:`NEAR_DUPLICATE_THRESHOLD` the incoming claim is *merged into* the incumbent — higher
impact score, union of skills, technologies and metrics, earliest start date, latest end date,
reinforced confidence — instead of inserted. How far apart two wordings may drift and still be
caught depends on the configured embedder; see ``docs/OPEN_QUESTIONS.md`` §49.

Both passes run against a single batched embedding call. :meth:`FactStore.upsert_many`
embeds every candidate in **one** request through :func:`app.ai.embeddings.embed_texts`
(which additionally collapses duplicates and serves cache hits); embedding inside a loop
would turn a re-index from seconds into minutes and is the single easiest way to make this
subsystem unusable.

Everything degrades. With no vector store and no embedder — the zero-API-key install — the
hash pass and an in-batch cosine pass still run, search falls back to SQL keyword matching,
and the product works.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import JSON, ColumnElement, String, and_, func, or_, select, type_coerce

from app.ai.embeddings import content_tokens, cosine
from app.knowledge.extractors import fact_content_hash
from app.knowledge.graph import KnowledgeStore, chunked
from app.knowledge.vector.base import VectorRecord
from app.models.enums import FactKind
from app.models.knowledge import DEFAULT_CONFIDENCE, KnowledgeFact

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.knowledge.analyzers.base import ExtractedFact

__all__ = [
    "FACT_COLLECTION",
    "KEYWORD_OVERFETCH",
    "MAX_KEYWORDS",
    "NEAR_DUPLICATE_PROBE_K",
    "NEAR_DUPLICATE_THRESHOLD",
    "RRF_K",
    "SQL_DEDUPE_SCAN_LIMIT",
    "VECTOR_OVERFETCH",
    "FactStore",
    "reciprocal_rank_fusion",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Vector-store collection holding one record per active fact. Fixed by
#: ``docs/CONTRACTS.md`` §8.2's worked example.
FACT_COLLECTION: Final[str] = "facts"

#: Cosine similarity at or above which two claims are the same claim. Fixed by
#: ``docs/CONTRACTS.md`` §8.3 ("dedupe by content_hash + 0.93 cosine"). High on purpose:
#: merging two genuinely different accomplishments silently destroys evidence, whereas
#: failing to merge two similar ones only leaves a duplicate the user can retire by hand.
NEAR_DUPLICATE_THRESHOLD: Final[float] = 0.93

#: Neighbours fetched per candidate when probing the vector store for a near duplicate.
#: More than one because the nearest hit may be an inactive or foreign row that has to be
#: skipped; far fewer than a search because anything below the threshold is irrelevant.
NEAR_DUPLICATE_PROBE_K: Final[int] = 8

#: Most recent embedded facts scanned when the vector store is unavailable and near
#: duplicate detection has to fall back to a Python cosine sweep. Bounded because that
#: sweep is O(facts × candidates) and the fallback must stay a fallback.
SQL_DEDUPE_SCAN_LIMIT: Final[int] = 2000

#: Reciprocal-rank-fusion constant. 60 is the value from the original Cormack et al. paper
#: and the de-facto standard: large enough that the top few ranks of each list are treated
#: as roughly comparable, small enough that rank still matters.
RRF_K: Final[int] = 60

#: How many extra candidates each retrieval arm fetches before fusion. Fusing two lists
#: truncated at *k* loses documents that rank 41st in one arm and 3rd in the other, which is
#: exactly the case hybrid retrieval exists to catch.
VECTOR_OVERFETCH: Final[int] = 3
KEYWORD_OVERFETCH: Final[int] = 3

#: Most keywords taken from a search query. Past this the ``OR`` chain stops discriminating
#: and starts matching everything.
MAX_KEYWORDS: Final[int] = 12

#: Facts embedded per request by :meth:`FactStore.reembed_missing`.
DEFAULT_REEMBED_BATCH: Final[int] = 64

#: Safety bound on :meth:`FactStore.reembed_missing`'s loop, so a backend that silently
#: refuses to persist embeddings cannot spin forever.
MAX_REEMBED_PASSES: Final[int] = 10_000

#: The literal a JSON column holds when a Python ``None`` is stored in it. See
#: :meth:`FactStore._embedding_absent` — this is the single subtlest thing in the module.
_JSON_NULL_TEXT: Final[str] = "null"

#: ``LIKE`` escape character, and the three characters that need escaping under it.
_LIKE_ESCAPE: Final[str] = "\\"
_LIKE_SPECIALS: Final[tuple[str, ...]] = (_LIKE_ESCAPE, "%", "_")

#: Surface forms meaning "this has not ended", which sort after every real date.
_PRESENT_MARKERS: Final[frozenset[str]] = frozenset(
    {"present", "current", "currently", "ongoing", "now", "to date", "today"}
)

#: Stats keys. ``fact_<kind>`` entries are added for the fact kinds actually present.
STATS_TOTAL: Final[str] = "facts"
STATS_ACTIVE: Final[str] = "facts_active"
STATS_VERIFIED: Final[str] = "facts_verified"
STATS_EMBEDDED: Final[str] = "facts_embedded"
STATS_KIND_PREFIX: Final[str] = "fact_"


# ======================================================================================
# Fusion and text helpers
# ======================================================================================


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[uuid.UUID]], *, k: int = RRF_K
) -> list[uuid.UUID]:
    """Fuse several ranked id lists into one, by reciprocal rank.

    Each list contributes ``1 / (k + rank)`` to every id it contains, where ``rank`` is
    one-based. The method needs no score calibration between the arms, which is what makes
    it the right choice here: a cosine similarity and a keyword match count are not
    comparable numbers, but their *rankings* are.

    Args:
        rankings: The ranked lists, best-first. Empty lists are ignored.
        k: The fusion constant; see :data:`RRF_K`.

    Returns:
        Ids sorted by descending fused score. Ties break on the id's first appearance
        across the input lists, so the result is deterministic.
    """
    scores: dict[uuid.UUID, float] = {}
    first_seen: dict[uuid.UUID, int] = {}
    position = 0
    for ranking in rankings:
        for rank, identifier in enumerate(ranking, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (k + rank)
            if identifier not in first_seen:
                first_seen[identifier] = position
                position += 1
    return sorted(scores, key=lambda item: (-scores[item], first_seen[item]))


def _like_pattern(term: str) -> str:
    """Return a ``LIKE`` pattern matching *term* anywhere, with wildcards escaped.

    Args:
        term: A user-supplied keyword, which may legitimately contain ``%`` or ``_``.

    Returns:
        The pattern, to be used with ``escape="\\\\"``.
    """
    escaped = term
    for special in _LIKE_SPECIALS:
        escaped = escaped.replace(special, f"{_LIKE_ESCAPE}{special}")
    return f"%{escaped}%"


def _date_key(value: str | None) -> tuple[int, int] | None:
    """Return a comparable ``(year, month)`` key for a resume-style date, if parseable.

    Dates are stored exactly as the source wrote them — ``"2024-06"``, ``"2023"``,
    ``"Summer 2023"``, ``"Present"`` — because normalising them would invent precision the
    source never had. Comparison therefore has to be defensive: anything this cannot read
    returns ``None``, and the caller keeps the incumbent rather than guessing.

    Args:
        value: A stored date string, or ``None``.

    Returns:
        ``(year, month)`` with month defaulting to ``0``, or ``None`` when unreadable.
    """
    if not value:
        return None
    text = value.strip()
    if len(text) < 4 or not text[:4].isdigit():
        return None
    year = int(text[:4])
    month = 0
    if len(text) > 5 and text[4] in "-/" and text[5:7].strip("0123456789") == "":
        candidate = text[5:7]
        if candidate.isdigit():
            month = int(candidate)
    return (year, month)


def _is_present(value: str | None) -> bool:
    """Return whether *value* means "still ongoing"."""
    return bool(value) and value.strip().casefold() in _PRESENT_MARKERS


def _earliest_date(incumbent: str | None, incoming: str | None) -> str | None:
    """Return the earlier of two start dates, filling a gap when only one is known.

    Args:
        incumbent: The stored value.
        incoming: The new observation's value.

    Returns:
        The earlier date, or the incumbent when the two cannot be compared.
    """
    if incumbent is None:
        return incoming
    if incoming is None:
        return incumbent
    left, right = _date_key(incumbent), _date_key(incoming)
    if left is None or right is None:
        return incumbent
    return incumbent if left <= right else incoming


def _latest_date(incumbent: str | None, incoming: str | None) -> str | None:
    """Return the later of two end dates, treating "Present" as later than any date.

    Args:
        incumbent: The stored value.
        incoming: The new observation's value.

    Returns:
        The later date, or the incumbent when the two cannot be compared.
    """
    if incumbent is None:
        return incoming
    if incoming is None:
        return incumbent
    if _is_present(incumbent):
        return incumbent
    if _is_present(incoming):
        return incoming
    left, right = _date_key(incumbent), _date_key(incoming)
    if left is None or right is None:
        return incumbent
    return incumbent if left >= right else incoming


def _union(*groups: Iterable[str]) -> list[str]:
    """Merge string lists, preserving first-appearance order and folding case.

    Args:
        *groups: The lists to merge, most authoritative first.

    Returns:
        A new list. Always a new object, because the ``skills`` / ``technologies`` /
        ``metrics`` columns are plain JSON with no mutation tracking — an in-place
        ``append`` would never reach the database.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for value in group or ():
            text = str(value).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


# ======================================================================================
# The merge unit
# ======================================================================================


@dataclass(slots=True)
class _Draft:
    """A fact's mergeable content, decoupled from whether it is a row yet.

    The single place merge semantics live. An incoming
    :class:`~app.knowledge.analyzers.base.ExtractedFact`, an existing
    :class:`~app.models.knowledge.KnowledgeFact` row, and another draft are all reduced to
    this shape, so "merge two facts" is written once and used by all three deduplication
    paths (in-batch exact, stored exact, near-duplicate) rather than three times with
    subtly different rules.

    Attributes:
        kind: Category of claim.
        text: The claim in the source's own words.
        organization: Employer, school or project, if any.
        role: Title held, if any.
        location: Where it happened, if known. Never present on an extracted fact — it only
            survives a round trip through an existing row.
        date_start: Period start, as written.
        date_end: Period end, as written.
        skills: Disciplines evidenced.
        technologies: Concrete technologies used.
        metrics: Quantified outcomes, quoted verbatim.
        impact_score: Heuristic 0-100 prominence.
        confidence: Extractor confidence.
        content_hash: The deduplication digest of the identifying fields above.
        source_uri: Provenance, for logging.
    """

    kind: FactKind
    text: str
    organization: str | None = None
    role: str | None = None
    location: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    skills: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    impact_score: int = 0
    confidence: float = DEFAULT_CONFIDENCE
    content_hash: str = ""
    source_uri: str | None = None

    @classmethod
    def from_extracted(cls, fact: ExtractedFact) -> _Draft:
        """Build a draft from an analyzer's output.

        Args:
            fact: The extracted claim. Its ``__post_init__`` has already trimmed the text,
                deduplicated the name lists and clamped the numbers.

        Returns:
            The draft, with :attr:`content_hash` computed by
            :func:`~app.knowledge.extractors.fact_content_hash` — the same arithmetic the
            stored row will use.
        """
        return cls(
            kind=FactKind(fact.kind),
            text=fact.text,
            organization=fact.organization,
            role=fact.role,
            location=None,
            date_start=fact.date_start,
            date_end=fact.date_end,
            skills=list(fact.skills),
            technologies=list(fact.technologies),
            metrics=list(fact.metrics),
            impact_score=int(fact.impact_score),
            confidence=float(fact.confidence),
            content_hash=fact_content_hash(fact),
            source_uri=fact.source_uri,
        )

    @classmethod
    def from_row(cls, row: KnowledgeFact) -> _Draft:
        """Build a draft from a stored fact.

        Args:
            row: The stored fact.

        Returns:
            The draft mirroring its current content.
        """
        return cls(
            kind=FactKind(row.kind),
            text=row.text,
            organization=row.organization,
            role=row.role,
            location=row.location,
            date_start=row.date_start,
            date_end=row.date_end,
            skills=list(row.skills or []),
            technologies=list(row.technologies or []),
            metrics=list(row.metrics or []),
            impact_score=int(row.impact_score or 0),
            confidence=float(row.confidence or 0.0),
            content_hash=row.content_hash,
        )

    def merge(self, other: _Draft) -> _Draft:
        """Fold *other* into this draft, in place, and return it.

        The incumbent — ``self`` — owns identity. Its wording, kind, organization and role
        survive; a claim does not change what it says because a second source phrased it
        differently. What merges is everything *additive*, exactly as
        ``docs/CONTRACTS.md`` §8.3 specifies:

        * ``impact_score`` takes the maximum, so the strongest reading of the claim ranks it;
        * ``skills`` / ``technologies`` / ``metrics`` union, because two sources describing
          one accomplishment usually each know something the other did not;
        * ``date_start`` takes the earliest and ``date_end`` the latest, widening the period
          to everything the sources agree happened;
        * ``confidence`` is raised by
          :meth:`~app.knowledge.graph.KnowledgeStore.reinforce_confidence` — being seen
          twice is evidence;
        * ``organization`` / ``role`` / ``location`` fill only when the incumbent has none.

        Args:
            other: The claim being folded in. Left unmodified.

        Returns:
            ``self``, so calls chain.
        """
        self.organization = self.organization or other.organization
        self.role = self.role or other.role
        self.location = self.location or other.location
        self.date_start = _earliest_date(self.date_start, other.date_start)
        self.date_end = _latest_date(self.date_end, other.date_end)
        self.skills = _union(self.skills, other.skills)
        self.technologies = _union(self.technologies, other.technologies)
        self.metrics = _union(self.metrics, other.metrics)
        self.impact_score = max(self.impact_score, other.impact_score)
        self.confidence = KnowledgeStore.reinforce_confidence(self.confidence, other.confidence)
        self.source_uri = self.source_uri or other.source_uri
        return self

    def apply_to(self, row: KnowledgeFact, *, freeze_identity: bool) -> None:
        """Write this draft onto *row* and refresh its derived columns.

        Args:
            row: The row to update.
            freeze_identity: When ``True``, the identifying fields — text, organization,
                role, location and both dates — and therefore ``content_hash`` are left
                exactly as they are, and only the additive fields are written. Set for a
                ``user_verified`` fact, which
                :class:`~app.models.knowledge.KnowledgeFact` documents as never silently
                rewritten by a later index.
        """
        row.skills = list(self.skills)
        row.technologies = list(self.technologies)
        row.metrics = list(self.metrics)
        row.impact_score = int(self.impact_score)
        row.confidence = float(self.confidence)

        if freeze_identity:
            return

        row.kind = self.kind
        row.text = self.text
        row.organization = self.organization
        row.role = self.role
        row.location = self.location
        row.date_start = self.date_start
        row.date_end = self.date_end
        # Recompute `normalized_text` and `content_hash` together: merging may have widened
        # the date range, which changes the row's identity, and a stale hash would silently
        # stop deduplicating this claim.
        self.content_hash = row.refresh_derived_fields()

    def to_row(
        self,
        *,
        user_id: uuid.UUID,
        source_document_id: uuid.UUID | None,
        embedding: list[float] | None,
    ) -> KnowledgeFact:
        """Build a brand-new fact row from this draft.

        Args:
            user_id: Owning user.
            source_document_id: Document this claim was extracted from, if known.
            embedding: The claim's vector, when one could be computed.

        Returns:
            An unsaved :class:`~app.models.knowledge.KnowledgeFact` with its derived
            columns already populated.
        """
        row = KnowledgeFact(
            user_id=user_id,
            kind=self.kind,
            text=self.text,
            organization=self.organization,
            role=self.role,
            location=self.location,
            date_start=self.date_start,
            date_end=self.date_end,
            skills=list(self.skills),
            technologies=list(self.technologies),
            metrics=list(self.metrics),
            impact_score=int(self.impact_score),
            confidence=float(self.confidence),
            embedding=embedding,
            source_document_id=source_document_id,
        )
        self.content_hash = row.refresh_derived_fields()
        return row


# ======================================================================================
# The store
# ======================================================================================


class FactStore(KnowledgeStore):
    """Persist, deduplicate, search and maintain a user's knowledge facts.

    Scoped to one ``user_id`` on every method; facts never cross users. Like every store in
    this package it flushes but never commits, so a service can compose a whole indexing
    pass into one transaction.
    """

    # ----------------------------------------------------------------------------------
    # "Has this fact been embedded?" — harder than it looks
    # ----------------------------------------------------------------------------------

    def _embedding_is_json(self) -> bool:
        """Return whether the ``embedding`` column resolves to JSON on this connection.

        :class:`~app.database.types.EmbeddingType` is a ``TypeDecorator`` that becomes a
        pgvector ``vector`` column on PostgreSQL (when the extension package is importable)
        and a plain ``JSON`` column everywhere else — including on PostgreSQL without
        pgvector. The two behave differently for ``None``, which is what
        :meth:`_embedding_absent` exists to paper over, so the predicate has to know which
        one it is talking to. It is answered by asking the *bound dialect* rather than by
        checking the dialect's name, because the name alone does not determine the answer.

        Returns:
            ``True`` when a missing embedding is stored as JSON ``null``.
        """
        dialect = self.session.get_bind().dialect
        implementation = KnowledgeFact.__table__.c.embedding.type.load_dialect_impl(dialect)
        return isinstance(implementation, JSON)

    def _embedding_absent(self) -> ColumnElement[bool]:
        """Return the predicate matching facts that have **no** usable embedding.

        ``embedding IS NULL`` is not sufficient, and the reason is worth stating plainly:
        SQLAlchemy's ``JSON`` type persists a Python ``None`` as the JSON literal ``null``
        — the four-character string — not as SQL ``NULL``, unless the column was declared
        ``none_as_null=True``. On the SQLite install, therefore, **every unembedded fact has
        a non-NULL ``embedding`` column**, and the obvious query silently matches nothing:
        the embedding backlog would report "nothing to do" forever while never embedding a
        single row. On pgvector the column is a real ``vector`` and ``None`` really is SQL
        ``NULL``, so both spellings are needed and each is used only where it is valid.

        Returns:
            A predicate true for rows with no vector.
        """
        if self._embedding_is_json():
            return or_(
                KnowledgeFact.embedding.is_(None),
                type_coerce(KnowledgeFact.embedding, String) == _JSON_NULL_TEXT,
            )
        return KnowledgeFact.embedding.is_(None)

    def _embedding_present(self) -> ColumnElement[bool]:
        """Return the predicate matching facts that **do** have a usable embedding.

        The exact complement of :meth:`_embedding_absent`; see there for why the naive
        ``IS NOT NULL`` would count every unembedded row as embedded.

        Returns:
            A predicate true for rows with a vector.
        """
        if self._embedding_is_json():
            return and_(
                KnowledgeFact.embedding.is_not(None),
                type_coerce(KnowledgeFact.embedding, String) != _JSON_NULL_TEXT,
            )
        return KnowledgeFact.embedding.is_not(None)

    # ----------------------------------------------------------------------------------
    # Ingest
    # ----------------------------------------------------------------------------------

    async def upsert_many(
        self,
        user_id: uuid.UUID,
        facts: list[ExtractedFact],
        *,
        source_document_id: uuid.UUID | None = None,
    ) -> int:
        """Store *facts*, merging anything that already exists rather than duplicating it.

        Four passes, in this order, each catching what the previous one cannot:

        1. **Hash within the batch.** Identical claims extracted twice from one document
           collapse before any I/O happens.
        2. **Hash against the store.** A claim whose identifying fields are unchanged since
           the last index updates its row in place.
        3. **Cosine within the batch.** Two rewordings of one accomplishment that arrive
           together — the resume's version and the README's version — merge into one row.
           This pass needs no vector store at all, only the embeddings computed in step 3's
           single batched call, which is why it works on a bare offline install.
        4. **Cosine against the store.** A reworded claim merges into the row an earlier
           index created, via the vector store, or via a bounded Python sweep of the user's
           most recent embedded facts when there is no vector store.

        All embeddings for the batch are produced by **one** call to
        :meth:`~app.knowledge.graph.KnowledgeStore.embed`. Nothing in this method embeds
        inside a loop.

        Args:
            user_id: Owning user.
            facts: The extracted claims, in document order.
            source_document_id: Document they came from, recorded as provenance on newly
                inserted rows.

        Returns:
            How many of *facts* reached the store — inserted as new rows plus merged into
            existing ones. Blank claims are skipped and not counted.
        """
        if not facts:
            return 0

        drafts = self._collapse_batch(facts)
        if not drafts:
            return 0

        merged = 0
        pending: list[_Draft] = []
        stored = await self._rows_by_hash(user_id, [draft.content_hash for draft in drafts])
        for draft in drafts:
            row = stored.get(draft.content_hash)
            if row is None:
                pending.append(draft)
                continue
            self._merge_into_row(row, draft)
            merged += 1

        vectors = await self.embed([draft.text for draft in pending]) if pending else []
        if vectors is None or len(vectors) != len(pending):
            if vectors is not None and pending:
                logger.warning(
                    "facts.embedding_arity_mismatch",
                    expected=len(pending),
                    received=len(vectors),
                )
            vectors = []

        survivors = self._collapse_near_duplicates(pending, vectors)
        if vectors:
            merged += await self._merge_near_duplicates(user_id, survivors)

        inserted = await self._insert(user_id, survivors, source_document_id=source_document_id)
        await self.session.flush()
        await self._store_vectors(user_id, inserted)

        logger.info(
            "facts.upserted",
            user_id=str(user_id),
            received=len(facts),
            inserted=len(inserted),
            merged=merged,
            source_document_id=str(source_document_id) if source_document_id else None,
        )
        return len(inserted) + merged

    @staticmethod
    def _collapse_batch(facts: Sequence[ExtractedFact]) -> list[_Draft]:
        """Reduce a batch to one draft per content hash, in first-appearance order.

        Args:
            facts: The extracted claims.

        Returns:
            The collapsed drafts. Claims with no text are dropped — an empty bullet is an
            extractor artifact, never a fact.
        """
        collapsed: dict[str, _Draft] = {}
        for fact in facts:
            if not (fact.text or "").strip():
                continue
            draft = _Draft.from_extracted(fact)
            existing = collapsed.get(draft.content_hash)
            if existing is None:
                collapsed[draft.content_hash] = draft
            else:
                existing.merge(draft)
        return list(collapsed.values())

    async def _rows_by_hash(
        self, user_id: uuid.UUID, hashes: Sequence[str]
    ) -> dict[str, KnowledgeFact]:
        """Load the user's facts whose content hash is in *hashes*.

        ``content_hash`` is indexed but not unique — a merge that widens a date range
        rewrites one, so two rows can in principle converge on the same digest. The winner
        is the active, user-verified, oldest row, which is the one a human is most likely to
        have curated.

        Args:
            user_id: Owning user.
            hashes: The digests to look for.

        Returns:
            Digest to row, for the digests that exist.
        """
        found: dict[str, KnowledgeFact] = {}
        for chunk in chunked(list(dict.fromkeys(hashes))):
            statement = (
                select(KnowledgeFact)
                .where(
                    KnowledgeFact.user_id == user_id,
                    KnowledgeFact.content_hash.in_(chunk),
                )
                .order_by(
                    KnowledgeFact.is_active.desc(),
                    KnowledgeFact.user_verified.desc(),
                    KnowledgeFact.created_at.asc(),
                )
            )
            for row in (await self.session.execute(statement)).scalars().all():
                found.setdefault(row.content_hash, row)
        return found

    @staticmethod
    def _merge_into_row(row: KnowledgeFact, draft: _Draft) -> None:
        """Merge *draft* into an existing row, honouring human verification.

        Args:
            row: The incumbent row, which owns identity.
            draft: The incoming claim.
        """
        _Draft.from_row(row).merge(draft).apply_to(row, freeze_identity=bool(row.user_verified))

    @staticmethod
    def _collapse_near_duplicates(
        drafts: list[_Draft], vectors: Sequence[Sequence[float]]
    ) -> list[tuple[_Draft, list[float]]]:
        """Merge drafts that are near duplicates *of each other*.

        Runs before anything touches the vector store, using only the embeddings already
        computed for the batch — which is what makes near-duplicate detection work on an
        install with no vector backend at all.

        Args:
            drafts: The candidate drafts, in order.
            vectors: Their embeddings, positionally aligned. Empty when embedding was
                unavailable, in which case every draft survives.

        Returns:
            ``(draft, vector)`` pairs for the survivors, in order. The vector list is empty
            for each pair when embeddings were unavailable.
        """
        if not vectors:
            return [(draft, []) for draft in drafts]

        survivors: list[tuple[_Draft, list[float]]] = []
        for draft, vector in zip(drafts, vectors, strict=True):
            candidate = list(vector)
            winner: _Draft | None = None
            best = NEAR_DUPLICATE_THRESHOLD
            for kept, kept_vector in survivors:
                if len(kept_vector) != len(candidate):
                    continue
                score = cosine(kept_vector, candidate)
                if score >= best:
                    best = score
                    winner = kept
            if winner is None:
                survivors.append((draft, candidate))
                continue
            winner.merge(draft)
            logger.debug(
                "facts.near_duplicate_in_batch",
                similarity=round(best, 4),
                kept=winner.text[:80],
                dropped=draft.text[:80],
            )
        return survivors

    async def _merge_near_duplicates(
        self, user_id: uuid.UUID, survivors: list[tuple[_Draft, list[float]]]
    ) -> int:
        """Merge surviving drafts into stored facts they are near duplicates of.

        Mutates *survivors* in place, removing every draft that found a home.

        Args:
            user_id: Owning user.
            survivors: ``(draft, vector)`` pairs from :meth:`_collapse_near_duplicates`.

        Returns:
            How many drafts were merged into an existing row.
        """
        if not survivors:
            return 0

        fallback = await self._embedded_facts(user_id) if self.vector_store is None else None
        merged = 0
        remaining: list[tuple[_Draft, list[float]]] = []

        for draft, vector in survivors:
            if not vector:
                remaining.append((draft, vector))
                continue
            row, score = (
                await self._probe_vector_store(user_id, vector)
                if fallback is None
                else self._probe_in_python(vector, fallback)
            )
            if row is None:
                remaining.append((draft, vector))
                continue
            self._merge_into_row(row, draft)
            merged += 1
            logger.debug(
                "facts.near_duplicate_stored",
                user_id=str(user_id),
                similarity=round(score, 4),
                fact_id=str(row.id),
            )

        survivors[:] = remaining
        return merged

    async def _probe_vector_store(
        self, user_id: uuid.UUID, vector: Sequence[float]
    ) -> tuple[KnowledgeFact | None, float]:
        """Ask the vector store for a stored near duplicate of *vector*.

        Args:
            user_id: Owning user.
            vector: The candidate's embedding.

        Returns:
            ``(row, score)`` for the best hit at or above
            :data:`NEAR_DUPLICATE_THRESHOLD`, or ``(None, 0.0)``.
        """
        hits = await self.vector_query(
            FACT_COLLECTION,
            vector,
            k=NEAR_DUPLICATE_PROBE_K,
            filters={"user_id": str(user_id)},
        )
        for hit in hits:
            if hit.score < NEAR_DUPLICATE_THRESHOLD:
                break
            identifier = _as_uuid(hit.id)
            if identifier is None:
                continue
            row = await self.session.get(KnowledgeFact, identifier)
            if row is not None and row.user_id == user_id and row.is_active:
                return row, hit.score
        return None, 0.0

    @staticmethod
    def _probe_in_python(
        vector: Sequence[float], candidates: Sequence[tuple[KnowledgeFact, list[float]]]
    ) -> tuple[KnowledgeFact | None, float]:
        """Find a stored near duplicate by brute-force cosine, for the no-vector-store path.

        Args:
            vector: The candidate's embedding.
            candidates: ``(row, embedding)`` pairs to compare against, already bounded by
                :data:`SQL_DEDUPE_SCAN_LIMIT`.

        Returns:
            ``(row, score)`` for the best hit at or above
            :data:`NEAR_DUPLICATE_THRESHOLD`, or ``(None, 0.0)``.
        """
        best_row: KnowledgeFact | None = None
        best_score = NEAR_DUPLICATE_THRESHOLD
        for row, embedding in candidates:
            if len(embedding) != len(vector):
                continue
            score = cosine(embedding, vector)
            if score >= best_score:
                best_score = score
                best_row = row
        return (best_row, best_score) if best_row is not None else (None, 0.0)

    async def _embedded_facts(self, user_id: uuid.UUID) -> list[tuple[KnowledgeFact, list[float]]]:
        """Load the user's most recent embedded active facts for the Python sweep.

        Args:
            user_id: Owning user.

        Returns:
            ``(row, embedding)`` pairs, newest first, at most
            :data:`SQL_DEDUPE_SCAN_LIMIT` of them.
        """
        statement = (
            select(KnowledgeFact)
            .where(
                KnowledgeFact.user_id == user_id,
                KnowledgeFact.is_active.is_(True),
                self._embedding_present(),
            )
            .order_by(KnowledgeFact.created_at.desc())
            .limit(SQL_DEDUPE_SCAN_LIMIT)
        )
        rows = (await self.session.execute(statement)).scalars().all()
        return [(row, list(row.embedding or [])) for row in rows]

    async def _insert(
        self,
        user_id: uuid.UUID,
        survivors: Sequence[tuple[_Draft, list[float]]],
        *,
        source_document_id: uuid.UUID | None,
    ) -> list[KnowledgeFact]:
        """Insert the drafts that found no home as new rows.

        Args:
            user_id: Owning user.
            survivors: ``(draft, vector)`` pairs to insert.
            source_document_id: Provenance for the new rows.

        Returns:
            The inserted rows, flushed so their ids exist.
        """
        if not survivors:
            return []
        rows = [
            draft.to_row(
                user_id=user_id,
                source_document_id=source_document_id,
                embedding=list(vector) if vector else None,
            )
            for draft, vector in survivors
        ]
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def _store_vectors(self, user_id: uuid.UUID, rows: Sequence[KnowledgeFact]) -> int:
        """Write the vector records for *rows*, skipping those without an embedding.

        Args:
            user_id: Owning user.
            rows: Freshly written facts.

        Returns:
            The number of vector records written.
        """
        records = [_vector_record(row, user_id) for row in rows if row.embedding is not None]
        return await self.vector_upsert(FACT_COLLECTION, records) if records else 0

    # ----------------------------------------------------------------------------------
    # Retrieval
    # ----------------------------------------------------------------------------------

    async def search(
        self,
        user_id: uuid.UUID,
        query: str,
        *,
        k: int = 40,
        kinds: Sequence[FactKind] | None = None,
    ) -> list[KnowledgeFact]:
        """Return the user's active facts most relevant to *query*.

        Hybrid by design. The vector arm finds claims that mean the same thing as the query
        without sharing a word — the case that matters when a job description says
        "real-time embedded control" and the fact says "tuned a 1 kHz PID loop on an STM32".
        The keyword arm finds exact tokens the embedder may have smoothed over: a product
        name, an acronym, an employer. Neither alone is sufficient, and their scores are not
        comparable, so the two rankings are fused by
        :func:`reciprocal_rank_fusion` rather than by blending numbers.

        With no embedder or no vector store the vector arm simply contributes nothing and
        the keyword arm answers alone.

        Args:
            user_id: Owning user.
            query: Free text — a job description, a section heading, a question.
            k: Maximum facts to return.
            kinds: Restrict to these fact kinds; ``None`` searches all.

        Returns:
            At most *k* active facts, most relevant first.
        """
        if k < 1 or not (query or "").strip():
            return []

        resolved_kinds = [FactKind(kind) for kind in kinds] if kinds else None

        vector_ids: list[uuid.UUID] = []
        embedded = await self.embed([query])
        if embedded:
            filters: dict[str, Any] = {"user_id": str(user_id)}
            if resolved_kinds:
                filters["kind"] = [kind.value for kind in resolved_kinds]
            hits = await self.vector_query(
                FACT_COLLECTION,
                embedded[0],
                k=k * VECTOR_OVERFETCH,
                filters=filters,
            )
            vector_ids = [
                identifier
                for identifier in (_as_uuid(hit.id) for hit in hits)
                if identifier is not None
            ]

        keyword_ids = await self._keyword_ids(user_id, query, k=k, kinds=resolved_kinds)
        fused = reciprocal_rank_fusion([vector_ids, keyword_ids])
        rows = await self.by_ids(fused)

        # The ownership check is repeated here on purpose. The keyword arm filters in SQL
        # and the vector arm filters on metadata, but a stale record left in the index by an
        # older schema would carry the wrong `user_id` — and one person's facts appearing in
        # another person's resume is the worst failure this system could have.
        results = [
            row
            for row in rows
            if row.user_id == user_id
            and row.is_active
            and (resolved_kinds is None or FactKind(row.kind) in resolved_kinds)
        ][:k]

        logger.debug(
            "facts.search",
            user_id=str(user_id),
            k=k,
            vector_hits=len(vector_ids),
            keyword_hits=len(keyword_ids),
            returned=len(results),
        )
        return results

    async def _keyword_ids(
        self,
        user_id: uuid.UUID,
        query: str,
        *,
        k: int,
        kinds: Sequence[FactKind] | None,
    ) -> list[uuid.UUID]:
        """Return fact ids matching *query*'s keywords, best first.

        The prefilter is a plain ``OR`` of substring matches over the normalised text, the
        organization and the role — cheap and indexable-adjacent. Ranking then happens in
        Python over the fetched rows, by how many distinct keywords the row actually
        contains and then by impact, which is a far better ordering than ``OR`` can express
        in portable SQL and costs nothing on a bounded result set.

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
        if not keywords:
            return []

        conditions = []
        for keyword in keywords:
            pattern = _like_pattern(keyword)
            conditions.append(KnowledgeFact.normalized_text.like(pattern, escape=_LIKE_ESCAPE))
            conditions.append(
                func.lower(KnowledgeFact.organization).like(pattern, escape=_LIKE_ESCAPE)
            )
            conditions.append(func.lower(KnowledgeFact.role).like(pattern, escape=_LIKE_ESCAPE))

        statement = select(KnowledgeFact).where(
            KnowledgeFact.user_id == user_id,
            KnowledgeFact.is_active.is_(True),
            or_(*conditions),
        )
        if kinds:
            statement = statement.where(KnowledgeFact.kind.in_(list(kinds)))
        statement = statement.order_by(
            KnowledgeFact.impact_score.desc(),
            KnowledgeFact.confidence.desc(),
            KnowledgeFact.created_at.asc(),
        ).limit(max(k * KEYWORD_OVERFETCH, k))

        rows = list((await self.session.execute(statement)).scalars().all())
        rows.sort(
            key=lambda row: (
                -_keyword_overlap(row, keywords),
                -int(row.impact_score or 0),
                -float(row.confidence or 0.0),
                str(row.id),
            )
        )
        return [row.id for row in rows]

    async def active(self, user_id: uuid.UUID) -> list[KnowledgeFact]:
        """Return every active fact the user has, strongest first.

        The query the resume engine runs on every tailoring pass, served by the
        ``(user_id, is_active)`` index.

        Args:
            user_id: Owning user.

        Returns:
            The active facts, ordered by descending impact score, then confidence, then
            creation time.
        """
        statement = (
            select(KnowledgeFact)
            .where(
                KnowledgeFact.user_id == user_id,
                KnowledgeFact.is_active.is_(True),
            )
            .options(*_LOAD_OPTIONS)
            .order_by(
                KnowledgeFact.impact_score.desc(),
                KnowledgeFact.confidence.desc(),
                KnowledgeFact.created_at.asc(),
            )
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def by_ids(self, ids: Sequence[uuid.UUID]) -> list[KnowledgeFact]:
        """Load facts by id, preserving the order they were asked for.

        Order preservation is what makes this usable as the second half of a ranked
        retrieval: the caller ranks ids, this returns rows in that ranking.

        Args:
            ids: The fact ids. Duplicates collapse; unknown ids are silently absent.

        Returns:
            The rows, in *ids* order, with ``entity`` and ``source_document`` eagerly
            loaded so a caller can read them without triggering lazy IO in async context.
        """
        unique = list(dict.fromkeys(ids))
        if not unique:
            return []
        found: dict[uuid.UUID, KnowledgeFact] = {}
        for chunk in chunked(unique):
            statement = (
                select(KnowledgeFact).where(KnowledgeFact.id.in_(chunk)).options(*_LOAD_OPTIONS)
            )
            for row in (await self.session.execute(statement)).scalars().all():
                found[row.id] = row
        return [found[identifier] for identifier in unique if identifier in found]

    # ----------------------------------------------------------------------------------
    # Curation
    # ----------------------------------------------------------------------------------

    async def deactivate(self, fact_id: uuid.UUID) -> KnowledgeFact | None:
        """Retire a fact from generation without deleting the evidence.

        The row stays — provenance and audit trail are the point of this table — but it
        stops being retrievable, and its vector record is removed so semantic search cannot
        surface it either.

        Args:
            fact_id: The fact to retire.

        Returns:
            The updated row, or ``None`` when no such fact exists.
        """
        row = await self.session.get(KnowledgeFact, fact_id)
        if row is None:
            return None
        row.is_active = False
        await self.session.flush()
        await self.vector_delete(FACT_COLLECTION, [str(fact_id)])
        logger.info("facts.deactivated", fact_id=str(fact_id))
        return row

    async def verify(self, fact_id: uuid.UUID, verified: bool) -> KnowledgeFact | None:
        """Mark a fact as human-confirmed, or withdraw that confirmation.

        A verified fact's identifying fields are frozen: later indexing runs may still
        enrich its skills, technologies and metrics, but they will not rewrite what it says
        (see :meth:`_Draft.apply_to`).

        Args:
            fact_id: The fact to update.
            verified: The new verification state.

        Returns:
            The updated row, or ``None`` when no such fact exists.
        """
        row = await self.session.get(KnowledgeFact, fact_id)
        if row is None:
            return None
        row.user_verified = bool(verified)
        await self.session.flush()
        logger.info("facts.verified", fact_id=str(fact_id), verified=bool(verified))
        return row

    async def update_text(self, fact_id: uuid.UUID, text: str) -> KnowledgeFact | None:
        """Rewrite a fact's claim, keeping every derived value consistent with it.

        Three things must move together or deduplication silently breaks: the text, the
        normalised text and content hash (recomputed through
        :meth:`~app.models.knowledge.KnowledgeFact.refresh_derived_fields`), and the
        embedding plus its vector record. Doing all three here is why callers should never
        assign ``fact.text`` directly.

        Args:
            fact_id: The fact to edit.
            text: The new claim. Must not be blank.

        Returns:
            The updated row, or ``None`` when no such fact exists.

        Raises:
            ValueError: If *text* is empty or whitespace-only — a fact with no claim is not
                a fact, and its hash would collide with every other blank one.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("fact text must not be blank")

        row = await self.session.get(KnowledgeFact, fact_id)
        if row is None:
            return None

        row.text = cleaned
        row.refresh_derived_fields()
        vectors = await self.embed([cleaned])
        row.embedding = list(vectors[0]) if vectors else None
        await self.session.flush()

        if row.embedding is not None and row.is_active:
            await self.vector_upsert(FACT_COLLECTION, [_vector_record(row, row.user_id)])
        logger.info("facts.text_updated", fact_id=str(fact_id))
        return row

    # ----------------------------------------------------------------------------------
    # Maintenance and reporting
    # ----------------------------------------------------------------------------------

    async def stats(self, user_id: uuid.UUID) -> dict[str, int]:
        """Return fact counts for *user_id*.

        Keys are :data:`STATS_TOTAL`, :data:`STATS_ACTIVE`, :data:`STATS_VERIFIED` and
        :data:`STATS_EMBEDDED`, plus one ``fact_<kind>`` entry per
        :class:`~app.models.enums.FactKind` actually present. Kinds with no facts are
        omitted; a client should read a missing key as ``0``.

        Args:
            user_id: Owning user.

        Returns:
            A flat ``dict[str, int]``.
        """
        counts: dict[str, int] = {
            STATS_TOTAL: await self._count(user_id),
            STATS_ACTIVE: await self._count(user_id, KnowledgeFact.is_active.is_(True)),
            STATS_VERIFIED: await self._count(user_id, KnowledgeFact.user_verified.is_(True)),
            STATS_EMBEDDED: await self._count(user_id, self._embedding_present()),
        }

        rows = await self.session.execute(
            select(KnowledgeFact.kind, func.count(KnowledgeFact.id))
            .where(KnowledgeFact.user_id == user_id)
            .group_by(KnowledgeFact.kind)
        )
        for kind, count in rows.all():
            counts[f"{STATS_KIND_PREFIX}{FactKind(kind).value}"] = int(count)
        return counts

    async def _count(self, user_id: uuid.UUID, *conditions: Any) -> int:
        """Count the user's facts matching *conditions*.

        Args:
            user_id: Owning user.
            *conditions: Extra SQL predicates.

        Returns:
            The row count.
        """
        statement = select(func.count(KnowledgeFact.id)).where(
            KnowledgeFact.user_id == user_id, *conditions
        )
        return int((await self.session.execute(statement)).scalar_one() or 0)

    async def reembed_missing(
        self, user_id: uuid.UUID, *, batch: int = DEFAULT_REEMBED_BATCH
    ) -> int:
        """Embed the user's active facts that have no vector yet, in batches.

        The backlog worker for two situations: facts written while the embedder was
        unavailable, and facts written before the embedding model changed. Each pass takes
        the next *batch* unembedded facts, embeds them in one call, writes the vectors to
        both the row and the vector store, and repeats until none are left.

        Args:
            user_id: Owning user.
            batch: Facts per embedding request. Clamped to at least one.

        Returns:
            How many facts were embedded. ``0`` when there was nothing to do, or when no
            embedder is available (logged once by
            :meth:`~app.knowledge.graph.KnowledgeStore.embed`).
        """
        size = max(1, int(batch))
        embedded = 0

        for _ in range(MAX_REEMBED_PASSES):
            statement = (
                select(KnowledgeFact)
                .where(
                    KnowledgeFact.user_id == user_id,
                    KnowledgeFact.is_active.is_(True),
                    self._embedding_absent(),
                )
                .order_by(KnowledgeFact.created_at.asc())
                .limit(size)
            )
            rows = list((await self.session.execute(statement)).scalars().all())
            if not rows:
                break

            vectors = await self.embed([row.text for row in rows])
            if not vectors or len(vectors) != len(rows):
                logger.warning(
                    "facts.reembed_unavailable",
                    user_id=str(user_id),
                    pending=len(rows),
                )
                break

            for row, vector in zip(rows, vectors, strict=True):
                row.embedding = list(vector)
            await self.session.flush()
            await self.vector_upsert(
                FACT_COLLECTION, [_vector_record(row, user_id) for row in rows]
            )
            embedded += len(rows)
        else:  # pragma: no cover - only reachable if a backend refuses to persist
            logger.warning(
                "facts.reembed_pass_limit",
                user_id=str(user_id),
                passes=MAX_REEMBED_PASSES,
            )

        if embedded:
            logger.info("facts.reembedded", user_id=str(user_id), facts=embedded)
        return embedded


# ======================================================================================
# Module-private helpers
# ======================================================================================


def _keywords(query: str) -> list[str]:
    """Return the distinct content keywords of *query*, capped at :data:`MAX_KEYWORDS`.

    Reuses :func:`app.ai.embeddings.content_tokens`, which already tokenises technical
    identifiers correctly (``c++``, ``node.js``, ``ci/cd`` survive as single tokens) and
    drops English function words. Matching normalised text, which is case-folded, so the
    keywords are too.

    Args:
        query: The raw query.

    Returns:
        Keywords in first-appearance order.
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


def _keyword_overlap(row: KnowledgeFact, keywords: Sequence[str]) -> int:
    """Count how many of *keywords* the row mentions anywhere.

    Args:
        row: A candidate fact.
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


def _as_uuid(value: str) -> uuid.UUID | None:
    """Parse a vector record id back into a UUID.

    Args:
        value: The record id, which is a fact's primary key rendered as a string.

    Returns:
        The parsed UUID, or ``None`` when the id did not come from this store — a stale
        record from another schema version, say. Never raises: a malformed id in the index
        must not fail a search.
    """
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _vector_record(row: KnowledgeFact, user_id: uuid.UUID) -> VectorRecord:
    """Build the vector-store record for a fact.

    ``user_id`` and ``kind`` are the two reserved filter keys the SQL backends promote to
    indexed columns (:mod:`app.knowledge.vector.base`), so putting them in metadata is what
    makes a per-user, per-kind query use an index instead of scanning JSON.

    Args:
        row: The fact, already flushed so it has an id.
        user_id: Owning user.

    Returns:
        The record to upsert into :data:`FACT_COLLECTION`.
    """
    return VectorRecord(
        id=str(row.id),
        embedding=list(row.embedding or []),
        text=row.text,
        metadata={
            "user_id": str(user_id),
            "kind": FactKind(row.kind).value,
            "impact_score": int(row.impact_score or 0),
            "content_hash": row.content_hash,
        },
    )


def _load_options() -> tuple[Any, ...]:
    """Return the eager-load options applied to every fact query that returns rows.

    ``selectinload`` issues one extra ``SELECT ... IN (...)`` per relationship for the
    whole result set — not one per row — so the cost is two queries regardless of size, and
    it removes any possibility of a caller touching ``fact.entity`` or
    ``fact.source_document`` and raising ``MissingGreenlet`` inside an async session.

    Returns:
        The options tuple, built lazily so importing this module does not require the ORM
        relationship configuration to have run.
    """
    from sqlalchemy.orm import selectinload

    return (
        selectinload(KnowledgeFact.entity),
        selectinload(KnowledgeFact.source_document),
    )


#: Materialised once at import; see :func:`_load_options`.
_LOAD_OPTIONS: Final[tuple[Any, ...]] = _load_options()
