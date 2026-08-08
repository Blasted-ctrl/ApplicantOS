"""Discovery — polling every enabled board and turning the results into scored postings.

This is the front half of the pipeline (``docs/CONTRACTS.md`` §13): find work, deduplicate
it, decide what it is worth. Nothing here knows the name of a single applicant tracking
system. Providers are resolved by string through :mod:`app.jobs.registry` (golden rule #5),
identity is decided by :class:`~app.services.dedupe_service.DedupeService`, and the number
comes from :class:`app.ai.scoring.Scorer`. Adding a board therefore changes nothing in this
file.

**Partial failure is the normal case, not the exception.** Job boards rate-limit, time out,
change their JSON and go down, and they do it one at a time. A discovery run that aborted
because one provider misbehaved would leave the user with an empty feed and no explanation,
so every provider is isolated: a :class:`~app.jobs.base.ProviderRateLimitError` logs and
skips *that board* for this cycle, any other failure is recorded in
:attr:`DiscoveryReport.errors`, and the remaining providers run to completion. Individual
postings are isolated the same way — one unparseable advertisement costs one advertisement.

**Scoring is deterministic by default.** :meth:`DiscoveryService.score_new` runs
:meth:`app.ai.scoring.Scorer.score_rules`, which is pure, synchronous and free, so the whole
pipeline works with zero API keys. When a real model is configured
(``settings.llm_provider != "null"``) the optional ``±10`` adjustment pass runs instead; it
can never flip a hard negative into "apply", and any failure of it falls back to the rule
score untouched (``docs/CONTRACTS.md`` §10).

Like every service here this one flushes but never commits — the caller owns the unit of
work.
"""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from app.ai.scoring import VERDICT_SKIP, Scorer, ScoreResult, load_rules
from app.jobs.base import (
    DEFAULT_SEARCH_LIMIT,
    ProviderError,
    ProviderRateLimitError,
    SearchQuery,
)
from app.jobs.registry import resolve_providers
from app.models.enums import PostingStatus
from app.models.posting import JobPosting
from app.models.score import JobScore
from app.models.user import User, UserPreferences
from app.services.dedupe_service import DedupeService

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config.settings import Settings
    from app.jobs.base import ATSProvider, RawPosting

__all__ = [
    "DEFAULT_KEYWORDS",
    "DEFAULT_POSTED_WITHIN_DAYS",
    "EVENT_PROVIDER_FAILED",
    "EVENT_PROVIDER_FINISHED",
    "EVENT_PROVIDER_STARTED",
    "RESCORABLE_STATUSES",
    "DiscoveryReport",
    "DiscoveryService",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Defaults and vocabulary
# ======================================================================================

#: Search terms used when a user has expressed no preference and no desired role. Broad on
#: purpose: a first run that returns something the user can react to teaches the system more
#: than an empty feed does, and the scorer filters what the query cannot.
DEFAULT_KEYWORDS: Final[tuple[str, ...]] = ("software engineer",)

#: Location filter implied by ``remote_only`` when the user named no places. Boards that
#: cannot filter on it ignore it; the ``not_remote`` preference gate catches the rest.
DEFAULT_REMOTE_LOCATION: Final[str] = "Remote"

#: Freshness window applied when the caller does not supply a query. A month is roughly how
#: long a posting stays genuinely open, and it keeps a first run from ingesting a board's
#: entire history.
DEFAULT_POSTED_WITHIN_DAYS: Final[int] = 30

#: Progress events handed to ``progress_callback``. Named constants because the desktop app
#: subscribes to them by string (``docs/CONTRACTS.md`` §14).
EVENT_PROVIDER_STARTED: Final[str] = "provider.started"
EVENT_PROVIDER_FINISHED: Final[str] = "provider.finished"
EVENT_PROVIDER_FAILED: Final[str] = "provider.failed"

#: Statuses whose posting may have its lifecycle state rewritten by a scoring pass. A
#: posting that has been queued, applied to, or escalated to a human has moved *past*
#: scoring, and quietly resetting it to ``scored`` would re-offer work that is already done
#: — the failure golden rule #1 exists to prevent.
RESCORABLE_STATUSES: Final[frozenset[PostingStatus]] = frozenset(
    {
        PostingStatus.DISCOVERED,
        PostingStatus.DEDUPED,
        PostingStatus.SCORED,
        PostingStatus.SKIPPED,
    }
)

#: Signature of the optional progress hook: an event name and a JSON-ready payload.
ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


# ======================================================================================
# The report
# ======================================================================================


@dataclass(slots=True)
class DiscoveryReport:
    """What one discovery run did.

    Returned by :meth:`DiscoveryService.discover` and rendered by the desktop app's run
    panel, so the counters are defined in terms a user can act on rather than in terms of
    the code paths that produced them.

    Attributes:
        found: Postings the providers yielded, before deduplication. ``found`` always equals
            ``created + deduped + skipped``.
        created: New :class:`~app.models.posting.JobPosting` rows.
        deduped: Postings that collapsed onto a row that already existed — a re-poll, a
            second provider carrying the same job, or a near-duplicate listing.
        scored: Postings scored by this run. Only :meth:`DiscoveryService.discover_and_score`
            fills this in; plain :meth:`~DiscoveryService.discover` leaves it at zero.
        skipped: Postings that could not be ingested at all and were dropped.
        errors: Human-readable failures, one line each, safe to show an operator. A
            populated list with a non-zero ``found`` is a *partial* run, not a failed one.
        by_provider: Postings found, keyed by provider name. A provider that was polled and
            yielded nothing appears with ``0``, which is how "the board is empty" is
            distinguished from "the board was never asked".
        duration_seconds: Wall-clock time the run took.
    """

    found: int = 0
    created: int = 0
    deduped: int = 0
    scored: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    by_provider: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Whether the run completed without recording a single failure."""
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """Return the report as a JSON-ready mapping.

        Returns:
            Every counter plus the error list, in the shape the API and the progress
            callback publish.
        """
        return {
            "found": self.found,
            "created": self.created,
            "deduped": self.deduped,
            "scored": self.scored,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "by_provider": dict(self.by_provider),
            "duration_seconds": round(self.duration_seconds, 3),
        }


# ======================================================================================
# The service
# ======================================================================================


class DiscoveryService:
    """Polls providers, ingests what they return, and scores the result.

    Args:
        session: The unit of work. Flushed, never committed.
        settings: Application settings. Consulted for the default freshness window and to
            decide whether the optional language-model scoring pass is available.
        progress_callback: Awaited once per provider with an event name and a payload, so
            the desktop app can render a live run. A callback that raises is logged and
            ignored — telemetry may never abort a discovery run.
        dedupe: An explicit :class:`~app.services.dedupe_service.DedupeService`; one is
            built over *session* when omitted.
        use_llm: Force the optional model pass on or off. ``None`` (the default) enables it
            exactly when a real model provider is configured, which is what keeps a
            zero-API-key install fully deterministic.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        progress_callback: ProgressCallback | None = None,
        dedupe: DedupeService | None = None,
        use_llm: bool | None = None,
    ) -> None:
        """Bind the service to a session, its settings and its optional progress hook."""
        self._session = session
        self._settings = settings
        self._progress = progress_callback
        self._dedupe = dedupe if dedupe is not None else DedupeService(session)
        configured = str(getattr(settings, "llm_provider", "null") or "null")
        self._use_llm = (configured != "null") if use_llm is None else bool(use_llm)

    # ----------------------------------------------------------------------------------
    # Query construction
    # ----------------------------------------------------------------------------------

    def build_query(self, user: User, *, limit: int | None = None) -> SearchQuery:
        """Turn a user's preferences into the query every provider will be given.

        Preferences come first, then the profile's desired roles, then
        :data:`DEFAULT_KEYWORDS` — a run that returns nothing because the user has not
        filled in a settings screen yet is worse than a broad one, and the scorer is what
        actually decides fit.

        Only already-loaded profile state is consulted, so this never emits SQL and is safe
        to call synchronously from inside an async session.

        Args:
            user: The user to search for.
            limit: Per-provider posting ceiling. Defaults to
                :data:`~app.jobs.base.DEFAULT_SEARCH_LIMIT`.

        Returns:
            The search query. ``keywords`` is never empty; ``locations`` may be, which every
            provider reads as "anywhere".
        """
        prefs = user.prefs

        keywords = [term for term in prefs.preferred_keywords if term.strip()]
        if not keywords:
            keywords = self._desired_roles(user)
        if not keywords:
            keywords = list(DEFAULT_KEYWORDS)
            logger.debug("discovery.default_keywords_used", user_id=str(user.id))

        locations = [place for place in prefs.preferred_locations if place.strip()]
        if not locations and prefs.remote_only:
            locations = [DEFAULT_REMOTE_LOCATION]

        return SearchQuery(
            keywords=keywords,
            locations=locations,
            remote_only=prefs.remote_only,
            posted_within_days=DEFAULT_POSTED_WITHIN_DAYS,
            limit=limit if limit is not None else DEFAULT_SEARCH_LIMIT,
        )

    @staticmethod
    def _desired_roles(user: User) -> list[str]:
        """Read the roles a user said they want, without touching the database.

        ``User.profile`` is configured ``lazy="selectin"`` and so is normally present, but a
        detached or expired instance would emit SQL on attribute access — and inside an
        async session that raises ``MissingGreenlet``. Only loaded instance state is read.

        Args:
            user: The user.

        Returns:
            The desired role titles, or ``[]`` when there is no loaded profile.
        """
        profile = user.__dict__.get("profile")
        if profile is None:
            return []
        roles = getattr(profile, "desired_roles", None)
        if not isinstance(roles, list):
            return []
        return [str(role).strip() for role in roles if str(role).strip()]

    # ----------------------------------------------------------------------------------
    # Discovery
    # ----------------------------------------------------------------------------------

    async def discover(
        self,
        user_id: uuid.UUID | str,
        providers: Sequence[str] | None = None,
        query: SearchQuery | None = None,
    ) -> DiscoveryReport:
        """Poll every selected provider and ingest what they return.

        Providers are resolved through :func:`app.jobs.registry.resolve_providers`, which
        skips names that are unknown or switched off rather than refusing to poll anything —
        a stale entry in ``providers_enabled`` must not silence every other board.

        Each provider is isolated. A rate limit skips that board for this cycle; any other
        provider failure is recorded and the run continues; a posting that cannot be
        ingested costs one posting. ``query.limit`` is applied *per provider*, so a
        two-board run may ingest up to twice the limit.

        Args:
            user_id: Whose preferences drive the run.
            providers: Provider names to poll. ``None`` uses
                ``UserPreferences.providers_enabled``.
            query: An explicit query. ``None`` builds one with :meth:`build_query`.

        Returns:
            The run's counters. :attr:`DiscoveryReport.scored` is always ``0`` — use
            :meth:`discover_and_score` to score in the same pass.

        Raises:
            LookupError: If no user with *user_id* exists.
        """
        report, _ = await self._discover(user_id, providers, query)
        return report

    async def _discover(
        self,
        user_id: uuid.UUID | str,
        providers: Sequence[str] | None,
        query: SearchQuery | None,
    ) -> tuple[DiscoveryReport, list[uuid.UUID]]:
        """Run discovery and also report which postings it touched.

        Split out of :meth:`discover` so :meth:`discover_and_score` can score exactly the
        postings this run produced — including the ones that deduplicated onto an existing
        row, whose description may just have been refreshed — instead of re-scanning the
        whole table.

        Args:
            user_id: Whose preferences drive the run.
            providers: Provider names, or ``None`` for the user's enabled set.
            query: An explicit query, or ``None``.

        Returns:
            The report and the touched posting ids, de-duplicated and in first-seen order.
        """
        started = time.monotonic()
        user = await self._load_user(user_id)
        report = DiscoveryReport()
        touched: dict[uuid.UUID, None] = {}

        effective_query = query if query is not None else self.build_query(user)
        names = list(providers) if providers else list(user.prefs.providers_enabled)
        resolved = resolve_providers(names)

        if not resolved:
            message = f"no usable providers among {names or ['<all>']}"
            logger.warning("discovery.no_providers", requested=names)
            report.errors.append(message)

        logger.info(
            "discovery.started",
            user_id=str(user.id),
            providers=[provider.meta.name for provider in resolved],
            keywords=effective_query.keywords,
            limit=effective_query.limit,
        )

        for provider in resolved:
            await self._poll_provider(provider, effective_query, report, touched)

        report.duration_seconds = time.monotonic() - started
        logger.info("discovery.finished", user_id=str(user.id), **report.as_dict())
        return report, list(touched)

    async def _poll_provider(
        self,
        provider: ATSProvider,
        query: SearchQuery,
        report: DiscoveryReport,
        touched: dict[uuid.UUID, None],
    ) -> None:
        """Poll one provider, ingesting each posting as it arrives.

        Postings are ingested inside the iteration rather than collected first, so a board
        that dies halfway through still contributes everything it managed to yield.

        Args:
            provider: The provider to poll.
            query: The search to run.
            report: Counters to update in place.
            touched: Ordered set of posting ids this run has written, updated in place.
        """
        name = provider.meta.name
        report.by_provider.setdefault(name, 0)
        await self._emit(EVENT_PROVIDER_STARTED, {"provider": name, "limit": query.limit})

        found = 0
        try:
            stream = provider.search(query)
            if inspect.isawaitable(stream):
                # A provider may implement `search` as a coroutine returning an iterator
                # rather than as an async generator; both satisfy the contract's signature.
                stream = await stream
            async for raw in stream:
                if found >= query.limit:
                    logger.debug(
                        "discovery.provider_limit_reached", provider=name, limit=query.limit
                    )
                    break
                found += 1
                report.found += 1
                report.by_provider[name] = report.by_provider.get(name, 0) + 1
                await self._ingest(raw, report, touched)
        except ProviderRateLimitError as exc:
            # Not a bug and not worth an alarm: the board asked us to back off, so this
            # cycle ends for this board and the next poll picks it up again.
            retry_after = getattr(exc, "retry_after", None)
            logger.info(
                "discovery.provider_rate_limited",
                provider=name,
                retry_after=retry_after,
                found=found,
            )
            report.errors.append(f"{name}: rate limited, skipped for this run")
            await self._emit(
                EVENT_PROVIDER_FAILED,
                {"provider": name, "reason": "rate_limited", "retry_after": retry_after},
            )
            return
        except ProviderError as exc:
            logger.warning(
                "discovery.provider_failed",
                provider=name,
                error=str(exc),
                error_type=type(exc).__name__,
                found=found,
            )
            report.errors.append(f"{name}: {exc}")
            await self._emit(
                EVENT_PROVIDER_FAILED, {"provider": name, "reason": type(exc).__name__}
            )
            return
        except Exception as exc:
            logger.exception(
                "discovery.provider_crashed",
                provider=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            report.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            await self._emit(
                EVENT_PROVIDER_FAILED, {"provider": name, "reason": type(exc).__name__}
            )
            return

        await self._emit(
            EVENT_PROVIDER_FINISHED,
            # ``provider_found`` is this board's own tally; the spread carries the run's
            # running totals, and the two must not share a key.
            {"provider": name, "provider_found": found, **report.as_dict()},
        )

    async def _ingest(
        self,
        raw: RawPosting,
        report: DiscoveryReport,
        touched: dict[uuid.UUID, None],
    ) -> None:
        """Persist one discovered posting, counting the outcome.

        Args:
            raw: The posting as the provider found it.
            report: Counters to update in place.
            touched: Ordered set of posting ids, updated in place.
        """
        try:
            posting, created = await self._dedupe.upsert(raw)
        except Exception as exc:
            logger.warning(
                "discovery.ingest_failed",
                provider=raw.provider.value,
                external_id=raw.external_id[:64],
                error=str(exc),
                error_type=type(exc).__name__,
            )
            report.skipped += 1
            report.errors.append(
                f"{raw.provider.value}: could not ingest {raw.title[:60]!r}: {exc}"
            )
            return

        if created:
            report.created += 1
        else:
            report.deduped += 1
        if posting.id is not None:
            touched[posting.id] = None

    # ----------------------------------------------------------------------------------
    # Scoring
    # ----------------------------------------------------------------------------------

    async def score_new(
        self,
        user_id: uuid.UUID | str,
        posting_ids: Iterable[uuid.UUID] | None = None,
    ) -> int:
        """Score postings for one user and route them by verdict.

        The scorer is built once per call from the packaged rule pack
        (:func:`app.ai.scoring.load_rules`) and the user's preferences, so every posting in
        the batch is judged against exactly the same rules — the dashboard ranks these
        numbers against each other and a pack that changed mid-batch would make every
        comparison it shows a lie.

        Each result is upserted onto ``UNIQUE(posting_id, user_id)``, so re-scoring replaces
        a verdict instead of accumulating contradictory ones, and the posting is routed:
        ``skip`` sends it to :attr:`~app.models.enums.PostingStatus.SKIPPED`, anything else
        to :attr:`~app.models.enums.PostingStatus.SCORED`. A posting that has already moved
        past scoring — queued, applied, awaiting a human — keeps its status and only has its
        score refreshed.

        Args:
            user_id: Whose preferences to score against.
            posting_ids: Postings to score. ``None`` means "every posting this user has no
                score for"; an explicit list is scored whether or not it has been scored
                before, which is what makes it correct to re-run after a description
                changed. An empty iterable scores nothing.

        Returns:
            How many postings were scored.

        Raises:
            LookupError: If no user with *user_id* exists.
        """
        user = await self._load_user(user_id)
        prefs: UserPreferences = user.prefs
        scorer = Scorer(load_rules(), prefs)

        postings = await self._postings_to_score(user.id, posting_ids)
        if not postings:
            logger.debug("discovery.nothing_to_score", user_id=str(user.id))
            return 0

        verdicts: dict[str, int] = {}
        for posting in postings:
            result = await self._score_one(scorer, posting)
            await self._upsert_score(posting, user.id, result)
            self._route(posting, result)
            verdicts[result.verdict] = verdicts.get(result.verdict, 0) + 1

        await self._session.flush()
        logger.info(
            "discovery.scored",
            user_id=str(user.id),
            count=len(postings),
            verdicts=verdicts,
            use_llm=self._use_llm,
        )
        return len(postings)

    async def _score_one(self, scorer: Scorer, posting: JobPosting) -> ScoreResult:
        """Score one posting, with or without the optional model pass.

        Args:
            scorer: The scorer bound to this user's preferences and rule pack.
            posting: The posting to judge.

        Returns:
            The itemised result. When the model pass is enabled it may move the total by
            ``±10`` and attach a rationale, but it can never overturn a hard negative, and
            any failure of it degrades silently to the rule score.
        """
        if not self._use_llm:
            return scorer.score_rules(posting)
        return await scorer.score(posting, use_llm=True)

    async def _postings_to_score(
        self,
        user_id: uuid.UUID,
        posting_ids: Iterable[uuid.UUID] | None,
    ) -> list[JobPosting]:
        """Select the postings this scoring pass will judge.

        Args:
            user_id: The scoring user.
            posting_ids: Explicit ids, or ``None`` for "everything unscored".

        Returns:
            The postings, oldest first so a long backlog is worked through in discovery
            order.
        """
        statement = select(JobPosting)
        if posting_ids is not None:
            ids = list(posting_ids)
            if not ids:
                return []
            statement = statement.where(JobPosting.id.in_(ids))
        else:
            scored = (
                select(JobScore.id)
                .where(JobScore.posting_id == JobPosting.id, JobScore.user_id == user_id)
                .exists()
            )
            statement = statement.where(~scored)
        statement = statement.order_by(JobPosting.created_at.asc())
        result = await self._session.execute(statement)
        return list(result.scalars().unique().all())

    async def _upsert_score(
        self,
        posting: JobPosting,
        user_id: uuid.UUID,
        result: ScoreResult,
    ) -> JobScore:
        """Write one score, replacing any previous verdict for the same pair.

        The insert runs inside a SAVEPOINT for the same reason ingestion does: two workers
        can score one posting for one user at the same instant, and the loser must re-select
        and update rather than poison the whole transaction.

        Args:
            posting: The scored posting.
            user_id: The scoring user.
            result: The scorer's output.

        Returns:
            The persisted :class:`~app.models.score.JobScore`.

        Raises:
            IntegrityError: If the insert loses a race and the winning row cannot then be
                found.
        """
        existing = await self._session.scalar(
            select(JobScore)
            .where(JobScore.posting_id == posting.id, JobScore.user_id == user_id)
            .limit(1)
        )
        if existing is not None:
            self._write_score(existing, result)
            await self._session.flush()
            return existing

        score = JobScore(posting_id=posting.id, user_id=user_id)
        self._write_score(score, result)
        try:
            async with self._session.begin_nested():
                self._session.add(score)
                await self._session.flush()
        except IntegrityError:
            try:
                self._session.expunge(score)
            except InvalidRequestError:
                logger.debug("discovery.score_already_detached", posting_id=str(posting.id))
            logger.info("discovery.score_insert_raced", posting_id=str(posting.id))
            raced = await self._session.scalar(
                select(JobScore)
                .where(JobScore.posting_id == posting.id, JobScore.user_id == user_id)
                .limit(1)
            )
            if raced is None:
                raise
            self._write_score(raced, result)
            await self._session.flush()
            return raced
        return score

    @staticmethod
    def _write_score(score: JobScore, result: ScoreResult) -> None:
        """Copy a scoring result onto a row.

        The deterministic figures and the model's prose are written to separate columns and
        stay that way (``docs/CONTRACTS.md`` §10): ``rationale`` is commentary and must
        never be mistaken for the source of the number.

        Args:
            score: The row to write.
            result: The scorer's output.
        """
        score.total = result.total
        score.normalized = result.normalized
        # Reassigned wholesale rather than mutated: JSON columns are not change tracked.
        score.breakdown = result.to_breakdown()
        score.verdict = result.verdict
        score.model_used = result.model_used
        score.rationale = result.rationale

    @staticmethod
    def _route(posting: JobPosting, result: ScoreResult) -> None:
        """Move a posting's lifecycle state to match its verdict.

        Args:
            posting: The scored posting.
            result: The scorer's output.
        """
        if posting.status not in RESCORABLE_STATUSES:
            logger.debug(
                "discovery.status_preserved",
                posting_id=str(posting.id),
                status=posting.status.value,
                verdict=result.verdict,
            )
            return
        posting.status = (
            PostingStatus.SKIPPED if result.verdict == VERDICT_SKIP else PostingStatus.SCORED
        )

    # ----------------------------------------------------------------------------------
    # Combined pass
    # ----------------------------------------------------------------------------------

    async def discover_and_score(
        self,
        user_id: uuid.UUID | str,
        providers: Sequence[str] | None = None,
        query: SearchQuery | None = None,
    ) -> DiscoveryReport:
        """Discover, then score exactly what was discovered.

        The convenience path the ``POST /postings/discover`` route and the
        ``jobs.poll_all`` worker both want: one report covering both halves, with
        :attr:`DiscoveryReport.duration_seconds` spanning the whole thing.

        Only the postings this run touched are scored — including ones that deduplicated
        onto an existing row, since a refreshed description invalidates the previous score.

        Args:
            user_id: Whose preferences drive the run.
            providers: Provider names to poll, or ``None`` for the user's enabled set.
            query: An explicit query, or ``None`` to build one.

        Returns:
            The combined report.

        Raises:
            LookupError: If no user with *user_id* exists.
        """
        started = time.monotonic()
        report, posting_ids = await self._discover(user_id, providers, query)
        if posting_ids:
            report.scored = await self.score_new(user_id, posting_ids)
        report.duration_seconds = time.monotonic() - started
        return report

    # ----------------------------------------------------------------------------------
    # Plumbing
    # ----------------------------------------------------------------------------------

    async def _load_user(self, user_id: uuid.UUID | str) -> User:
        """Load the user a run belongs to.

        Args:
            user_id: The user's identifier, as a UUID or its string form.

        Returns:
            The user row, with its eager relationships loaded.

        Raises:
            LookupError: If the identifier is malformed or names no user. Discovery writes
                user-owned rows, so continuing without a real user would attach postings and
                scores to nobody.
        """
        try:
            identifier = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        except ValueError as exc:
            raise LookupError(f"{user_id!r} is not a valid user id") from exc

        user = await self._session.scalar(select(User).where(User.id == identifier).limit(1))
        if user is None:
            raise LookupError(f"user {identifier} not found")
        return user

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """Publish a progress event, tolerating a broken subscriber.

        Args:
            event: One of :data:`EVENT_PROVIDER_STARTED`,
                :data:`EVENT_PROVIDER_FINISHED`, :data:`EVENT_PROVIDER_FAILED`.
            payload: JSON-ready event data.
        """
        if self._progress is None:
            return
        try:
            await self._progress(event, payload)
        except Exception as exc:
            logger.warning(
                "discovery.progress_callback_failed",
                event=event,
                error=str(exc),
                error_type=type(exc).__name__,
            )
