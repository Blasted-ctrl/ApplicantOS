"""The discovery feed (``docs/CONTRACTS.md`` §14).

``job_postings`` is where the pipeline starts and the busiest screen in the desktop app, so
these four endpoints are shaped by the read path more than any other group.

**The list returns** :class:`~app.schemas.posting.PostingRead`, **not the summary.** The
summary exists to keep rows small, but ``min_score`` is a frozen query parameter and a feed
that can filter by score while being unable to *show* it would force one detail request per
visible row — strictly more bytes than including the score, and a worse experience. The score
is not an ORM attribute of a posting (a posting has a *collection* of per-user scores), so it
is fetched for the whole page in one query and grafted on; naming a schema field ``scores``
would instead make pydantic read the relationship, which is the wrong one and the wrong
shape.

**No posting service exists**, so this module owns its query. It is the one place in the API
layer that builds SQL, and the query is written once, here, rather than being re-derived by
each filter.

**``POST /postings/{id}/apply`` does not apply.** It enqueues ``apply.run_one`` and returns
202. The kill switch stays where it belongs — inside
:meth:`app.services.pipeline.Pipeline.submit`, which checks ``auto_apply_enabled`` and
``dry_run`` independently of anything a request can say. The response reports
``submission_allowed`` so the desktop app can warn "this will be prepared but not sent"
before the user watches nothing happen.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, SettingsDep
from app.api.routes._support import like_pattern
from app.api.tasks import (
    TASK_APPLY_RUN_ONE,
    TASK_JOBS_POLL_ALL,
    TASK_JOBS_POLL_PROVIDER,
    dispatch,
)
from app.models.company import Company
from app.models.enums import WorkArrangement
from app.models.posting import JobPosting
from app.models.score import JobScore
from app.schemas.common import OkResponse, Page, paginate
from app.schemas.posting import DiscoverRequest, PostingFilter, PostingRead
from app.schemas.scoring import ScoreRead

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/postings"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["postings"]

#: Key under which a work-triggering response reports the effective kill-switch state.
SUBMISSION_ALLOWED_KEY: Final[str] = "submission_allowed"

router = APIRouter()


# ======================================================================================
# Query construction
# ======================================================================================


def _base_query(filters: PostingFilter, user_id: uuid.UUID) -> Select[Any]:
    """Build the ``WHERE`` clause behind ``GET /postings``.

    Returns a statement selecting :class:`~app.models.posting.JobPosting` with every filter
    applied and no ordering, limit or offset — so the same predicate set drives both the page
    and its total, and the two can never disagree about what "matching" means.

    Args:
        filters: The request's query parameters.
        user_id: The acting user, needed for the score-threshold join.

    Returns:
        The filtered statement.
    """
    statement: Select[Any] = select(JobPosting)

    if filters.q:
        pattern = like_pattern(filters.q)
        statement = statement.outerjoin(Company, JobPosting.company_id == Company.id).where(
            or_(
                JobPosting.title.ilike(pattern, escape="\\"),
                func.coalesce(JobPosting.description, "").ilike(pattern, escape="\\"),
                func.coalesce(Company.name, "").ilike(pattern, escape="\\"),
            )
        )

    if filters.provider is not None:
        statement = statement.where(JobPosting.provider == filters.provider)

    if filters.status is not None:
        statement = statement.where(JobPosting.status == filters.status)

    if filters.company:
        # Matched against the normalised form, so "Acme, Inc." and "acme inc" are the same
        # employer — which is the whole reason `companies.normalized_name` exists.
        normalized = Company.normalize(filters.company)
        statement = statement.where(
            JobPosting.company_id.in_(
                select(Company.id).where(Company.normalized_name == normalized)
            )
        )

    if filters.since is not None:
        # Postings from feeds that omit a publication date fall back to when we first saw
        # them; without the coalesce, "posted since Monday" would silently hide every one
        # of them rather than including the ones we discovered after Monday.
        statement = statement.where(
            func.coalesce(JobPosting.posted_at, JobPosting.created_at) >= filters.since
        )

    if filters.remote_only:
        statement = statement.where(JobPosting.work_arrangement == WorkArrangement.REMOTE)

    if filters.min_score is not None:
        statement = statement.where(
            JobPosting.id.in_(
                select(JobScore.posting_id).where(
                    JobScore.user_id == user_id,
                    JobScore.normalized >= filters.min_score,
                )
            )
        )

    return statement


async def _attach_scores(
    session: DbSession,
    user_id: uuid.UUID,
    postings: list[JobPosting],
) -> dict[uuid.UUID, ScoreRead]:
    """Fetch this user's scores for a whole page of postings in one query.

    One query for the page rather than one per row: the feed is the busiest screen in the
    app and an N+1 here is the difference between a page that paints instantly and one that
    visibly fills in.

    Args:
        session: The request's database session.
        user_id: The acting user.
        postings: The page's rows.

    Returns:
        Posting id to score, missing entries for postings this user has not scored.
    """
    if not postings:
        return {}
    rows = await session.execute(
        select(JobScore).where(
            JobScore.user_id == user_id,
            JobScore.posting_id.in_([posting.id for posting in postings]),
        )
    )
    return {score.posting_id: ScoreRead.model_validate(score) for score in rows.scalars().all()}


def _to_read(posting: JobPosting, scores: dict[uuid.UUID, ScoreRead]) -> PostingRead:
    """Project one posting into its API shape with the requesting user's score attached.

    Args:
        posting: The ORM row, with ``company`` eagerly loaded.
        scores: The page's score lookup.

    Returns:
        The populated read model.
    """
    item = PostingRead.model_validate(posting)
    item.score = scores.get(posting.id)
    return item


# ======================================================================================
# Endpoints
# ======================================================================================


@router.get(
    "",
    response_model=Page[PostingRead],
    summary="The discovery feed",
)
async def list_postings(
    user: CurrentUser,
    session: DbSession,
    filters: Annotated[PostingFilter, Depends()],
) -> Page[PostingRead]:
    """Return a filtered page of postings, newest first, each with this user's score.

    Ordered by publication date descending (falling back to discovery time), because
    freshness is what decides whether applying is still worth doing — a perfect match posted
    six weeks ago is usually already filled.

    :class:`~app.schemas.posting.PostingFilter` inherits
    :class:`~app.schemas.common.PaginationParams`, so ``limit`` and ``offset`` arrive with
    the filters and this handler declares one dependency rather than two — declaring both
    would publish each parameter twice in the OpenAPI document.

    Args:
        user: The acting user, whose scores are attached.
        session: The request's database session.
        filters: ``q``, ``provider``, ``status``, ``min_score``, ``company``, ``since``,
            ``remote_only``, plus ``limit``/``offset``. Every filter is optional; the
            unfiltered call is the default view.

    Returns:
        The page, with a true unpaginated total.
    """
    statement = _base_query(filters, user.id)

    total = await session.scalar(select(func.count()).select_from(statement.subquery()))

    rows = await session.execute(
        statement.options(selectinload(JobPosting.company))
        .order_by(
            func.coalesce(JobPosting.posted_at, JobPosting.created_at).desc(),
            JobPosting.id.desc(),
        )
        .limit(filters.limit)
        .offset(filters.offset)
    )
    postings = list(rows.scalars().unique().all())
    scores = await _attach_scores(session, user.id, postings)

    return paginate(
        [_to_read(posting, scores) for posting in postings],
        total=int(total or 0),
        params=filters,
    )


@router.post(
    "/discover",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run discovery now",
)
async def discover(
    payload: DiscoverRequest,
    user: CurrentUser,
) -> OkResponse:
    """Queue a manual discovery run across the requested boards.

    Naming providers fans out to one ``jobs.poll_provider`` task per board rather than one
    ``jobs.poll_all``, so a board that is rate limiting cannot delay the others and each
    one's failure is isolated to its own task. With no providers named, a single
    ``jobs.poll_all`` runs the user's enabled set — the common call, and the one the
    scheduler makes every thirty minutes.

    Empty ``keywords`` and ``locations`` mean "use the user's stored preferences" rather
    than "search for nothing", so the ordinary request body is ``{}``.

    Args:
        payload: Providers, search terms and limits for this run.
        user: The acting user.

    Returns:
        202 with one dispatch outcome per task. ``degraded`` is true when the broker is
        unreachable, in which case nothing has started and the run can be re-triggered.
    """
    query = payload.model_dump(mode="json")
    providers = [provider.value for provider in payload.providers]

    if providers:
        outcomes = [
            (await dispatch(TASK_JOBS_POLL_PROVIDER, str(user.id), provider, query=query)).as_dict()
            for provider in providers
        ]
    else:
        outcomes = [(await dispatch(TASK_JOBS_POLL_ALL, str(user.id), query=query)).as_dict()]

    degraded = any(outcome["degraded"] for outcome in outcomes)
    logger.info(
        "api.discovery_requested",
        user_id=str(user.id),
        providers=providers or ["<enabled>"],
        degraded=degraded,
    )
    return OkResponse(
        message=(
            "Discovery queued."
            if not degraded
            else "Discovery accepted, but the background worker is not running."
        ),
        data={"tasks": outcomes, "degraded": degraded},
    )


@router.get(
    "/{posting_id}",
    response_model=PostingRead,
    summary="One posting in full",
)
async def read_posting(
    posting_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
) -> PostingRead:
    """Return one posting with its description and this user's score.

    Postings are not user-owned — the same row is shared by every profile that discovered
    it — so there is no ownership check here. The *score* is user-scoped, and only the
    acting user's is attached.

    Args:
        posting_id: The posting to read.
        user: The acting user.
        session: The request's database session.

    Returns:
        The posting.

    Raises:
        HTTPException: ``404`` when no posting has that id.
    """
    posting = await session.scalar(
        select(JobPosting)
        .options(selectinload(JobPosting.company))
        .where(JobPosting.id == posting_id)
        .limit(1)
    )
    if posting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No posting exists with that id.",
        )
    scores = await _attach_scores(session, user.id, [posting])
    return _to_read(posting, scores)


@router.post(
    "/{posting_id}/apply",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue this posting for the apply pipeline",
    description="Enqueues only. The kill switch is still evaluated inside Pipeline.submit.",
)
async def apply_to_posting(
    posting_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
) -> OkResponse:
    """Queue score → prepare → submit for one posting.

    **This endpoint cannot cause a submission on its own.** It hands ``apply.run_one`` to
    the ``apply`` queue, and :meth:`app.services.pipeline.Pipeline.submit` then evaluates the
    full guard ladder — never-applied-before, ``auto_apply_enabled``, ``dry_run``, the daily
    cap, the score threshold, and whether the provider permits automation at all. A request
    is an intent, never a permission (golden rules #1 and #3).

    ``submission_allowed`` is reported back so the UI can say "this will be prepared but not
    sent" up front, rather than letting the user discover it from a run that quietly stops
    at ``ready``.

    Args:
        posting_id: The posting to apply to.
        user: The acting user.
        session: The request's database session, used to confirm the posting exists.
        settings: Runtime configuration, read only for the kill-switch report.

    Returns:
        202 with the dispatch outcome and the effective kill-switch state.

    Raises:
        HTTPException: ``404`` when no posting has that id — checked before dispatching, so
            a bad id fails here rather than on a queue nobody is watching.
    """
    posting = await session.scalar(select(JobPosting).where(JobPosting.id == posting_id).limit(1))
    if posting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No posting exists with that id.",
        )

    outcome = await dispatch(TASK_APPLY_RUN_ONE, str(posting_id), str(user.id))
    logger.info(
        "api.apply_requested",
        user_id=str(user.id),
        posting_id=str(posting_id),
        provider=posting.provider.value,
        submission_allowed=settings.is_submission_allowed,
        degraded=outcome.degraded,
    )
    return OkResponse(
        message=(
            "Queued for the apply pipeline."
            if settings.is_submission_allowed
            else "Queued. Submission is switched off, so this will stop before sending."
        ),
        data={
            **outcome.as_dict(),
            SUBMISSION_ALLOWED_KEY: settings.is_submission_allowed,
            "posting_id": str(posting_id),
        },
    )
