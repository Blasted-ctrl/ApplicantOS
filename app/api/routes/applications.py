"""Applications — the list, the detail view, the audit trail and the artifacts (§14).

An ``applications`` row is the object that guarantees ApplicantOS never applies to the same
job twice, and the object a user reaches for when they ask "what actually happened?". These
endpoints serve both jobs, and three rules govern them:

**Status changes go through the state machine, never through an assignment.**
``PATCH /applications/{id}`` accepts only the two fields a human legitimately owns —
``status`` and ``notes`` — and routes ``status`` through
:meth:`app.services.application_service.ApplicationService.transition`, which validates the
move, stamps ``submitted_at`` and appends an audit event. A rejected move raises
:class:`~app.services.application_service.InvalidTransition`, which :mod:`app.api.errors`
renders as 409. Nothing here can edit a submitted application back to ``draft`` and defeat
golden rule #1.

**Retrying enqueues; it does not submit.** ``POST /applications/{id}/retry`` hands
``apply.run_one`` to the ``apply`` queue and returns 202. An application the employer already
holds is refused with 409 before anything is queued.

**Artifacts are located by storage key.** ``uploaded_files`` deliberately carries no
``application_id`` column — the same catalogue describes user uploads, generated documents
and browser captures — so an application's artifacts are the rows whose ``storage_key`` sits
under ``users/{user}/applications/{application}/``, which is exactly the prefix
:meth:`app.services.pipeline.Pipeline._storage_key` writes. ``ArtifactRead.url`` points at
this server rather than at the storage backend, so a local install and an S3 install expose
the same shape.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import Select, func, or_, select

from app.api.deps import ApplicationServiceDep, CurrentUser, DbSession, SettingsDep
from app.api.events import (
    EVENT_APPLICATION_STATUS_CHANGED,
    bus,
)
from app.api.routes._support import (
    application_key_prefix,
    artifact_read,
    as_artifact_response,
    escape_like,
    like_pattern,
    load_uploaded_file,
    require_owner,
    resume_download_url,
)
from app.api.tasks import TASK_APPLY_RUN_ONE, dispatch
from app.models.application import Application
from app.models.company import Company
from app.models.enums import ApplicationStatus
from app.models.file import UploadedFile
from app.models.posting import JobPosting
from app.models.score import JobScore
from app.schemas.application import (
    ApplicationDetail,
    ApplicationFilter,
    ApplicationRead,
    ApplicationUpdate,
    ArtifactRead,
)
from app.schemas.common import OkResponse, Page, paginate
from app.schemas.scoring import ScoreRead

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/applications"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["applications"]

#: Detail returned when a retry is refused because the employer already has the application.
_ALREADY_SENT_DETAIL: Final[str] = (
    "This application has already been sent or has reached a terminal state, so it cannot "
    "be retried. An employer who holds an application cannot be made not to."
)

router = APIRouter()


# ======================================================================================
# Query construction
# ======================================================================================


def _base_query(filters: ApplicationFilter, user_id: uuid.UUID) -> Select[Any]:
    """Build the ``WHERE`` clause behind ``GET /applications``.

    Args:
        filters: The request's query parameters.
        user_id: The acting user; always the first predicate, so no filter combination can
            widen the query beyond one profile.

    Returns:
        The filtered statement, without ordering or paging.
    """
    statement: Select[Any] = select(Application).where(Application.user_id == user_id)

    if filters.q:
        pattern = like_pattern(filters.q)
        statement = statement.where(
            Application.posting_id.in_(
                select(JobPosting.id)
                .outerjoin(Company, JobPosting.company_id == Company.id)
                .where(
                    or_(
                        JobPosting.title.ilike(pattern, escape="\\"),
                        func.coalesce(Company.name, "").ilike(pattern, escape="\\"),
                    )
                )
            )
        )

    if filters.status is not None:
        statement = statement.where(Application.status == filters.status)

    if filters.provider is not None:
        statement = statement.where(
            Application.posting_id.in_(
                select(JobPosting.id).where(JobPosting.provider == filters.provider)
            )
        )

    if filters.company:
        statement = statement.where(
            Application.company_id.in_(
                select(Company.id).where(
                    Company.normalized_name == Company.normalize(filters.company)
                )
            )
        )

    if filters.session_id is not None:
        statement = statement.where(Application.session_id == filters.session_id)

    if filters.review_reason is not None:
        statement = statement.where(Application.review_reason == filters.review_reason)

    if filters.needs_review is not None:
        predicate = Application.status == ApplicationStatus.NEEDS_REVIEW
        statement = statement.where(predicate if filters.needs_review else ~predicate)

    if filters.since is not None:
        statement = statement.where(Application.created_at >= filters.since)

    if filters.until is not None:
        statement = statement.where(Application.created_at <= filters.until)

    return statement


async def _scores_for(
    session: DbSession,
    user_id: uuid.UUID,
    applications: list[Application],
) -> dict[uuid.UUID, ScoreRead]:
    """Fetch this user's scores for a whole page of applications in one query.

    Args:
        session: The request's database session.
        user_id: The acting user.
        applications: The page's rows.

    Returns:
        Posting id to score.
    """
    posting_ids = [application.posting_id for application in applications]
    if not posting_ids:
        return {}
    rows = await session.execute(
        select(JobScore).where(
            JobScore.user_id == user_id,
            JobScore.posting_id.in_(posting_ids),
        )
    )
    return {score.posting_id: ScoreRead.model_validate(score) for score in rows.scalars().all()}


def _to_read(application: Application, scores: dict[uuid.UUID, ScoreRead]) -> ApplicationRead:
    """Project one application into the list shape, with its posting's score attached.

    Args:
        application: The ORM row; ``posting`` and ``company`` are eagerly loaded on the model.
        scores: The page's score lookup.

    Returns:
        The populated read model.
    """
    item = ApplicationRead.model_validate(application)
    item.score = scores.get(application.posting_id)
    return item


async def _artifacts_of(
    session: DbSession,
    application: Application,
) -> list[UploadedFile]:
    """Return every live file stored under one application's key prefix.

    Args:
        session: The request's database session.
        application: The application whose artifacts are wanted.

    Returns:
        The catalogue rows, oldest first — which is capture order, and therefore the order
        the submission actually happened in.
    """
    prefix = application_key_prefix(application.user_id, application.id)
    rows = await session.execute(
        select(UploadedFile)
        .where(
            UploadedFile.storage_key.like(f"{escape_like(prefix)}%", escape="\\"),
            UploadedFile.deleted_at.is_(None),
        )
        .order_by(UploadedFile.created_at.asc(), UploadedFile.id.asc())
    )
    return list(rows.scalars().all())


async def _load_owned(
    service: ApplicationServiceDep,
    user: CurrentUser,
    application_id: uuid.UUID,
) -> Application:
    """Load an application and confirm it belongs to the acting user.

    Args:
        service: The application state machine.
        user: The acting user.
        application_id: The application being addressed.

    Returns:
        The row.

    Raises:
        LookupError: If no application has that id — mapped to 404.
        HTTPException: ``404`` if it belongs to another profile.
    """
    application = await service.get(application_id)
    require_owner(application.user_id, user.id, "application")
    return application


# ======================================================================================
# Endpoints
# ======================================================================================


@router.get(
    "",
    response_model=Page[ApplicationRead],
    summary="This user's applications",
)
async def list_applications(
    user: CurrentUser,
    session: DbSession,
    filters: Annotated[ApplicationFilter, Depends()],
) -> Page[ApplicationRead]:
    """Return a filtered page of applications, newest first.

    :class:`~app.schemas.application.ApplicationFilter` inherits the pagination parameters,
    so ``limit`` and ``offset`` arrive with the filters rather than as a second dependency.

    Args:
        user: The acting user.
        session: The request's database session.
        filters: ``q``, ``status``, ``provider``, ``company``, ``session_id``,
            ``review_reason``, ``needs_review``, ``since``, ``until``, plus paging.

    Returns:
        The page, each row carrying enough nested context — posting, company, score — to
        render without a second request.
    """
    statement = _base_query(filters, user.id)

    total = await session.scalar(select(func.count()).select_from(statement.subquery()))

    rows = await session.execute(
        statement.order_by(Application.created_at.desc(), Application.id.desc())
        .limit(filters.limit)
        .offset(filters.offset)
    )
    applications = list(rows.scalars().unique().all())
    scores = await _scores_for(session, user.id, applications)

    return paginate(
        [_to_read(application, scores) for application in applications],
        total=int(total or 0),
        params=filters,
    )


@router.get(
    "/{application_id}",
    response_model=ApplicationDetail,
    summary="One application, with its audit trail and artifacts",
)
async def read_application(
    application_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    service: ApplicationServiceDep,
) -> ApplicationDetail:
    """Return everything the application detail screen renders.

    The audit trail, the exact documents submitted, the answers given and the model's
    account of how it answered them. ``events`` is append-only and outlives log rotation, so
    this stays answerable months later — which is the point of keeping it in the database
    rather than only in the logs.

    Args:
        application_id: The application to read.
        user: The acting user.
        session: The request's database session.
        service: The application state machine.

    Returns:
        The full detail.

    Raises:
        LookupError: If no application has that id — mapped to 404.
    """
    application = await _load_owned(service, user, application_id)
    scores = await _scores_for(session, user.id, [application])

    detail = ApplicationDetail.model_validate(application)
    detail.score = scores.get(application.posting_id)

    artifacts = [
        artifact_read(record, application.id)
        for record in await _artifacts_of(session, application)
    ]
    detail.artifacts = artifacts
    detail.artifact_urls = [item.url for item in artifacts if item.url]

    if detail.resume_version is not None and detail.resume_version.file_id is not None:
        detail.resume_version.download_url = resume_download_url(detail.resume_version.id)

    return detail


@router.patch(
    "/{application_id}",
    response_model=ApplicationRead,
    responses={status.HTTP_409_CONFLICT: {"description": "The state machine refused the move."}},
    summary="Record an outcome or edit notes",
)
async def update_application(
    application_id: uuid.UUID,
    payload: ApplicationUpdate,
    user: CurrentUser,
    session: DbSession,
    service: ApplicationServiceDep,
) -> ApplicationRead:
    """Apply the only two changes a human owns: the outcome and the notes.

    ``status`` is writable because outcomes arrive out of band — only the user knows they
    were rejected, invited to interview, offered, or ghosted. It is *not* writable freely:
    the move goes through the state machine, which refuses anything that would let a
    submitted application be edited back into a submittable one.

    Everything else on the row (``submitted_at``, ``attempt_count``, ``answers``, the
    confirmation fields) is written by the pipeline and has no HTTP write path at all.

    A successful status change is broadcast as ``application.status_changed`` carrying the
    same :class:`~app.schemas.application.ApplicationRead` this returns, so other windows
    update from the event without refetching.

    Args:
        application_id: The application to update.
        payload: ``status`` and/or ``notes``.
        user: The acting user.
        session: The request's database session.
        service: The application state machine.

    Returns:
        The application after the write.

    Raises:
        LookupError: If no application has that id — mapped to 404.
        InvalidTransition: If the requested status cannot follow the present one — mapped
            to 409.
    """
    application = await _load_owned(service, user, application_id)
    changes = payload.model_dump(exclude_unset=True)

    # The status change goes first. It is the only part of this request that can be
    # refused, and running it first means a rejected transition leaves *nothing* written —
    # a PATCH that persisted the notes and then returned 409 would be half-applied, and the
    # client would have no way to tell which half.
    status_changed = False
    if payload.status is not None and payload.status is not application.status:
        application = await service.transition(
            application,
            payload.status,
            message="Outcome recorded by the user.",
        )
        status_changed = True

    if "notes" in changes:
        application.notes = changes["notes"]
        await session.flush()
        await session.commit()

    scores = await _scores_for(session, user.id, [application])
    item = _to_read(application, scores)

    if status_changed:
        bus.publish_model(EVENT_APPLICATION_STATUS_CHANGED, item)
    logger.info(
        "api.application_updated",
        user_id=str(user.id),
        application_id=str(application_id),
        fields=sorted(changes),
        status=application.status.value,
    )
    return item


@router.post(
    "/{application_id}/retry",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_409_CONFLICT: {"description": "Already sent or terminal."}},
    summary="Queue another attempt",
)
async def retry_application(
    application_id: uuid.UUID,
    user: CurrentUser,
    service: ApplicationServiceDep,
    settings: SettingsDep,
) -> OkResponse:
    """Queue score → prepare → submit again for this application's posting.

    ``apply.run_one`` rather than ``apply.submit``, because a failure can have happened at
    any stage: an application that never got past preparation needs its documents generated,
    and ``run_one`` is idempotent for the stages that already succeeded — ``prepare`` is a
    no-op past ``ready`` and ``submit`` refuses an application the employer already holds.

    The refusal below is the in-process half of golden rule #1 and is checked *before*
    dispatch, so a double-click cannot put a duplicate submission on the queue.

    Args:
        application_id: The application to retry.
        user: The acting user.
        service: The application state machine.
        settings: Runtime configuration, read only for the kill-switch report.

    Returns:
        202 with the dispatch outcome. ``degraded`` is true when the broker is unreachable.

    Raises:
        LookupError: If no application has that id — mapped to 404.
        HTTPException: ``409`` when the application has already been sent or has reached a
            terminal state.
    """
    application = await _load_owned(service, user, application_id)
    if not application.can_submit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_ALREADY_SENT_DETAIL,
        )

    # `requeued`: the user pressed Retry on this application specifically, which outranks
    # the stop of whatever run originally created it.
    outcome = await dispatch(
        TASK_APPLY_RUN_ONE, str(application.posting_id), str(user.id), requeued=True
    )
    logger.info(
        "api.application_retry_requested",
        user_id=str(user.id),
        application_id=str(application_id),
        status=application.status.value,
        degraded=outcome.degraded,
    )
    return OkResponse(
        message="Queued for another attempt.",
        data={
            **outcome.as_dict(),
            "application_id": str(application_id),
            "submission_allowed": settings.is_submission_allowed,
        },
    )


@router.get(
    "/{application_id}/artifacts",
    response_model=Page[ArtifactRead],
    summary="Screenshots and rendered documents for one application",
)
async def list_artifacts(
    application_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    service: ApplicationServiceDep,
) -> Page[ArtifactRead]:
    """Return the files produced or consumed by one application.

    Unpaginated in substance — an application has a handful of artifacts, not a feed of
    them — but still wrapped in a :class:`~app.schemas.common.Page`, because every list
    endpoint in this API returns one and a client should not have to remember which shape
    each returns.

    Args:
        application_id: The application whose artifacts are wanted.
        user: The acting user.
        session: The request's database session.
        service: The application state machine.

    Returns:
        The artifacts, in capture order, each with a working download link.

    Raises:
        LookupError: If no application has that id — mapped to 404.
    """
    application = await _load_owned(service, user, application_id)
    records = await _artifacts_of(session, application)
    items = [artifact_read(record, application.id) for record in records]
    return Page.of(items, total=len(items), limit=len(items) or 1, offset=0)


@router.get(
    "/{application_id}/artifacts/{file_id}",
    response_class=FileResponse,
    responses={
        status.HTTP_200_OK: {"content": {"application/octet-stream": {}}},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown id, or the bytes were reclaimed."},
    },
    summary="Download one artifact",
)
async def download_artifact(
    application_id: uuid.UUID,
    file_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    service: ApplicationServiceDep,
    settings: SettingsDep,
) -> FileResponse:
    """Stream one artifact's bytes.

    The concrete resolution of :attr:`~app.schemas.application.ArtifactRead.url`. The file is
    confirmed to sit under *this* application's key prefix before anything is opened, so a
    valid file id belonging to a different application — or a different profile — is a 404
    rather than a download.

    Args:
        application_id: The owning application.
        file_id: The artifact to stream.
        user: The acting user.
        session: The request's database session.
        service: The application state machine.
        settings: Runtime configuration, supplying the blob root.

    Returns:
        The file.

    Raises:
        HTTPException: ``404`` when the artifact is unknown, belongs elsewhere, or its bytes
            have been reclaimed; ``409`` when it lives in a backend this process cannot read.
    """
    application = await _load_owned(service, user, application_id)
    record = await load_uploaded_file(session, file_id)

    prefix = application_key_prefix(application.user_id, application.id)
    if not record.storage_key.startswith(prefix):
        logger.info(
            "api.artifact_scope_refused",
            user_id=str(user.id),
            application_id=str(application_id),
            file_id=str(file_id),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No artifact exists with that id for this application.",
        )

    return as_artifact_response(record, settings)
