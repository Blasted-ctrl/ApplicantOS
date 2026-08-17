"""The human review queue (``docs/CONTRACTS.md`` §14).

Golden rule #2 — never guess — is why this group exists. Every
:class:`~app.models.enums.ReviewReason` marks a point where the automation could have
produced *an* answer and stopping was the better outcome: an unanswerable required field, an
answer below ``settings.min_answer_confidence``, an essay overflow, a captcha, an MFA prompt,
a login wall. Manual review is therefore the **failure mode, not a wrong answer**, and the
queue is work rather than an error log.

``GET /reviews`` returns oldest first. That ordering is not cosmetic: a review that has been
waiting three days is more urgent than one raised a minute ago, and postings close while
their applications sit in a queue sorted newest-first.

**Resolving does not submit.** :meth:`~app.services.review_service.ReviewService.resolve`
merges the human's answers into ``Application.answers`` — merged, never substituted, so
answering the one blocking question does not erase the forty the automation already
resolved — and leaves the application at ``ready``. This module then enqueues
``apply.submit``, which keeps every real submission behind
:meth:`app.services.pipeline.Pipeline.submit` and therefore behind the kill switch. The
answers are also recorded as a :class:`~app.models.knowledge.MemoryEntry` correction, so the
same question answers itself next time.

**Dismissing is not failing.** A dismissed application becomes ``abandoned``: nothing broke,
a person made a decision, and conflating the two would corrupt every reliability number the
dashboard shows.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Body, Depends, status

from app.api.deps import ApplicationServiceDep, CurrentUser, ReviewServiceDep
from app.api.events import (
    EVENT_APPLICATION_STATUS_CHANGED,
    bus,
)
from app.api.routes._support import require_owner
from app.api.tasks import TASK_APPLY_SUBMIT, dispatch
from app.models.application import Application
from app.schemas.application import (
    ApplicationFilter,
    ApplicationRead,
    ReviewField,
    ReviewItem,
    ReviewResolve,
)
from app.schemas.common import OkResponse, Page, paginate
from app.services.review_service import REVIEW_FIELDS_PAYLOAD_KEY

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/reviews"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["reviews"]

router = APIRouter()


def _review_fields(payload: Mapping[str, Any] | None) -> list[ReviewField]:
    """Parse the unanswered fields the pipeline recorded when it stopped.

    Tolerant on purpose. The payload is JSON written by a different process, possibly a
    different build; a malformed entry must not make the review item unreadable, because
    that would strand the application it belongs to with no way for a human to unblock it.
    Unparseable entries are dropped and the rest of the item still renders.

    Args:
        payload: ``Application.review_payload`` as the pipeline wrote it.

    Returns:
        The fields the automation refused to guess at.
    """
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get(REVIEW_FIELDS_PAYLOAD_KEY)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []

    fields: list[ReviewField] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        try:
            fields.append(ReviewField.model_validate(dict(entry)))
        except ValueError as exc:
            logger.debug("api.review_field_unparseable", error=str(exc))
    return fields


def _to_item(application: Application) -> ReviewItem:
    """Project one parked application into a review-queue entry.

    Args:
        application: The application awaiting a human, with ``posting`` and ``company``
            eagerly loaded.

    Returns:
        Everything a reviewer needs to decide on one screen.
    """
    payload = dict(application.review_payload or {})
    return ReviewItem(
        application=ApplicationRead.model_validate(application),
        reason=application.review_reason,
        unanswered_fields=_review_fields(payload),
        payload=payload,
    )


@router.get(
    "",
    response_model=Page[ReviewItem],
    summary="Applications waiting on a human, oldest first",
)
async def list_reviews(
    user: CurrentUser,
    service: ReviewServiceDep,
    filters: Annotated[ApplicationFilter, Depends()],
) -> Page[ReviewItem]:
    """Return the open review queue.

    The filters are :class:`~app.schemas.application.ApplicationFilter`'s, minus the ones
    that make no sense here: ``status`` and ``needs_review`` are implied, because every item
    in this queue is by definition parked. Unrecognised filter keys are ignored rather than
    rejected — the desktop app sends its whole filter state, and a key this build does not
    know about must not blank the queue.

    Args:
        user: The acting user.
        service: The review queue service.
        filters: ``q``, ``review_reason``, ``provider``, ``company``, ``session_id``,
            ``since`` and ``until``, plus ``limit``/``offset`` — which the filter model
            inherits, so this handler declares one dependency rather than two.

    Returns:
        One page of review items, with a true unpaginated total so the client can render
        "3 of 17 waiting".
    """
    supplied: dict[str, Any] = filters.model_dump(exclude_none=True)

    applications = await service.list_pending(user.id, supplied)
    total = await service.count_pending(user.id, supplied)

    return paginate([_to_item(item) for item in applications], total=total, params=filters)


@router.post(
    "/{application_id}/resolve",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Answer the open questions and re-queue",
)
async def resolve_review(
    application_id: uuid.UUID,
    payload: ReviewResolve,
    user: CurrentUser,
    service: ReviewServiceDep,
    applications: ApplicationServiceDep,
) -> OkResponse:
    """Apply a human's answers and hand the application back to the apply queue.

    The application is left at ``ready`` and ``apply.submit`` is enqueued. Nothing here
    submits: routing through the pipeline is what keeps the kill switch, the daily cap and
    the never-apply-twice guard in force on a path a human initiated exactly as on one a
    scheduler did.

    Answers are keyed by the ``selector`` of each
    :class:`~app.schemas.application.ReviewField`, and are merged into any answers the
    automation had already resolved.

    Args:
        application_id: The parked application.
        payload: Field identifier to the value the reviewer supplied.
        user: The acting user.
        service: The review queue service.
        applications: The application state machine, used for the ownership check.

    Returns:
        202 with the dispatch outcome and the application's new status.

    Raises:
        LookupError: If no application has that id — mapped to 404.
        ValueError: If the application is not awaiting review, or ``answers`` is empty —
            mapped to 400. Resolving nothing would clear the reason and re-queue an
            application that is still unanswerable.
    """
    existing = await applications.get(application_id)
    require_owner(existing.user_id, user.id, "application")

    application = await service.resolve(application_id, payload.answers)
    # `requeued`: a person just answered the question this application was parked on. Its
    # run ended long before that — a run never waits on a human — so without this the submit
    # ladder's run-stop rung would refuse it and the answered application would leave the
    # queue without ever being sent.
    outcome = await dispatch(TASK_APPLY_SUBMIT, str(application.id), requeued=True)

    item = ApplicationRead.model_validate(application)
    bus.publish_model(EVENT_APPLICATION_STATUS_CHANGED, item)

    logger.info(
        "api.review_resolved",
        user_id=str(user.id),
        application_id=str(application_id),
        answers=len(payload.answers),
        degraded=outcome.degraded,
    )
    return OkResponse(
        message="Answers recorded. The application is queued for submission.",
        data={
            **outcome.as_dict(),
            "application_id": str(application.id),
            "status": application.status.value,
        },
    )


@router.post(
    "/{application_id}/dismiss",
    response_model=ApplicationRead,
    responses={status.HTTP_409_CONFLICT: {"description": "Already sent."}},
    summary="Abandon an application the user has decided against",
)
async def dismiss_review(
    application_id: uuid.UUID,
    user: CurrentUser,
    service: ReviewServiceDep,
    applications: ApplicationServiceDep,
    note: Annotated[
        str | None,
        Body(embed=True, description="Why, in the user's own words. Appended to notes."),
    ] = None,
) -> ApplicationRead:
    """Mark an application ``abandoned`` and take it off the queue.

    ``abandoned``, not ``failed``. The distinction is the difference between "the automation
    could not do this" and "the person did not want it", and collapsing the two would make
    every reliability figure on the dashboard wrong in the direction that flatters the
    system.

    Args:
        application_id: The application to dismiss.
        user: The acting user.
        service: The review queue service.
        applications: The application state machine, used for the ownership check.
        note: Optional reason, appended to ``Application.notes`` and recorded on the event.

    Returns:
        The application, now abandoned.

    Raises:
        LookupError: If no application has that id — mapped to 404.
        InvalidTransition: If the application has already been sent — mapped to 409. An
            employer who holds an application cannot be made not to.
    """
    existing = await applications.get(application_id)
    require_owner(existing.user_id, user.id, "application")

    application = await service.dismiss(application_id, note)
    item = ApplicationRead.model_validate(application)
    bus.publish_model(EVENT_APPLICATION_STATUS_CHANGED, item)

    logger.info(
        "api.review_dismissed",
        user_id=str(user.id),
        application_id=str(application_id),
        has_note=bool(note and note.strip()),
    )
    return item
