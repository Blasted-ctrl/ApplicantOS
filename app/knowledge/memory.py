"""The AI's memory — what the user taught it, and why it stops repeating a mistake.

Facts describe the user. **Memories describe what the system got wrong about the user**, and
that is a different and more valuable thing. A knowledge base can be perfect and still
generate a bullet the user hates for reasons no source document contains: a phrasing they
find boastful, a project they no longer want on a resume, a company they will not apply to
again. :class:`~app.models.knowledge.MemoryEntry` is where that lives, and
:meth:`MemoryStore.relevant` is what puts it back in front of the model on the next
generation.

Four signals, in descending order of how much they are worth:

``correction``
    The user edited something the AI generated. **The single most valuable signal in the
    product.** Both sides are stored — what was rejected *and* what was written instead —
    because "don't say X" without "say Y" teaches a model nothing it can act on. The pair is
    embedded together so that a later generation touching the same subject retrieves it.
``outcome``
    An application reached interview, rejection, or offer. Enriched with the company and
    title at write time, so the record stays meaningful after the posting expires and is the
    raw material for "what actually gets interviews".
``preference``
    Something the user stated rather than something inferred. May expire, which is how "not
    this company for now" stops influencing output without being erased from the audit
    trail.
``feedback``
    Free-form commentary that is neither a correction nor a stated rule.

Retrieval multiplies three independent quantities — semantic similarity to the query, the
memory's :attr:`~app.models.knowledge.MemoryEntry.weight`, and a recency factor that decays
with a documented half-life toward a floor rather than to zero (see
:func:`recency_factor`). A correction from a year ago still counts: a person's taste ages,
it does not expire.

Everything degrades to SQL when there is no vector store and no embedder, because
ApplicantOS must run offline with zero API keys — and an assistant that forgets the user's
corrections the moment it goes offline would be worse than one that never learned them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

import structlog
from sqlalchemy import delete, func, or_, select

from app.database.types import utcnow
from app.knowledge.graph import KnowledgeStore, chunked
from app.knowledge.vector.base import VectorRecord
from app.models.enums import ApplicationStatus, MemoryKind
from app.models.knowledge import (
    DEFAULT_MEMORY_WEIGHT,
    MemoryEntry,
    normalize_for_matching,
)

__all__ = [
    "CORRECTION_TEMPLATE",
    "KIND_WEIGHTS",
    "MAX_MEMORY_WEIGHT",
    "MEMORY_COLLECTION",
    "MEMORY_HALF_LIFE_DAYS",
    "MEMORY_PROMPT_HEADER",
    "MEMORY_PROMPT_TOKEN_BUDGET",
    "MIN_MEMORY_WEIGHT",
    "MIN_RECENCY_FACTOR",
    "OUTCOME_WEIGHTS",
    "REPEAT_REINFORCEMENT",
    "MemoryStore",
    "recency_factor",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Vector-store collection holding one record per memory.
MEMORY_COLLECTION: Final[str] = "memories"

#: Default weight per memory kind — the multiplier applied to relevance, so an emphatic
#: correction outranks a passing note even when the note is a closer semantic match. A
#: correction is worth double a note because it is the only signal that names a specific
#: mistake the system already made.
KIND_WEIGHTS: Final[dict[MemoryKind, float]] = {
    MemoryKind.CORRECTION: 2.0,
    MemoryKind.PREFERENCE: 1.6,
    MemoryKind.OUTCOME: 1.4,
    MemoryKind.FEEDBACK: 1.2,
    MemoryKind.NOTE: DEFAULT_MEMORY_WEIGHT,
}

#: Weight overrides for application outcomes. An offer or an interview says far more about
#: what works than a rejection does — a rejection has a hundred causes, most of them nothing
#: to do with the resume — and a ghosting says almost nothing at all.
OUTCOME_WEIGHTS: Final[dict[ApplicationStatus, float]] = {
    ApplicationStatus.OFFER: 2.2,
    ApplicationStatus.INTERVIEW: 2.0,
    ApplicationStatus.REJECTED: 1.4,
    ApplicationStatus.CONFIRMED: 1.1,
    ApplicationStatus.SUBMITTED: 1.0,
    ApplicationStatus.GHOSTED: 0.9,
}

#: Bounds on a memory's weight. Zero silences a memory without deleting it; the ceiling
#: stops a repeatedly-reinforced entry from crowding out everything else forever.
MIN_MEMORY_WEIGHT: Final[float] = 0.0
MAX_MEMORY_WEIGHT: Final[float] = 10.0

#: Weight added when the same memory is recorded again. Repetition is evidence: a user who
#: makes the same edit three times means it three times as much.
REPEAT_REINFORCEMENT: Final[float] = 0.5

#: Half-life of the recency factor, in days. Ninety days is roughly one job-search season:
#: a correction from this search is fully weighted, one from the last search still counts
#: but yields to anything more recent that contradicts it.
MEMORY_HALF_LIFE_DAYS: Final[float] = 90.0

#: Floor of the recency factor. Decay stops here instead of continuing to zero, so an old
#: correction is *quieter*, never *gone* — the user should not have to teach the system the
#: same thing again every quarter.
MIN_RECENCY_FACTOR: Final[float] = 0.25

#: Similarity credited to a candidate found by the SQL fallback rather than by vector
#: search, when it shares no keyword with the query. Small but non-zero, so weight and
#: recency still order the results instead of everything scoring zero.
SQL_FALLBACK_SIMILARITY: Final[float] = 0.05

#: Default number of memories :meth:`MemoryStore.relevant` returns, per
#: ``docs/CONTRACTS.md`` §8.3.
DEFAULT_MEMORY_K: Final[int] = 8

#: How many extra candidates :meth:`MemoryStore.relevant` fetches before re-ranking by
#: weight and recency. Without the overfetch, a hugely-weighted correction that ranks 9th on
#: raw similarity would never be seen.
RELEVANT_OVERFETCH: Final[int] = 4

#: Token budget for :meth:`MemoryStore.as_prompt_context`. Memories share the system prompt
#: with the retrieved facts and the job description; past this they start crowding out the
#: material the resume is actually generated from.
MEMORY_PROMPT_TOKEN_BUDGET: Final[int] = 600

#: Heading of the injected block. Phrased as an instruction because that is what it is.
MEMORY_PROMPT_HEADER: Final[str] = (
    "What this user has already taught you — honour it and do not repeat these mistakes:"
)

#: Rendering of one memory inside that block.
MEMORY_PROMPT_BULLET: Final[str] = "- [{kind}] {text}"

#: How a correction is rendered as retrievable, promptable text. Both halves are present
#: because a rejection without its replacement is not actionable.
CORRECTION_TEMPLATE: Final[str] = "Rejected wording: {before}\nPreferred wording: {after}"

#: How an application outcome is rendered.
OUTCOME_TEMPLATE: Final[str] = "Application to {company} for {title} ended in: {outcome}."
OUTCOME_TEMPLATE_MINIMAL: Final[str] = "An application ended in: {outcome}."

#: Longest ``before``/``after`` excerpt kept in a correction's text. The full strings stay
#: in ``context``; the text is what gets embedded and injected into a prompt, and a rejected
#: three-page draft is not a useful retrieval key.
MAX_CORRECTION_EXCERPT_CHARS: Final[int] = 400

#: Context keys this module writes itself. A caller's ``context`` is merged *under* these,
#: so a stray ``before`` key cannot overwrite the correction being recorded.
CONTEXT_KEY_BEFORE: Final[str] = "before"
CONTEXT_KEY_AFTER: Final[str] = "after"
CONTEXT_KEY_APPLICATION_ID: Final[str] = "application_id"
CONTEXT_KEY_OUTCOME: Final[str] = "outcome"
CONTEXT_KEY_NOTES: Final[str] = "notes"
CONTEXT_KEY_COMPANY: Final[str] = "company"
CONTEXT_KEY_TITLE: Final[str] = "title"
CONTEXT_KEY_POSTING_ID: Final[str] = "posting_id"
CONTEXT_KEY_OBSERVATIONS: Final[str] = "observations"

#: Seconds in a day, for the recency arithmetic.
_SECONDS_PER_DAY: Final[float] = 86_400.0


def recency_factor(created_at: datetime, *, now: datetime | None = None) -> float:
    """Return how much a memory's age discounts it, in ``[MIN_RECENCY_FACTOR, 1.0]``.

    The decay is exponential with a half-life of :data:`MEMORY_HALF_LIFE_DAYS`, compressed
    into a floored range::

        factor = MIN_RECENCY_FACTOR + (1 - MIN_RECENCY_FACTOR) * 0.5 ** (age_days / HALF_LIFE)

    So a memory recorded today scores ``1.0``, one from ninety days ago ``0.625``, one from a
    year ago ``0.30``, and one from five years ago converges on ``0.25`` — quieter, never
    silent. Decaying to zero would mean the user has to re-teach the system the same lesson
    every few months, which is precisely the failure this table exists to prevent.

    Args:
        created_at: When the memory was recorded. Naive values are treated as UTC.
        now: The instant to measure against; the current time when omitted.

    Returns:
        The multiplier to apply to relevance.
    """
    moment = now if now is not None else utcnow()
    stamp = created_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=moment.tzinfo)
    age_days = max(0.0, (moment - stamp).total_seconds() / _SECONDS_PER_DAY)
    decay = 0.5 ** (age_days / MEMORY_HALF_LIFE_DAYS)
    return MIN_RECENCY_FACTOR + (1.0 - MIN_RECENCY_FACTOR) * decay


class MemoryStore(KnowledgeStore):
    """Record, retrieve, reinforce and forget what the user has taught the system.

    Scoped to one ``user_id`` on every write and every search. Flushes but never commits, so
    a correction recorded during a review can share the transaction that resolves it.
    """

    # ----------------------------------------------------------------------------------
    # Writing
    # ----------------------------------------------------------------------------------

    async def record_correction(
        self,
        user_id: uuid.UUID,
        *,
        before: str,
        after: str,
        context: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        """Remember that the user rewrote something the AI generated.

        This is the signal the whole feedback loop is built around. Both sides are kept:
        ``before`` is what makes the memory *retrievable* when the model is about to make the
        same mistake again, and ``after`` is what makes it *actionable* when it does. Storing
        only the rejection would teach the model to avoid one phrasing without giving it
        anything to put in its place.

        The pair is rendered into one text (:data:`CORRECTION_TEMPLATE`) and embedded as a
        unit, so retrieval matches on either half. The full, untruncated strings are kept in
        ``context`` regardless of how long they are.

        Recording the same correction twice reinforces the existing entry rather than adding
        a row — see :meth:`_record`.

        Args:
            user_id: Owning user.
            before: The text the AI produced and the user rejected.
            after: The text the user wrote instead.
            context: Where it happened — application id, field name, resume section. Merged
                beneath this method's own keys.

        Returns:
            The stored memory, flushed.

        Raises:
            ValueError: If both sides are blank; there is no correction to learn from.
        """
        original = (before or "").strip()
        replacement = (after or "").strip()
        if not original and not replacement:
            raise ValueError("a correction needs at least one of `before` or `after`")

        text = CORRECTION_TEMPLATE.format(
            before=_excerpt(original) or "(nothing)",
            after=_excerpt(replacement) or "(removed)",
        )
        payload = dict(context or {})
        payload[CONTEXT_KEY_BEFORE] = original
        payload[CONTEXT_KEY_AFTER] = replacement

        return await self._record(
            user_id,
            kind=MemoryKind.CORRECTION,
            text=text,
            context=payload,
        )

    async def record_outcome(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        outcome: ApplicationStatus,
        notes: str | None = None,
    ) -> MemoryEntry:
        """Remember how an application ended.

        The company and job title are resolved *now* and written into the memory's text,
        rather than left as a foreign key to be joined later. Postings expire and get
        deleted; the lesson ("robotics firmware roles at hardware companies convert") has to
        outlive them, and a memory that reads "application 8f3a… → interview" teaches
        nothing when it is retrieved six months from now.

        Args:
            user_id: Owning user.
            application_id: The application whose outcome this is.
            outcome: The status reached — typically ``interview``, ``rejected``, ``offer``
                or ``ghosted``.
            notes: Anything the user or the system wants to add.

        Returns:
            The stored memory, flushed.
        """
        status = ApplicationStatus(outcome)
        company, title, posting_id = await self._describe_application(application_id)

        if company or title:
            text = OUTCOME_TEMPLATE.format(
                company=company or "an unnamed company",
                title=f"“{title}”" if title else "an unnamed role",
                outcome=status.value,
            )
        else:
            text = OUTCOME_TEMPLATE_MINIMAL.format(outcome=status.value)
        cleaned_notes = (notes or "").strip()
        if cleaned_notes:
            text = f"{text} Notes: {cleaned_notes}"

        context: dict[str, Any] = {
            CONTEXT_KEY_APPLICATION_ID: str(application_id),
            CONTEXT_KEY_OUTCOME: status.value,
        }
        if company:
            context[CONTEXT_KEY_COMPANY] = company
        if title:
            context[CONTEXT_KEY_TITLE] = title
        if posting_id is not None:
            context[CONTEXT_KEY_POSTING_ID] = str(posting_id)
        if cleaned_notes:
            context[CONTEXT_KEY_NOTES] = cleaned_notes

        return await self._record(
            user_id,
            kind=MemoryKind.OUTCOME,
            text=text,
            context=context,
            weight=OUTCOME_WEIGHTS.get(status, KIND_WEIGHTS[MemoryKind.OUTCOME]),
        )

    async def record_feedback(
        self,
        user_id: uuid.UUID,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        """Remember free-form commentary from the user.

        Args:
            user_id: Owning user.
            text: What the user said.
            context: Where they said it — screen, application id, generated artifact.

        Returns:
            The stored memory, flushed.

        Raises:
            ValueError: If *text* is blank.
        """
        return await self._record(
            user_id, kind=MemoryKind.FEEDBACK, text=text, context=dict(context or {})
        )

    async def record_preference(
        self,
        user_id: uuid.UUID,
        text: str,
        context: dict[str, Any] | None = None,
        *,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        """Remember a rule the user stated rather than one the system inferred.

        Distinct from :class:`~app.models.user.UserPreferences`, which is structured policy
        the pipeline enforces. This is the unstructured half — "keep the robotics work above
        the web work", "never call me a full-stack developer" — which nothing can enforce
        but the generator should honour.

        *expires_at* is what makes a temporary rule temporary: an expired preference stops
        being retrieved (:meth:`relevant`) without being deleted from the audit trail.

        Args:
            user_id: Owning user.
            text: The stated preference.
            context: Structured provenance.
            expires_at: When the preference stops applying; permanent when ``None``.

        Returns:
            The stored memory, flushed.

        Raises:
            ValueError: If *text* is blank.
        """
        return await self._record(
            user_id,
            kind=MemoryKind.PREFERENCE,
            text=text,
            context=dict(context or {}),
            expires_at=expires_at,
        )

    async def _record(
        self,
        user_id: uuid.UUID,
        *,
        kind: MemoryKind,
        text: str,
        context: dict[str, Any],
        weight: float | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        """Store a memory, or reinforce the identical one that already exists.

        Deduplication is exact equality of ``(user_id, kind, text)``. Two identical
        corrections are one lesson learned twice, not two lessons, and recording them as two
        rows would both bloat the table and let a single repeated edit dominate every
        retrieval. Reinforcement (:data:`REPEAT_REINFORCEMENT`) captures the repetition
        properly: emphasis, not volume.

        Args:
            user_id: Owning user.
            kind: Memory category.
            text: The remembered content, already rendered.
            context: Structured provenance; merged into the incumbent's on a repeat, with
                the incumbent's own values winning.
            weight: Starting weight; :data:`KIND_WEIGHTS` for the kind when omitted.
            expires_at: Expiry, or ``None`` for permanent.

        Returns:
            The stored memory, flushed.

        Raises:
            ValueError: If *text* is blank — an empty memory can never be retrieved and
                would collide with every other empty one.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError(f"a {kind.value} memory must carry text")

        existing = await self._find_identical(user_id, kind, cleaned)
        if existing is not None:
            existing.weight = _clamp_weight(float(existing.weight or 0.0) + REPEAT_REINFORCEMENT)
            merged = dict(context or {})
            merged.update(existing.context or {})
            existing.context = merged
            if expires_at is not None:
                existing.expires_at = expires_at
            await self.session.flush()
            logger.info(
                "memory.reinforced",
                user_id=str(user_id),
                kind=kind.value,
                memory_id=str(existing.id),
                weight=existing.weight,
            )
            return existing

        vectors = await self.embed([cleaned])
        entry = MemoryEntry(
            user_id=user_id,
            kind=kind,
            text=cleaned,
            context=dict(context or {}),
            embedding=list(vectors[0]) if vectors else None,
            weight=_clamp_weight(
                weight if weight is not None else KIND_WEIGHTS.get(kind, DEFAULT_MEMORY_WEIGHT)
            ),
            expires_at=expires_at,
        )
        self.session.add(entry)
        await self.session.flush()

        if entry.embedding is not None:
            await self.vector_upsert(MEMORY_COLLECTION, [_vector_record(entry)])

        logger.info(
            "memory.recorded",
            user_id=str(user_id),
            kind=kind.value,
            memory_id=str(entry.id),
            weight=entry.weight,
        )
        return entry

    async def _find_identical(
        self, user_id: uuid.UUID, kind: MemoryKind, text: str
    ) -> MemoryEntry | None:
        """Return the user's memory with exactly this kind and text, if one exists.

        Args:
            user_id: Owning user.
            kind: Memory category.
            text: The rendered text to match exactly.

        Returns:
            The oldest matching row, or ``None``.
        """
        statement = (
            select(MemoryEntry)
            .where(
                MemoryEntry.user_id == user_id,
                MemoryEntry.kind == kind,
                MemoryEntry.text == text,
            )
            .order_by(MemoryEntry.created_at.asc())
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def _describe_application(
        self, application_id: uuid.UUID
    ) -> tuple[str | None, str | None, uuid.UUID | None]:
        """Resolve an application's company name, job title and posting id.

        Args:
            application_id: The application to describe.

        Returns:
            ``(company, title, posting_id)``, each ``None`` when unavailable. A missing
            application is not an error: an outcome recorded against a deleted row is still
            worth remembering, just less specifically.
        """
        from app.models.application import Application
        from app.models.company import Company
        from app.models.posting import JobPosting

        statement = (
            select(Company.name, JobPosting.title, Application.posting_id)
            .select_from(Application)
            .outerjoin(Company, Company.id == Application.company_id)
            .outerjoin(JobPosting, JobPosting.id == Application.posting_id)
            .where(Application.id == application_id)
            .limit(1)
        )
        row = (await self.session.execute(statement)).first()
        if row is None:
            logger.debug("memory.application_not_found", application_id=str(application_id))
            return (None, None, None)
        company, title, posting_id = row
        return (company or None, title or None, posting_id)

    # ----------------------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------------------

    async def relevant(
        self, user_id: uuid.UUID, query: str, *, k: int = DEFAULT_MEMORY_K
    ) -> list[MemoryEntry]:
        """Return the memories most worth showing the model for *query*.

        Ranking multiplies three independent quantities::

            score = similarity * weight * recency_factor(created_at)

        * **similarity** — cosine against the query, clamped at zero so an unrelated memory
          cannot earn negative credit and flip the sign of the product.
        * **weight** — how much the memory matters (:data:`KIND_WEIGHTS`, raised by
          :meth:`reinforce` and by repetition). This is why a correction beats a note that
          matches the query more closely.
        * **recency** — :func:`recency_factor`, decaying to a floor rather than to zero.

        Expired entries are excluded at both the SQL and the Python level. With no vector
        store or no embedder the candidate set comes from a keyword-and-weight SQL query
        instead, and the same three-way ranking is applied to it.

        Args:
            user_id: Owning user.
            query: What the model is about to generate — a job description, a field label, a
                draft bullet.
            k: Maximum memories to return.

        Returns:
            At most *k* memories, most relevant first.
        """
        if k < 1:
            return []
        now = utcnow()
        fetch = k * RELEVANT_OVERFETCH

        scored: list[tuple[MemoryEntry, float]] = []
        embedded = await self.embed([query]) if (query or "").strip() else None
        if embedded:
            hits = await self.vector_query(
                MEMORY_COLLECTION,
                embedded[0],
                k=fetch,
                filters={"user_id": str(user_id)},
            )
            similarities: dict[uuid.UUID, float] = {}
            for hit in hits:
                identifier = _as_uuid(hit.id)
                if identifier is not None:
                    similarities[identifier] = hit.score
            if similarities:
                rows = await self._by_ids(user_id, list(similarities))
                scored = [
                    (row, max(0.0, similarities.get(row.id, 0.0)))
                    for row in rows
                    if not row.is_expired(now)
                ]

        if not scored:
            scored = await self._sql_candidates(user_id, query, limit=fetch, now=now)

        ranked = sorted(
            scored,
            key=lambda item: (
                -(
                    item[1]
                    * float(item[0].weight or 0.0)
                    * recency_factor(item[0].created_at, now=now)
                ),
                str(item[0].id),
            ),
        )
        results = [entry for entry, _ in ranked[:k]]
        logger.debug(
            "memory.relevant",
            user_id=str(user_id),
            k=k,
            candidates=len(scored),
            returned=len(results),
        )
        return results

    async def _by_ids(self, user_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> list[MemoryEntry]:
        """Load the user's memories by id.

        Args:
            user_id: Owning user — enforced in SQL, so a stale vector record belonging to
                another user can never leak into a result.
            ids: The memory ids.

        Returns:
            The rows that exist and belong to *user_id*.
        """
        loaded: list[MemoryEntry] = []
        for chunk in chunked(list(dict.fromkeys(ids))):
            statement = select(MemoryEntry).where(
                MemoryEntry.user_id == user_id, MemoryEntry.id.in_(chunk)
            )
            loaded.extend((await self.session.execute(statement)).scalars().all())
        return loaded

    async def _sql_candidates(
        self, user_id: uuid.UUID, query: str, *, limit: int, now: datetime
    ) -> list[tuple[MemoryEntry, float]]:
        """Return candidates and pseudo-similarities without using the vector store.

        The fallback path for an install with no embedder or no vector index. Candidates are
        the heaviest, most recent unexpired memories; the pseudo-similarity is the fraction
        of the query's words the memory's text contains, floored at
        :data:`SQL_FALLBACK_SIMILARITY` so weight and recency still order a set that matches
        nothing lexically.

        Args:
            user_id: Owning user.
            query: The raw query text.
            limit: Maximum candidates.
            now: The instant expiry is evaluated against.

        Returns:
            ``(memory, similarity)`` pairs.
        """
        statement = (
            select(MemoryEntry)
            .where(
                MemoryEntry.user_id == user_id,
                or_(MemoryEntry.expires_at.is_(None), MemoryEntry.expires_at > now),
            )
            .order_by(MemoryEntry.weight.desc(), MemoryEntry.created_at.desc())
            .limit(max(1, limit))
        )
        rows = (await self.session.execute(statement)).scalars().all()
        words = {token for token in normalize_for_matching(query or "").split() if len(token) > 2}
        candidates: list[tuple[MemoryEntry, float]] = []
        for row in rows:
            if row.is_expired(now):
                continue
            similarity = SQL_FALLBACK_SIMILARITY
            if words:
                haystack = normalize_for_matching(row.text or "")
                overlap = sum(1 for word in words if word in haystack)
                similarity = max(SQL_FALLBACK_SIMILARITY, overlap / len(words))
            candidates.append((row, similarity))
        return candidates

    @staticmethod
    def as_prompt_context(memories: Sequence[MemoryEntry]) -> str:
        """Render memories as a compact block for system-prompt injection.

        Bounded by :data:`MEMORY_PROMPT_TOKEN_BUDGET`: memories share the prompt with the
        retrieved facts and the job description, and an unbounded block would crowd out the
        material the resume is actually generated from. Entries are taken in the order given
        — :meth:`relevant` has already ranked them — and the first one that would breach the
        budget ends the block, so the most relevant memories are the ones that survive.

        Args:
            memories: The memories to render, most relevant first.

        Returns:
            The block, or ``""`` when there is nothing to say. Never ends in a newline.
        """
        if not memories:
            return ""

        from app.knowledge.analyzers.base import CHARS_PER_TOKEN, estimate_tokens

        budget = MEMORY_PROMPT_TOKEN_BUDGET * CHARS_PER_TOKEN
        lines = [MEMORY_PROMPT_HEADER]
        used = len(MEMORY_PROMPT_HEADER)
        rendered = 0

        for memory in memories:
            line = MEMORY_PROMPT_BULLET.format(
                kind=MemoryKind(memory.kind).value,
                text=" / ".join(
                    part.strip() for part in (memory.text or "").splitlines() if part.strip()
                ),
            )
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
            rendered += 1

        if rendered == 0:
            return ""
        if rendered < len(memories):
            logger.debug(
                "memory.prompt_context_truncated",
                rendered=rendered,
                available=len(memories),
                tokens=estimate_tokens("\n".join(lines)),
            )
        return "\n".join(lines)

    # ----------------------------------------------------------------------------------
    # Maintenance
    # ----------------------------------------------------------------------------------

    async def prune_expired(self) -> int:
        """Delete every memory whose expiry has passed, across all users.

        The maintenance worker's sweep. Permanent memories (``expires_at IS NULL``) are
        never touched — only entries something explicitly marked as temporary.

        Returns:
            The number of memories deleted.
        """
        now = utcnow()
        ids = list(
            (
                await self.session.execute(
                    select(MemoryEntry.id).where(
                        MemoryEntry.expires_at.is_not(None),
                        MemoryEntry.expires_at <= now,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return 0

        for chunk in chunked(ids):
            await self.session.execute(
                delete(MemoryEntry)
                .where(MemoryEntry.id.in_(chunk))
                .execution_options(synchronize_session="fetch")
            )
        await self.session.flush()
        await self.vector_delete(MEMORY_COLLECTION, [str(value) for value in ids])

        logger.info("memory.pruned", memories=len(ids))
        return len(ids)

    async def reinforce(self, memory_id: uuid.UUID, delta: float) -> MemoryEntry | None:
        """Adjust a memory's weight, clamped to the documented bounds.

        Positive *delta* is what happens when the system observes the same lesson again;
        negative *delta* is how a memory is quieted without being destroyed — dropping it to
        :data:`MIN_MEMORY_WEIGHT` makes it score zero in :meth:`relevant` while leaving the
        audit trail intact.

        Args:
            memory_id: The memory to adjust.
            delta: Weight change, positive or negative.

        Returns:
            The updated memory, or ``None`` when no such memory exists.
        """
        entry = await self.session.get(MemoryEntry, memory_id)
        if entry is None:
            return None
        entry.weight = _clamp_weight(float(entry.weight or 0.0) + float(delta))
        await self.session.flush()
        logger.info(
            "memory.weight_adjusted",
            memory_id=str(memory_id),
            delta=float(delta),
            weight=entry.weight,
        )
        return entry

    async def forget(self, memory_id: uuid.UUID) -> bool:
        """Delete a memory outright, including its vector record.

        The user's escape hatch: something the system remembered that it should not have.
        Unlike :meth:`reinforce` to zero, this leaves nothing behind, which is the correct
        behaviour when the memory itself is the problem.

        Args:
            memory_id: The memory to delete.

        Returns:
            ``True`` when a row was deleted, ``False`` when it did not exist.
        """
        entry = await self.session.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        await self.session.delete(entry)
        await self.session.flush()
        await self.vector_delete(MEMORY_COLLECTION, [str(memory_id)])
        logger.info("memory.forgotten", memory_id=str(memory_id))
        return True

    async def stats(self, user_id: uuid.UUID) -> dict[str, int]:
        """Return memory counts for *user_id*, broken down by kind.

        Args:
            user_id: Owning user.

        Returns:
            ``{"memories": total, "memories_expired": n, "memory_<kind>": n, ...}``. Kinds
            with no entries are omitted; read a missing key as ``0``.
        """
        now = utcnow()
        total = int(
            (
                await self.session.execute(
                    select(func.count(MemoryEntry.id)).where(MemoryEntry.user_id == user_id)
                )
            ).scalar_one()
            or 0
        )
        expired = int(
            (
                await self.session.execute(
                    select(func.count(MemoryEntry.id)).where(
                        MemoryEntry.user_id == user_id,
                        MemoryEntry.expires_at.is_not(None),
                        MemoryEntry.expires_at <= now,
                    )
                )
            ).scalar_one()
            or 0
        )
        counts: dict[str, int] = {"memories": total, "memories_expired": expired}

        rows = await self.session.execute(
            select(MemoryEntry.kind, func.count(MemoryEntry.id))
            .where(MemoryEntry.user_id == user_id)
            .group_by(MemoryEntry.kind)
        )
        for kind, count in rows.all():
            counts[f"memory_{MemoryKind(kind).value}"] = int(count)
        return counts


# ======================================================================================
# Module-private helpers
# ======================================================================================


def _clamp_weight(value: float) -> float:
    """Clamp a memory weight into ``[MIN_MEMORY_WEIGHT, MAX_MEMORY_WEIGHT]``.

    Args:
        value: The proposed weight.

    Returns:
        The clamped weight.
    """
    return max(MIN_MEMORY_WEIGHT, min(MAX_MEMORY_WEIGHT, float(value)))


def _excerpt(text: str) -> str:
    """Shorten *text* for inclusion in a memory's embedded, promptable form.

    Args:
        text: The full string; kept in full in ``context``.

    Returns:
        A single-line excerpt of at most :data:`MAX_CORRECTION_EXCERPT_CHARS` characters,
        ellipsised when it had to be cut.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= MAX_CORRECTION_EXCERPT_CHARS:
        return collapsed
    return f"{collapsed[:MAX_CORRECTION_EXCERPT_CHARS].rstrip()}…"


def _as_uuid(value: str) -> uuid.UUID | None:
    """Parse a vector record id back into a UUID.

    Args:
        value: The record id, which is a memory's primary key rendered as a string.

    Returns:
        The parsed UUID, or ``None`` for an id that did not come from this store. Never
        raises: a stale record in the index must not fail a retrieval.
    """
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _vector_record(entry: MemoryEntry) -> VectorRecord:
    """Build the vector-store record for a memory.

    ``user_id`` and ``kind`` are the reserved filter keys the SQL backends promote to
    indexed columns (:mod:`app.knowledge.vector.base`), so retrieval scoped to one user and
    one memory kind uses an index rather than scanning JSON.

    Args:
        entry: The memory, already flushed so it has an id.

    Returns:
        The record to upsert into :data:`MEMORY_COLLECTION`.
    """
    return VectorRecord(
        id=str(entry.id),
        embedding=list(entry.embedding or []),
        text=entry.text,
        metadata={
            "user_id": str(entry.user_id),
            "kind": MemoryKind(entry.kind).value,
            "weight": float(entry.weight or 0.0),
        },
    )
