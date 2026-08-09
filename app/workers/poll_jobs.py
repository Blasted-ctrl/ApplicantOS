"""Discovery and scoring tasks — ``jobs.*`` (``docs/CONTRACTS.md`` §15).

Three tasks, arranged as a fan-out so that one slow or rate-limited board never delays the
others:

``jobs.poll_all``
    The scheduler's entry point, every thirty minutes. It polls nothing itself. It resolves
    the active users and, for each, the providers that user has enabled, and enqueues one
    ``jobs.poll_provider`` per pair. Fanning out here rather than looping in one task is what
    makes a single ``ProviderRateLimitError`` cost one board for one cycle instead of
    emptying the whole feed.

``jobs.poll_provider``
    Polls exactly one board for exactly one user and scores what came back, via
    :meth:`~app.services.discovery_service.DiscoveryService.discover_and_score` — one
    :class:`~app.ai.scoring.Scorer` built once for the whole batch, so every posting in a run
    is judged against identical rules and the dashboard's rankings mean something.

``jobs.score_posting``
    Re-scores one posting on demand — after a description changed, or when a user's
    preferences did — and is the single place that decides whether an application should be
    prepared. That decision lives in :func:`_enqueue_qualified` and nowhere else.

Both scoring paths gate the hand-off to ``apply.prepare`` on ``settings.auto_apply_enabled``.
That is not the kill switch doing its job — :meth:`app.services.pipeline.Pipeline.submit`
re-reads *both* switches on every submission and is the only thing that can send an
application. It is simply pointless to prepare documents nobody asked for.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from sqlalchemy import select

from app.api.events import EVENT_POSTING_DISCOVERED, EVENT_POSTING_SCORED, bus
from app.api.tasks import (
    TASK_APPLY_PREPARE,
    TASK_JOBS_POLL_ALL,
    TASK_JOBS_POLL_PROVIDER,
    TASK_JOBS_SCORE_POSTING,
)
from app.config.settings import get_settings
from app.database.session import session_scope
from app.workers import run_async, task_span
from app.workers.celery_app import celery_app, enqueue
from app.workers.retry import retryable

__all__ = ["MAX_APPLY_FANOUT", "poll_all", "poll_provider", "score_posting"]

logger = structlog.get_logger(__name__)

#: Ceiling on how many ``apply.prepare`` tasks one scoring pass may enqueue. The daily cap in
#: :meth:`app.services.pipeline.Pipeline.submit` is the real limit on submissions; this one
#: stops a first-run poll that discovered two thousand postings from queueing two thousand
#: document generations before the cap has a chance to refuse them.
MAX_APPLY_FANOUT: Final[int] = 100

#: Session counters a discovery run reports, mapped from
#: :class:`~app.services.discovery_service.DiscoveryReport`.
_FOUND_COUNTER: Final[str] = "jobs_found"
_QUALIFIED_COUNTER: Final[str] = "jobs_qualified"


# ======================================================================================
# Shared helpers
# ======================================================================================


def _search_query(query: dict[str, Any] | None) -> Any:
    """Build a :class:`~app.jobs.base.SearchQuery` from a serialised request body.

    The queue carries JSON, so the ``POST /postings/discover`` body arrives as a plain
    mapping. ``providers`` is stripped: which boards to poll is the task's own argument, not
    part of the query.

    Args:
        query: The serialised
            :class:`~app.schemas.posting.DiscoverRequest`, or ``None`` to let the service
            build a query from the user's stored preferences.

    Returns:
        A ``SearchQuery``, or ``None`` when *query* was ``None`` or empty.
    """
    if not query:
        return None

    from app.jobs.base import SearchQuery

    # ``query`` is a serialised request body, so every value is ``Any``; annotating the
    # accumulator says that plainly instead of letting the checker infer a union of the five
    # observed value types and then reject ``**fields`` against every parameter in turn.
    fields: dict[str, Any] = {
        "keywords": list(query.get("keywords") or []),
        "locations": list(query.get("locations") or []),
        "remote_only": bool(query.get("remote_only", False)),
        "posted_within_days": query.get("posted_within_days"),
        "extra": dict(query.get("extra") or {}),
    }
    limit = query.get("limit")
    if limit:
        fields["limit"] = int(limit)
    return SearchQuery(**fields)


async def _active_user_ids(user_id: str | None) -> list[uuid.UUID]:
    """Resolve which users this poll covers.

    Args:
        user_id: An explicit user, or ``None`` for every active one — which is how the beat
            schedule calls it, since a scheduler has no user in hand.

    Returns:
        User ids. Empty when the named user does not exist or is deactivated, which is a
        no-op rather than an error: a user disabled between the enqueue and the run is an
        expected race, not a failure.
    """
    from app.models.user import User

    async with session_scope() as session:
        statement = select(User.id).where(User.is_active.is_(True))
        if user_id is not None:
            statement = statement.where(User.id == uuid.UUID(str(user_id)))
        rows = (await session.execute(statement.order_by(User.created_at.asc()))).scalars()
        return list(rows.all())


async def _enabled_providers(user_id: uuid.UUID) -> list[str]:
    """Return the provider names *user_id* has switched on.

    Resolved through :func:`app.jobs.registry.resolve_providers`, which drops names that are
    unknown or unavailable — a stale entry in ``providers_enabled`` must not silence the
    boards that still work (golden rule #5: never import a provider module directly).

    Args:
        user_id: The user whose preferences select the boards.

    Returns:
        Provider names, or an empty list when the user has none enabled.
    """
    from app.jobs.registry import resolve_providers
    from app.models.user import User

    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            return []
        names = list(user.prefs.providers_enabled or [])
    return [str(provider.name) for provider in resolve_providers(names or None)]


async def _qualified_posting_ids(
    session: Any,
    user_id: uuid.UUID,
    *,
    posting_ids: list[uuid.UUID] | None = None,
    limit: int = MAX_APPLY_FANOUT,
) -> list[uuid.UUID]:
    """Return postings this user should have an application prepared for.

    The one place the "is this worth applying to?" question is asked outside the pipeline,
    shared by both scoring paths so the answer cannot drift between them. It is a *scheduling*
    query — which outstanding work exists — not a policy decision: every guard that actually
    matters (the daily cap, the score floor, the provider's ToS posture, the kill switch) is
    re-evaluated by :meth:`app.services.pipeline.Pipeline.submit` at submission time.

    Postings that already have an application are excluded. That is an optimisation, not a
    safety control — ``UNIQUE(user_id, posting_id)`` and the status guard in ``submit`` are
    what enforce golden rule #1.

    Args:
        session: The open async session.
        user_id: The applicant.
        posting_ids: Restrict to these postings, or ``None`` for every scored posting.
        limit: Ceiling on the number returned.

    Returns:
        Posting ids, highest score first, so a capped run works on the best candidates.
    """
    from app.ai.scoring import VERDICT_APPLY
    from app.models.application import Application
    from app.models.enums import PostingStatus
    from app.models.posting import JobPosting
    from app.models.score import JobScore

    settings = get_settings()
    statement = (
        select(JobScore.posting_id)
        .join(JobPosting, JobPosting.id == JobScore.posting_id)
        .where(
            JobScore.user_id == user_id,
            JobScore.verdict == VERDICT_APPLY,
            JobScore.normalized >= int(settings.auto_apply_min_score),
            JobPosting.status == PostingStatus.SCORED,
            ~select(Application.id)
            .where(
                Application.user_id == user_id,
                Application.posting_id == JobScore.posting_id,
            )
            .exists(),
        )
        .order_by(JobScore.normalized.desc())
        .limit(max(1, int(limit)))
    )
    if posting_ids:
        statement = statement.where(JobScore.posting_id.in_(posting_ids))
    return list((await session.execute(statement)).scalars().all())


async def _enqueue_qualified(
    session: Any,
    user_id: uuid.UUID,
    *,
    posting_ids: list[uuid.UUID] | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Enqueue ``apply.prepare`` for every posting that qualifies, honouring the switch.

    Args:
        session: The open async session.
        user_id: The applicant.
        posting_ids: Restrict to these postings, or ``None`` for every scored posting.
        session_id: The run session to attribute the work to, when there is one.

    Returns:
        The posting ids handed to ``apply.prepare``. Empty when auto-apply is off, which is
        the default posture (golden rule #3).
    """
    settings = get_settings()
    if not settings.auto_apply_enabled:
        logger.info("jobs.apply_fanout_disabled", user_id=str(user_id))
        return []

    candidates = await _qualified_posting_ids(session, user_id, posting_ids=posting_ids)
    queued: list[str] = []
    for posting_id in candidates:
        task_id = enqueue(TASK_APPLY_PREPARE, str(posting_id), str(user_id), session_id=session_id)
        if task_id is not None:
            queued.append(str(posting_id))
    if queued:
        logger.info("jobs.apply_fanout", user_id=str(user_id), postings=len(queued))
    return queued


async def _record_session(session_id: str | None, **deltas: int) -> None:
    """Add *deltas* to a run session's counters, when the work belongs to a run.

    Args:
        session_id: The run session id, or ``None`` for work outside a run.
        **deltas: Counter increments, validated by
            :meth:`app.services.session_service.SessionService.record`.
    """
    if not session_id or not any(deltas.values()):
        return

    from app.services.session_service import SessionService

    try:
        async with session_scope() as session:
            await SessionService(session).record(session_id, **deltas)
    except (LookupError, ValueError) as exc:
        # A run that finished, or was reaped by the watchdog, while its tasks were still in
        # flight. Losing a counter is not a reason to fail a discovery run.
        logger.warning("jobs.session_record_failed", session_id=session_id, error=str(exc))


# ======================================================================================
# jobs.poll_all
# ======================================================================================


@celery_app.task(name=TASK_JOBS_POLL_ALL, bind=False)
@retryable()
def poll_all(
    user_id: str | None = None,
    *,
    query: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Fan out one ``jobs.poll_provider`` per active user per enabled provider.

    Called by beat every thirty minutes with no arguments, and by
    ``POST /postings/discover`` / ``POST /sessions/start`` with a specific user.

    Args:
        user_id: Poll for this user only. ``None`` — the scheduler's call — covers every
            active user.
        query: A serialised :class:`~app.schemas.posting.DiscoverRequest`, or ``None`` to
            build each user's query from their stored preferences.
        session_id: Attribute the discovered postings to this run session.

    Returns:
        ``{"users": int, "dispatched": int, "skipped": int, "tasks": [...]}``. ``skipped``
        counts users with no usable provider enabled.
    """
    with task_span(TASK_JOBS_POLL_ALL, user_id=user_id, session_id=session_id) as log:
        users = run_async(_active_user_ids(user_id))
        dispatched: list[dict[str, str]] = []
        skipped = 0

        for identifier in users:
            providers = run_async(_enabled_providers(identifier))
            if not providers:
                skipped += 1
                log.info("jobs.no_providers_enabled", user_id=str(identifier))
                continue
            for provider in providers:
                task_id = enqueue(
                    TASK_JOBS_POLL_PROVIDER,
                    str(identifier),
                    provider,
                    query=query,
                    session_id=session_id,
                )
                if task_id is not None:
                    dispatched.append(
                        {"user_id": str(identifier), "provider": provider, "task_id": task_id}
                    )

        log.info(
            "jobs.poll_all_fanned_out",
            users=len(users),
            dispatched=len(dispatched),
            skipped=skipped,
        )
        return {
            "users": len(users),
            "dispatched": len(dispatched),
            "skipped": skipped,
            "tasks": dispatched,
        }


# ======================================================================================
# jobs.poll_provider
# ======================================================================================


@celery_app.task(name=TASK_JOBS_POLL_PROVIDER, bind=False)
@retryable()
def poll_provider(
    user_id: str,
    provider: str,
    *,
    query: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Poll one board for one user, ingest what it returns, and score it.

    Delegates the whole run to
    :meth:`~app.services.discovery_service.DiscoveryService.discover_and_score`, which
    isolates the provider, deduplicates on ingest and scores only the postings this pass
    touched. The task adds nothing but the event, the session counters and the hand-off to
    ``apply.prepare``.

    Args:
        user_id: Whose preferences drive the run.
        provider: The single board to poll.
        query: A serialised :class:`~app.schemas.posting.DiscoverRequest`, or ``None``.
        session_id: The run session to credit.

    Returns:
        The :class:`~app.services.discovery_service.DiscoveryReport` as a mapping, plus
        ``queued_for_apply``.

    Raises:
        LookupError: If no active user has that id.
    """
    with task_span(
        TASK_JOBS_POLL_PROVIDER, user_id=user_id, provider=provider, session_id=session_id
    ) as log:

        async def _run() -> dict[str, Any]:
            """Poll, score, publish and fan out inside one unit of work."""
            from app.services.discovery_service import DiscoveryService

            settings = get_settings()
            async with session_scope() as session:
                service = DiscoveryService(session, settings)
                report = await service.discover_and_score(user_id, [provider], _search_query(query))
                await session.commit()
                queued = await _enqueue_qualified(
                    session, uuid.UUID(str(user_id)), session_id=session_id
                )

            payload = report.as_dict()
            payload["provider"] = provider
            payload["user_id"] = str(user_id)
            payload["queued_for_apply"] = len(queued)
            # One run-level frame per provider rather than one per posting: a poll that
            # ingested two hundred rows would otherwise cost two hundred frames to say the
            # one thing the run panel renders. The payload is
            # :meth:`~app.services.discovery_service.DiscoveryReport.as_dict`, the same body
            # ``POST /postings/discover`` reports.
            bus.publish(EVENT_POSTING_DISCOVERED, payload)
            return payload

        outcome = run_async(_run())
        run_async(
            _record_session(
                session_id,
                **{
                    _FOUND_COUNTER: int(outcome.get("found", 0)),
                    _QUALIFIED_COUNTER: int(outcome.get("queued_for_apply", 0)),
                },
            )
        )
        log.info(
            "jobs.polled",
            found=outcome.get("found"),
            created=outcome.get("created"),
            deduped=outcome.get("deduped"),
            scored=outcome.get("scored"),
            queued_for_apply=outcome.get("queued_for_apply"),
            errors=len(outcome.get("errors") or []),
        )
        return outcome


# ======================================================================================
# jobs.score_posting
# ======================================================================================


@celery_app.task(name=TASK_JOBS_SCORE_POSTING, bind=False)
@retryable()
def score_posting(
    posting_id: str,
    user_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Score one posting against one user, and queue an application if it qualifies.

    The on-demand path: a posting whose description changed, or a user whose preferences
    did. Scoring is upserted on ``UNIQUE(posting_id, user_id)``, so re-running replaces the
    verdict rather than accumulating contradictory ones.

    An application is prepared only when the verdict is ``apply``, the normalised score
    clears ``auto_apply_min_score``, and ``auto_apply_enabled`` is on.

    Args:
        posting_id: The posting to judge.
        user_id: Whose preferences to judge it against.
        session_id: The run session to attribute a resulting application to.

    Returns:
        ``{"posting_id", "user_id", "score", "verdict", "queued_for_apply"}``.

    Raises:
        LookupError: If the posting or the user does not exist.
    """
    with task_span(TASK_JOBS_SCORE_POSTING, posting_id=posting_id, user_id=user_id) as log:

        async def _run() -> dict[str, Any]:
            """Score, publish and decide inside one unit of work."""
            from app.schemas.scoring import ScoreRead
            from app.services.pipeline import Pipeline

            settings = get_settings()
            async with session_scope() as session:
                pipeline = Pipeline(session, settings)
                score = await pipeline.score_posting(posting_id, user_id)
                payload: dict[str, Any] = {
                    "posting_id": str(posting_id),
                    "user_id": str(user_id),
                    "score": int(score.normalized or 0),
                    "verdict": score.verdict,
                }
                # Published as the same schema ``GET /postings/{id}`` nests, so the desktop
                # app can ``setQueryData`` the new verdict without a refetch (§14).
                item = ScoreRead.model_validate(score)
                queued = await _enqueue_qualified(
                    session,
                    uuid.UUID(str(user_id)),
                    posting_ids=[uuid.UUID(str(posting_id))],
                    session_id=session_id,
                )

            payload["queued_for_apply"] = len(queued)
            bus.publish_model(EVENT_POSTING_SCORED, item)
            return payload

        outcome = run_async(_run())
        log.info(
            "jobs.scored",
            score=outcome.get("score"),
            verdict=outcome.get("verdict"),
            queued_for_apply=outcome.get("queued_for_apply"),
        )
        return outcome
