"""Ingestion identity — the service that decides whether a discovered posting is new.

Every :class:`~app.jobs.base.RawPosting` the discovery layer produces passes through
:meth:`DedupeService.upsert`, and that method is the only place in the system allowed to
create a :class:`~app.models.posting.JobPosting`. Concentrating it here is what makes
"never apply twice" (golden rule #1) a property of the schema rather than a convention:
the row a user applies to is the same row every provider, every re-poll and every
syndicated copy of that advertisement collapses onto.

**Three tiers, hardest first** (:meth:`find_existing`):

1. ``(provider, external_id)`` — the provider's own identity claim, backed by
   ``UNIQUE(job_postings.provider, external_id)``. Re-polling a board is therefore
   idempotent by construction.
2. :func:`app.jobs.dedupe.dedupe_key` — backed by ``UNIQUE(job_postings.dedupe_key)``.
   Exact, stable across re-parses, and incapable of merging two different jobs.
3. Fuzzy — same employer, and a title similarity at or above
   :data:`app.jobs.dedupe.DEFAULT_SIMILARITY_THRESHOLD` inside a
   :data:`FUZZY_WINDOW_DAYS` window. This is the *judgement* tier, and it is the only
   thing that can merge one opening syndicated to two different ATSs, because their
   provider-scoped dedupe keys are different by design (see the
   :mod:`app.jobs.dedupe` module docstring). A canonicalised-URL match is accepted inside
   the same tier, which is what collapses two links to one job that differ only in their
   tracking parameters.

**Why every insert sits inside a SAVEPOINT.** Two pollers can reach the same brand-new
posting in the same instant: both check, both miss, both insert, and one of them loses on
the unique index. Without a savepoint that ``IntegrityError`` poisons the *whole*
transaction — every subsequent statement fails with "current transaction is aborted" on
PostgreSQL — so one racing posting would destroy an entire discovery run. Wrapping the
insert in ``session.begin_nested()`` confines the damage to the savepoint: it rolls back,
the outer transaction survives intact, and the loser simply re-selects the winner's row and
reports ``created=False``. :meth:`get_or_create_company` does the same for employers.

**What an upsert never touches.** ``dedupe_key`` and ``external_id`` are identity. Once a
row has them they are frozen, because rewriting identity mid-life is how a posting acquires
a second row and a user acquires a second application. Description, salary, closing date and
the stored provider payload *are* refreshed, since employers edit live postings, and
``content_hash`` is recomputed from the resulting state so a cached score or a cached
tailored resume is correctly invalidated (golden rule #9).

This service flushes but never commits: the caller owns the unit of work
(``app/database/session.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from app.jobs._parsing import clean_text
from app.jobs.dedupe import (
    DEFAULT_SIMILARITY_THRESHOLD,
    canonical_url,
    content_hash,
    dedupe_key,
    normalize_company,
    similarity,
)
from app.models.company import COMPANY_NAME_MAX_LENGTH, Company
from app.models.enums import EmploymentType, PostingStatus, WorkArrangement
from app.models.posting import (
    EXTERNAL_ID_MAX_LENGTH,
    POSTING_LOCATION_MAX_LENGTH,
    POSTING_TITLE_MAX_LENGTH,
    JobPosting,
)
from app.observability.metrics import record_posting_deduped, record_posting_discovered

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.jobs.base import RawPosting

__all__ = [
    "COMPANY_HINT_FIELDS",
    "FUZZY_CANDIDATE_LIMIT",
    "FUZZY_WINDOW_DAYS",
    "MUTABLE_POSTING_FIELDS",
    "DedupeService",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Tuning constants
# ======================================================================================

#: How far apart two postings may be published and still be judged the same opening by the
#: fuzzy tier. Thirty days is the practical life of a syndicated advertisement: long enough
#: that a role reposted to a second board a fortnight later still collapses, short enough
#: that next quarter's identically-titled opening at the same employer does not.
FUZZY_WINDOW_DAYS: Final[int] = 30

#: Ceiling on rows the fuzzy tier examines for one incoming posting. A large employer can
#: have thousands of open roles; comparing against the most recent
#: :data:`FUZZY_CANDIDATE_LIMIT` of them bounds ingestion cost, and anything older than that
#: has almost certainly fallen outside :data:`FUZZY_WINDOW_DAYS` anyway.
FUZZY_CANDIDATE_LIMIT: Final[int] = 200

#: Fields on an existing row that a re-poll is allowed to overwrite. Employers edit live
#: postings — a description is rewritten, a range is published, a closing date is added —
#: and the stored row must follow. Identity (``provider``, ``external_id``, ``dedupe_key``)
#: is deliberately absent.
MUTABLE_POSTING_FIELDS: Final[tuple[str, ...]] = (
    "description",
    "salary_min",
    "salary_max",
    "salary_currency",
    "closes_at",
    "raw_json",
)

#: Enrichment hints :meth:`DedupeService.get_or_create_company` accepts. Anything else is
#: ignored with a log line rather than silently dropped, so a misspelled keyword is visible.
COMPANY_HINT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "domain",
        "industry",
        "size_bucket",
        "employee_count",
        "is_defense",
        "is_startup",
        "metadata_json",
    }
)

#: Hint fields that are booleans. ``False`` is a real answer, not "unknown", so these are
#: only ever written when the stored value is still the default ``False``.
_BOOLEAN_HINTS: Final[frozenset[str]] = frozenset({"is_defense", "is_startup"})


# ======================================================================================
# Adapters
# ======================================================================================


@dataclass(frozen=True, slots=True)
class _TextView:
    """A posting's comparable text, detached from wherever it came from.

    :func:`app.jobs.dedupe.similarity` and :func:`app.jobs.dedupe.content_hash` duck-type
    their arguments over ``title`` / ``company_name`` / ``description``. A
    :class:`~app.models.posting.JobPosting` row spells its employer as a *relationship*
    rather than a name, so this tiny adapter is what lets a stored row and an incoming
    :class:`~app.jobs.base.RawPosting` be measured by exactly the same function.

    Attributes:
        title: The role title, unnormalised — the dedupe helpers normalise internally.
        company_name: The employer name, or ``""`` to compare titles alone.
        description: The advertised body, when hashing content.
    """

    title: str
    company_name: str = ""
    description: str | None = None


def _title_similarity(left: str, right: str) -> float:
    """Score two role titles against each other, ignoring their employers.

    The fuzzy tier has already restricted its candidates to one employer, so folding the
    company back into the token bag would only inflate every score by the tokens the two
    postings are guaranteed to share. Comparing titles alone keeps
    :data:`~app.jobs.dedupe.DEFAULT_SIMILARITY_THRESHOLD` meaning what it says.

    Args:
        left: One title.
        right: The other.

    Returns:
        The Jaccard coefficient over the normalised titles, ``0.0`` when either normalises
        to nothing.
    """
    return similarity(
        cast("RawPosting", _TextView(title=left)),
        cast("RawPosting", _TextView(title=right)),
    )


def _content_hash_of(*, title: str, company_name: str, description: str | None) -> str:
    """Digest the advertised content that will actually be stored.

    Computed from the row's post-refresh state rather than from the incoming posting, so a
    truncated title or a description the refresh declined to overwrite can never leave
    ``content_hash`` describing text that is not in the database.

    Args:
        title: The stored title.
        company_name: The stored employer's display name.
        description: The stored description.

    Returns:
        A 64-character lowercase SHA-256 digest.
    """
    return content_hash(
        cast(
            "RawPosting",
            _TextView(title=title, company_name=company_name, description=description),
        )
    )


def _truncate(value: str, limit: int, *, field: str) -> str:
    """Clip *value* to a column's width, loudly.

    PostgreSQL refuses an over-long value while SQLite silently accepts it, so a posting
    with a 700-character title would be ingestible on a laptop and fatal in production.
    Clipping here makes both backends behave identically, and the log line makes the loss
    visible instead of mysterious.

    Args:
        value: The text to store.
        limit: The column's declared width.
        field: Column name, for the log line.

    Returns:
        *value*, clipped to *limit* characters.
    """
    if len(value) <= limit:
        return value
    logger.warning("dedupe.value_truncated", field=field, length=len(value), limit=limit)
    return value[:limit]


def _utcnow() -> datetime:
    """Return the current instant as timezone-aware UTC."""
    return datetime.now(UTC)


def _as_aware(moment: datetime | None) -> datetime | None:
    """Return *moment* as timezone-aware UTC, or ``None``.

    SQLite hands back naive datetimes for rows written before ``UTCDateTime`` normalised
    them, and comparing a naive value with an aware one raises :class:`TypeError` mid-run.

    Args:
        moment: A stored or incoming timestamp.

    Returns:
        The timezone-aware equivalent, assuming UTC when no zone is attached.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


# ======================================================================================
# The service
# ======================================================================================


class DedupeService:
    """Resolves employers and turns raw postings into deduplicated rows.

    Args:
        session: The unit of work. The service flushes so that generated keys and
            constraint violations surface immediately, but never commits — that decision
            belongs to the caller (``app/database/session.py``).
        similarity_threshold: Minimum title similarity for the fuzzy tier. Defaults to
            :data:`~app.jobs.dedupe.DEFAULT_SIMILARITY_THRESHOLD`.
        window_days: Width of the fuzzy tier's publication window, in days.
        candidate_limit: Ceiling on rows the fuzzy tier examines per incoming posting.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        window_days: int = FUZZY_WINDOW_DAYS,
        candidate_limit: int = FUZZY_CANDIDATE_LIMIT,
    ) -> None:
        """Bind the service to a session and its matching tolerances."""
        self._session = session
        self._threshold = float(similarity_threshold)
        self._window = timedelta(days=max(int(window_days), 0))
        self._candidate_limit = max(int(candidate_limit), 1)

    # ----------------------------------------------------------------------------------
    # Employers
    # ----------------------------------------------------------------------------------

    async def get_or_create_company(self, name: str, **hints: Any) -> Company:
        """Resolve an employer name to exactly one :class:`~app.models.company.Company`.

        Identity is :attr:`~app.models.company.Company.normalized_name`, produced by
        :meth:`~app.models.company.Company.normalize` — the same function that backs the
        column's unique index, so the lookup and the constraint can never disagree.
        ``"Acme, Inc."``, ``"ACME Inc"`` and ``"Acme Incorporated"`` therefore all resolve to
        one row, which is what keeps the block list, the analytics and the posting dedupe
        key from fragmenting across spellings.

        A miss then widens the search once, through
        :func:`app.jobs.dedupe.normalize_company` — documented as a deliberate superset of
        the model's frozen suffix list, adding ``corporation`` and the ``bv``/``ab``/``oy``
        forms. That is what makes ``"Globex Corp"`` and ``"Globex Corporation"`` one
        employer instead of two, which matters far beyond cosmetics: a fragmented employer
        is a fragmented block list, and a user who blocked one spelling would keep being
        offered the other. Only the *lookup* widens; a genuinely new row still stores the
        key the unique index expects.

        The insert runs inside a SAVEPOINT. A concurrent poller that wins the race raises
        :class:`~sqlalchemy.exc.IntegrityError` here; the savepoint absorbs it, the outer
        transaction stays usable, and the winner's row is re-selected and returned.

        Args:
            name: The employer name exactly as a provider spelled it.
            **hints: Enrichment fields from :data:`COMPANY_HINT_FIELDS` —
                ``domain``, ``industry``, ``size_bucket``, ``employee_count``,
                ``is_defense``, ``is_startup``, ``metadata_json``. Hints only ever *fill*
                empty fields: an enricher's considered answer is never overwritten by a
                provider's guess. Unknown keys are ignored and logged.

        Returns:
            The persisted employer, flushed so its ``id`` is available.

        Raises:
            ValueError: If *name* carries no normalisable content. A blank employer would
                normalise to ``""``, and since that key is unique, every unnamed employer in
                the system would collapse into one meaningless row.
        """
        cleaned = clean_text(name)
        key = Company.normalize(cleaned)
        if not key:
            raise ValueError(f"company name {name!r} normalises to nothing")

        existing = await self._find_company(key)
        if existing is None:
            existing = await self._find_related_company(cleaned)
        if existing is not None:
            if self._apply_company_hints(existing, hints):
                await self._session.flush()
            return existing

        company = Company(name=_truncate(cleaned, COMPANY_NAME_MAX_LENGTH, field="name"))
        # Assigned after `name` on purpose: the model's `validates` hook derives this from
        # the display name, and an explicit assignment afterwards is the documented way to
        # pin the key computed from the *untruncated* name.
        company.normalized_name = _truncate(key, COMPANY_NAME_MAX_LENGTH, field="normalized_name")
        self._apply_company_hints(company, hints)

        try:
            async with self._session.begin_nested():
                self._session.add(company)
                await self._session.flush()
        except IntegrityError:
            self._detach(company)
            logger.info("dedupe.company_insert_raced", normalized_name=key)
            raced = await self._find_company(key)
            if raced is None:
                raise
            if self._apply_company_hints(raced, hints):
                await self._session.flush()
            return raced

        logger.debug("dedupe.company_created", normalized_name=key, company_id=str(company.id))
        return company

    async def _find_company(self, normalized_name: str) -> Company | None:
        """Look one employer up by its normalisation key.

        Isolated from :meth:`get_or_create_company` so the race path re-selects through
        exactly the same query the first check used.

        Args:
            normalized_name: The key produced by
                :meth:`~app.models.company.Company.normalize`.

        Returns:
            The employer, or ``None``.
        """
        result = await self._session.execute(
            select(Company).where(Company.normalized_name == normalized_name).limit(1)
        )
        return result.scalars().first()

    async def _find_related_company(self, name: str) -> Company | None:
        """Find an employer row that is the same employer under the broader key.

        Args:
            name: The raw employer name.

        Returns:
            The stored employer whose name collapses to the same
            :func:`~app.jobs.dedupe.normalize_company` key, preferring the most collapsed
            spelling on file so the answer is stable whichever order the spellings arrived
            in. ``None`` when there is no such row.
        """
        matches = await self._companies_matching(normalize_company(name))
        return matches[0] if matches else None

    async def _companies_matching(self, broad_key: str) -> list[Company]:
        """Return every employer row that collapses to *broad_key*.

        :func:`~app.jobs.dedupe.normalize_company` only ever strips tokens from the *end* of
        a name, so any stored :attr:`~app.models.company.Company.normalized_name` with this
        broad key is either the key itself or the key followed by a space and the legal-form
        tokens the model's frozen list declined to strip. That makes an index-friendly
        prefix scan both exhaustive and cheap; the Python pass then confirms each candidate
        rather than trusting the prefix.

        Args:
            broad_key: A :func:`~app.jobs.dedupe.normalize_company` key.

        Returns:
            The matching rows, most-collapsed name first, then oldest first — a total order,
            so two runs never disagree about which row is canonical.
        """
        if not broad_key:
            return []
        result = await self._session.execute(
            select(Company)
            .where(
                or_(
                    Company.normalized_name == broad_key,
                    Company.normalized_name.startswith(f"{broad_key} ", autoescape=True),
                )
            )
            .order_by(Company.normalized_name.asc(), Company.created_at.asc())
        )
        return [
            company
            for company in result.scalars().all()
            if normalize_company(company.normalized_name) == broad_key
        ]

    def _apply_company_hints(self, company: Company, hints: dict[str, Any]) -> bool:
        """Fill empty enrichment fields on *company* from *hints*.

        Args:
            company: The row to enrich.
            hints: Caller-supplied values, already keyword-collected.

        Returns:
            Whether anything changed, so the caller can skip a needless flush.
        """
        changed = False
        for field, value in hints.items():
            if field not in COMPANY_HINT_FIELDS:
                logger.debug("dedupe.company_hint_ignored", field=field)
                continue
            if value is None:
                continue
            current = getattr(company, field, None)
            if field in _BOOLEAN_HINTS:
                if current or not bool(value):
                    continue
                setattr(company, field, True)
                changed = True
                continue
            if field == "metadata_json":
                if not isinstance(value, dict) or not value:
                    continue
                merged = {**(current or {}), **value}
                if merged != (current or {}):
                    # Reassigned, never mutated: a plain JSON column is not change tracked.
                    company.metadata_json = merged
                    changed = True
                continue
            if current in (None, ""):
                setattr(company, field, value)
                changed = True
        return changed

    # ----------------------------------------------------------------------------------
    # Posting lookup
    # ----------------------------------------------------------------------------------

    async def find_existing(self, raw: RawPosting) -> JobPosting | None:
        """Find the row *raw* already has, if it has one.

        Three tiers, evaluated hardest-first so that an exact identity claim is never
        overruled by a similarity judgement:

        1. ``(provider, external_id)`` — the provider's own identity for the posting.
        2. :func:`~app.jobs.dedupe.dedupe_key` — the cross-parse identity key.
        3. Fuzzy — same employer, publication dates within :data:`FUZZY_WINDOW_DAYS`, and
           either identical canonical URLs or a title similarity at or above the configured
           threshold. This is the tier that merges one opening syndicated to two ATSs, and
           the only tier whose mistakes are visible and reversible.

        Args:
            raw: The freshly discovered posting.

        Returns:
            The existing row, or ``None`` when this posting is genuinely new.
        """
        hard = await self._find_by_identity(raw)
        if hard is not None:
            return hard
        return await self._find_fuzzy(raw)

    async def _find_by_identity(self, raw: RawPosting) -> JobPosting | None:
        """Run the two hard-identity tiers only.

        Kept separate from :meth:`find_existing` because it is also the recovery lookup
        after a lost insert race, and a race must be resolved by *identity* — the row that
        actually holds the conflicting unique value — never by a fuzzy guess.

        Args:
            raw: The posting being ingested.

        Returns:
            The existing row, or ``None``.
        """
        external_id = clean_text(raw.external_id)
        if external_id:
            result = await self._session.execute(
                select(JobPosting)
                .where(
                    JobPosting.provider == raw.provider,
                    JobPosting.external_id == external_id[:EXTERNAL_ID_MAX_LENGTH],
                )
                .limit(1)
            )
            row = result.scalars().first()
            if row is not None:
                logger.debug(
                    "dedupe.matched",
                    tier="external_id",
                    provider=raw.provider.value,
                    posting_id=str(row.id),
                )
                return row

        result = await self._session.execute(
            select(JobPosting).where(JobPosting.dedupe_key == dedupe_key(raw)).limit(1)
        )
        row = result.scalars().first()
        if row is not None:
            logger.debug(
                "dedupe.matched",
                tier="dedupe_key",
                provider=raw.provider.value,
                posting_id=str(row.id),
            )
        return row

    async def _find_fuzzy(self, raw: RawPosting) -> JobPosting | None:
        """Run the judgement tier: same employer, close in time, near-identical title.

        Args:
            raw: The posting being ingested.

        Returns:
            The row this posting is judged to duplicate, or ``None``.
        """
        # The broad key, not the model's: an opening syndicated as "Globex Corp" on one
        # board and "Globex Corporation" on another is one opening, and the whole point of
        # this tier is to catch exactly that.
        employers = await self._companies_matching(normalize_company(raw.company_name))
        company_ids: list[uuid.UUID] = [company.id for company in employers]
        if not company_ids:
            return None

        reference = _as_aware(raw.posted_at) or _utcnow()
        window_start = reference - self._window
        window_end = reference + self._window
        result = await self._session.execute(
            select(JobPosting)
            .where(
                JobPosting.company_id.in_(company_ids),
                or_(
                    JobPosting.posted_at.is_(None),
                    JobPosting.posted_at.between(window_start, window_end),
                ),
            )
            .order_by(JobPosting.created_at.desc())
            .limit(self._candidate_limit)
        )

        incoming_url = canonical_url(raw.url)
        for candidate in result.scalars().all():
            if not self._within_window(candidate, reference):
                continue
            if incoming_url and canonical_url(candidate.url) == incoming_url:
                logger.info(
                    "dedupe.matched",
                    tier="canonical_url",
                    provider=raw.provider.value,
                    posting_id=str(candidate.id),
                    url=incoming_url,
                )
                return candidate
            score = _title_similarity(raw.title, candidate.title)
            if score >= self._threshold:
                logger.info(
                    "dedupe.matched",
                    tier="fuzzy_title",
                    provider=raw.provider.value,
                    posting_id=str(candidate.id),
                    score=round(score, 4),
                    incoming_title=raw.title[:120],
                    existing_title=candidate.title[:120],
                )
                return candidate
        return None

    def _within_window(self, candidate: JobPosting, reference: datetime) -> bool:
        """Return whether *candidate* is close enough in time to be the same opening.

        The comparison uses the advertised publication date, falling back to when the row
        was first seen — a provider that publishes no date must not thereby escape the
        window entirely.

        Args:
            candidate: A stored posting.
            reference: The incoming posting's effective date.

        Returns:
            ``True`` when the two dates are at most :data:`FUZZY_WINDOW_DAYS` apart.
        """
        effective = _as_aware(candidate.posted_at) or _as_aware(candidate.created_at)
        if effective is None:
            return True
        return abs(effective - reference) <= self._window

    # ----------------------------------------------------------------------------------
    # Ingestion
    # ----------------------------------------------------------------------------------

    async def upsert(self, raw: RawPosting) -> tuple[JobPosting, bool]:
        """Persist *raw*, creating a row only when it is genuinely a new opening.

        The single entry point for turning discovery output into database state. An
        existing row has its mutable fields refreshed (:data:`MUTABLE_POSTING_FIELDS`) and
        its ``content_hash`` recomputed; its ``dedupe_key`` and ``external_id`` are never
        rewritten, because those are the identity other tables and other users' application
        history already point at.

        A new row is inserted inside a SAVEPOINT. If a concurrent poller wins the unique
        index, the savepoint rolls back alone, the outer transaction survives, and the
        winner's row is refreshed and returned with ``created=False``.

        This is also where the first two rungs of the ``/metrics`` funnel are produced
        (``docs/CONTRACTS.md`` §16): ``applicantos_postings_discovered_total{provider}`` for
        every posting that arrives here, and ``applicantos_postings_deduped_total{provider}``
        for the subset that collapsed onto a row that already existed.

        Args:
            raw: The discovered posting.

        Returns:
            ``(posting, created)`` — the persisted row and whether this call inserted it.

        Raises:
            IntegrityError: If an insert loses a race *and* the winning row cannot then be
                found. That combination means the conflict was not the one this method
                understands, and swallowing it would silently drop a posting.
        """
        # ``applicantos_postings_discovered_total`` counts what the provider *returned*, so
        # it is incremented once per call, before the tiers run — every posting that
        # collapses onto an existing row was still discovered. The deduped counter below is
        # therefore a subset of this one, which is what makes the funnel panel readable.
        record_posting_discovered(raw.provider)

        company = await self._resolve_company(raw)

        existing = await self.find_existing(raw)
        if existing is not None:
            if self._refresh(existing, raw, company):
                await self._session.flush()
            record_posting_deduped(raw.provider)
            logger.debug(
                "dedupe.posting_deduped",
                provider=raw.provider.value,
                posting_id=str(existing.id),
                external_id=raw.external_id[:64],
            )
            return existing, False

        posting = self._build_posting(raw, company)
        try:
            async with self._session.begin_nested():
                self._session.add(posting)
                await self._session.flush()
        except IntegrityError:
            self._detach(posting)
            logger.info(
                "dedupe.posting_insert_raced",
                provider=raw.provider.value,
                external_id=raw.external_id[:64],
            )
            raced = await self._find_by_identity(raw)
            if raced is None:
                raise
            if self._refresh(raced, raw, company):
                await self._session.flush()
            record_posting_deduped(raw.provider)
            return raced, False

        logger.debug(
            "dedupe.posting_created",
            provider=raw.provider.value,
            posting_id=str(posting.id),
            external_id=posting.external_id[:64],
        )
        return posting, True

    async def bulk_upsert(self, raws: Iterable[RawPosting]) -> tuple[int, int]:
        """Ingest a batch of postings.

        Sequential rather than concurrent on purpose: a single
        :class:`~sqlalchemy.ext.asyncio.AsyncSession` is not safe to drive from several
        tasks at once, and ordering matters — the second copy of a job in the same batch has
        to see the first, which it does because each insert is flushed before the next
        lookup runs.

        Args:
            raws: The discovered postings, in any order.

        Returns:
            ``(created, deduped)``: how many rows were inserted, and how many incoming
            postings collapsed onto a row that already existed. The two always sum to the
            number of postings consumed.
        """
        created = 0
        deduped = 0
        for raw in raws:
            _, was_created = await self.upsert(raw)
            if was_created:
                created += 1
            else:
                deduped += 1
        logger.info("dedupe.bulk_upsert", created=created, deduped=deduped)
        return created, deduped

    # ----------------------------------------------------------------------------------
    # Row construction and refresh
    # ----------------------------------------------------------------------------------

    async def _resolve_company(self, raw: RawPosting) -> Company | None:
        """Resolve *raw*'s employer, tolerating a posting that names none.

        Args:
            raw: The posting being ingested.

        Returns:
            The employer row, or ``None`` when the feed supplied no usable name.
            ``job_postings.company_id`` is nullable precisely so that a posting is never
            lost for want of an employer.
        """
        try:
            return await self.get_or_create_company(raw.company_name)
        except ValueError:
            logger.info(
                "dedupe.posting_without_company",
                provider=raw.provider.value,
                external_id=raw.external_id[:64],
            )
            return None

    def _build_posting(self, raw: RawPosting, company: Company | None) -> JobPosting:
        """Map a discovered posting onto a new, unflushed row.

        Args:
            raw: The discovered posting.
            company: Its resolved employer, when it has one.

        Returns:
            The populated :class:`~app.models.posting.JobPosting`, not yet added to the
            session.
        """
        key = dedupe_key(raw)
        title = _truncate(raw.title, POSTING_TITLE_MAX_LENGTH, field="title")
        company_name = company.name if company is not None else raw.company_name

        return JobPosting(
            company_id=company.id if company is not None else None,
            provider=raw.provider,
            external_id=self._external_id_for(raw, key),
            url=raw.url,
            apply_url=raw.apply_url,
            title=title,
            description=raw.description,
            location=(
                _truncate(raw.location, POSTING_LOCATION_MAX_LENGTH, field="location")
                if raw.location
                else None
            ),
            work_arrangement=raw.work_arrangement,
            employment_type=raw.employment_type,
            salary_min=raw.salary_min,
            salary_max=raw.salary_max,
            salary_currency=raw.salary_currency,
            posted_at=raw.posted_at,
            closes_at=raw.closes_at,
            raw_json=dict(raw.raw),
            content_hash=_content_hash_of(
                title=title, company_name=company_name, description=raw.description
            ),
            dedupe_key=key,
            status=PostingStatus.DISCOVERED,
        )

    @staticmethod
    def _external_id_for(raw: RawPosting, key: str) -> str:
        """Return the value to store in ``external_id``.

        ``job_postings.external_id`` is ``NOT NULL`` and unique per provider, so a feed that
        supplies no identifier — a manually entered posting, a scraped careers page — cannot
        simply store ``""``: the second such posting from that provider would collide with
        the first and be mistaken for it. The dedupe key is substituted instead. It is
        derived from the employer, title and location, so it is deterministic across
        re-polls (the same posting keeps the same synthetic id) and distinct across
        different openings.

        Args:
            raw: The discovered posting.
            key: Its :func:`~app.jobs.dedupe.dedupe_key`.

        Returns:
            The provider's identifier, or the synthetic stand-in.
        """
        external_id = clean_text(raw.external_id)
        if external_id:
            return external_id[:EXTERNAL_ID_MAX_LENGTH]
        logger.info(
            "dedupe.external_id_synthesized",
            provider=raw.provider.value,
            title=raw.title[:80],
        )
        return key[:EXTERNAL_ID_MAX_LENGTH]

    def _refresh(self, posting: JobPosting, raw: RawPosting, company: Company | None) -> bool:
        """Bring an existing row up to date with a fresh sighting of the same posting.

        Three different rules apply, and keeping them apart is the point of this method:

        * **Mutable content** (:data:`MUTABLE_POSTING_FIELDS`) is overwritten whenever the
          provider supplied a value, because employers edit live postings.
        * **Blanks** — apply URL, location, publication date, arrangement, employment type,
          employer — are filled in when the stored row does not have them. A second provider
          often knows something the first did not.
        * **Identity** — ``provider``, ``external_id``, ``dedupe_key``, ``title``, ``url`` —
          is never rewritten. Those values are what other rows, other users' application
          history, and the unique indexes already point at.

        ``dedupe_key`` is the one identity field that may be *filled* when it is ``NULL``,
        and only after confirming no other row holds the key, since a row written before
        dedupe ran has no identity to protect yet.

        Args:
            posting: The stored row.
            raw: The fresh sighting.
            company: The employer resolved for *raw*.

        Returns:
            Whether anything changed.
        """
        changed = False

        if raw.description and raw.description != posting.description:
            posting.description = raw.description
            changed = True
        for field in ("salary_min", "salary_max", "salary_currency", "closes_at"):
            value = getattr(raw, field)
            if value is not None and value != getattr(posting, field):
                setattr(posting, field, value)
                changed = True
        if raw.raw and dict(raw.raw) != posting.raw_json:
            # Reassigned rather than mutated: JSON columns are not change tracked.
            posting.raw_json = dict(raw.raw)
            changed = True

        if posting.company_id is None and company is not None:
            posting.company_id = company.id
            changed = True
        if not posting.apply_url and raw.apply_url:
            posting.apply_url = raw.apply_url
            changed = True
        if not posting.location and raw.location:
            posting.location = _truncate(
                raw.location, POSTING_LOCATION_MAX_LENGTH, field="location"
            )
            changed = True
        if posting.posted_at is None and raw.posted_at is not None:
            posting.posted_at = raw.posted_at
            changed = True
        if (
            posting.work_arrangement is WorkArrangement.UNKNOWN
            and raw.work_arrangement is not WorkArrangement.UNKNOWN
        ):
            posting.work_arrangement = raw.work_arrangement
            changed = True
        if (
            posting.employment_type is EmploymentType.UNKNOWN
            and raw.employment_type is not EmploymentType.UNKNOWN
        ):
            posting.employment_type = raw.employment_type
            changed = True

        if changed:
            employer = company.name if company is not None else (raw.company_name or "")
            refreshed = _content_hash_of(
                title=posting.title,
                company_name=employer,
                description=posting.description,
            )
            if refreshed != posting.content_hash:
                # A changed hash is what invalidates the cached score and the cached
                # tailored resume for this posting (golden rule #9).
                posting.content_hash = refreshed
                logger.info(
                    "dedupe.posting_content_changed",
                    posting_id=str(posting.id),
                    provider=posting.provider.value,
                )
        return changed

    # ----------------------------------------------------------------------------------
    # Session plumbing
    # ----------------------------------------------------------------------------------

    def _detach(self, instance: object) -> None:
        """Drop a failed pending object from the session.

        Rolling back a SAVEPOINT already expunges anything that became pending inside it,
        so this is normally a no-op — but "normally" is not a guarantee across SQLAlchemy
        versions, and an instance left pending would have its failing INSERT retried on the
        next autoflush, turning one lost race into an unrecoverable transaction.

        Args:
            instance: The object whose insert failed.
        """
        try:
            self._session.expunge(instance)
        except InvalidRequestError:
            logger.debug("dedupe.instance_already_detached", instance=type(instance).__name__)
