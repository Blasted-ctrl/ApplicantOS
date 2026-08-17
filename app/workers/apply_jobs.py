"""Application tasks — ``apply.*`` (``docs/CONTRACTS.md`` §15).

The three tasks map one-to-one onto the pipeline's three stages, and the split exists because
the stages have completely different costs and completely different failure modes:

``apply.prepare`` (queue ``ai``)
    Knowledge retrieval, tailoring, cover letter, render. Expensive in tokens, cheap in
    consequences — nothing has left the machine. Idempotent past ``READY``.

``apply.submit`` (queue ``apply``)
    The only task in the system that can send something to an employer. Serialised onto its
    own queue so a backlog of document generation cannot delay a submission, and vice versa.

``apply.run_one`` (queue ``apply``)
    Score, prepare and submit one posting in a single unit of work. The path
    ``POST /postings/{id}/apply`` and ``POST /applications/{id}/retry`` take, where a user is
    watching and wants one answer rather than three.

**``apply.submit`` is the task that closes the review loop.**
:meth:`~app.services.review_service.ReviewService.resolve` deliberately leaves an application
at ``READY`` rather than submitting it, so that every real submission stays behind
:meth:`~app.services.pipeline.Pipeline.submit` and therefore behind the kill switch;
``POST /reviews/{id}/resolve`` then enqueues ``apply.submit``. Without this module that
enqueue lands nowhere, and resolving a review silently does nothing.

Not one guard is re-implemented here. The daily cap, the score floor, the provider's ToS
posture, the never-apply-twice check and the two-switch kill switch all live in
``Pipeline.submit``, in that order, and are re-read on every attempt. A task that re-checked
them would be a second copy of the safety envelope, and the two copies would diverge. What
this module adds is the outcome mapping: a terminal verdict is never retried
(:mod:`app.workers.retry`), and every outcome is published and counted.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from sqlalchemy.exc import SQLAlchemyError

from app.api.events import (
    EVENT_APPLICATION_CREATED,
    EVENT_APPLICATION_NEEDS_REVIEW,
    EVENT_APPLICATION_STATUS_CHANGED,
    EVENT_APPLICATION_SUBMITTED,
    bus,
)
from app.api.tasks import TASK_APPLY_PREPARE, TASK_APPLY_RUN_ONE, TASK_APPLY_SUBMIT
from app.config.settings import get_settings
from app.database.session import session_scope
from app.workers import run_async, task_span
from app.workers.celery_app import (
    APPLY_HARD_TIME_LIMIT_SECONDS,
    APPLY_SOFT_TIME_LIMIT_SECONDS,
    celery_app,
    enqueue,
)
from app.workers.counters import record_session
from app.workers.retry import is_terminal_outcome, retryable

__all__ = ["prepare", "run_one", "submit"]

logger = structlog.get_logger(__name__)

#: Session counters an application run reports.
_RESUMES_COUNTER: Final[str] = "resumes_generated"
_STARTED_COUNTER: Final[str] = "applications_started"
_COMPLETED_COUNTER: Final[str] = "applications_completed"
_SKIPPED_COUNTER: Final[str] = "applications_skipped"
_REVIEW_COUNTER: Final[str] = "manual_review"
_FAILURE_COUNTER: Final[str] = "failures"


# ======================================================================================
# Shared helpers
# ======================================================================================


async def _publish_application(session: Any, event: str, application: Any) -> None:
    """Publish an application as the schema ``GET /applications/{id}`` returns.

    Serialisation runs inside :meth:`~sqlalchemy.ext.asyncio.AsyncSession.run_sync` rather
    than being called directly, and that is load-bearing. ``ApplicationRead`` nests
    ``posting`` and ``company``. Those relationships are ``lazy="selectin"``, which populates
    them for rows that arrive from a *query* — but an application this task just **inserted**
    has never been through one, so its relationships are unloaded and pydantic's attribute
    read becomes a lazy load. On an async session a lazy load outside a greenlet context
    raises ``MissingGreenlet``, which would turn "we published an event" into "the whole
    prepare task failed after the documents were already generated". ``run_sync`` provides
    exactly that context, so the load succeeds instead.

    Args:
        session: The open async session owning *application*.
        event: One of the ``application.*`` names in ``docs/CONTRACTS.md`` §14.
        application: The ORM row.
    """
    from pydantic import ValidationError

    from app.schemas.application import ApplicationRead

    try:
        item = await session.run_sync(lambda _sync: ApplicationRead.model_validate(application))
    except (ValidationError, SQLAlchemyError) as exc:
        # ``event_name``, not ``event``: structlog's bound-logger methods take the event
        # name positionally, so an ``event=`` keyword collides with it and raises.
        logger.warning(
            "apply.publish_serialize_failed",
            event_name=event,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    bus.publish_model(event, item)


def _session_deltas(result: dict[str, Any]) -> dict[str, int]:
    """Translate a pipeline verdict into run-session counter increments.

    ``blocked`` is read through the application's resulting *status*, not through the verdict
    alone, because one verdict covers two outcomes that differ in exactly the way this counter
    measures. The daily cap blocks an application and leaves it ``ready`` for tomorrow — no
    human is involved. The kill switch blocks it and calls ``mark_needs_review`` — the
    application is now in the queue, which is what ``manual_review`` is documented to count
    ("applications routed to a human rather than guessed at"). Counting neither made a run
    that stopped every application at the kill switch — the shipped default — report
    ``0 need review`` beside a review queue it had just filled.

    The status is only consulted for ``blocked``. A ``skipped`` verdict also carries whatever
    status the application already had, so an application left over in ``needs_review`` from
    an earlier run would otherwise be re-counted by every run that passed over it.

    Args:
        result: A serialised :class:`~app.services.pipeline.PipelineResult`.

    Returns:
        Counter increments; empty for verdicts that change nothing (a skip costs the run
        nothing and must not inflate its failure count).
    """
    from app.models.enums import ApplicationStatus
    from app.services.pipeline import (
        VERDICT_BLOCKED,
        VERDICT_FAILED,
        VERDICT_NEEDS_REVIEW,
        VERDICT_SUBMITTED,
    )

    verdict = result.get("verdict")
    if verdict == VERDICT_SUBMITTED:
        return {_COMPLETED_COUNTER: 1}
    if verdict == VERDICT_NEEDS_REVIEW:
        return {_REVIEW_COUNTER: 1}
    if verdict == VERDICT_FAILED:
        return {_FAILURE_COUNTER: 1}
    if verdict == VERDICT_BLOCKED:
        escalated = result.get("status") == ApplicationStatus.NEEDS_REVIEW.value
        # A block that did *not* escalate is a cap or a stop: the application was passed
        # over and stays ready. That is precisely what "skipped" counts, and leaving it
        # uncounted is what made a run report fewer outcomes than it had applications.
        return {_REVIEW_COUNTER: 1} if escalated else {_SKIPPED_COUNTER: 1}
    return {}


async def _publish_result(session: Any, result: dict[str, Any], application: Any) -> None:
    """Publish the §14 event matching a pipeline verdict.

    Args:
        session: The open async session owning *application*.
        result: A serialised :class:`~app.services.pipeline.PipelineResult`.
        application: The ORM row the result refers to, still attached to its session.
    """
    from app.services.pipeline import VERDICT_NEEDS_REVIEW, VERDICT_SUBMITTED

    verdict = result.get("verdict")
    if verdict == VERDICT_SUBMITTED:
        await _publish_application(session, EVENT_APPLICATION_SUBMITTED, application)
    elif verdict == VERDICT_NEEDS_REVIEW:
        await _publish_application(session, EVENT_APPLICATION_NEEDS_REVIEW, application)
    await _publish_application(session, EVENT_APPLICATION_STATUS_CHANGED, application)


# ======================================================================================
# apply.prepare
# ======================================================================================


@celery_app.task(name=TASK_APPLY_PREPARE, bind=False)
@retryable()
def prepare(
    posting_id: str,
    user_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Generate one application's documents and leave it ``ready`` to submit.

    Idempotent by construction: :meth:`~app.services.pipeline.Pipeline.prepare` returns an
    application that has already reached ``ready`` (or beyond) untouched, with no model call
    and no new :class:`~app.models.resume.ResumeVersion` row. That is what makes it safe for
    an ``acks_late`` redelivery to re-run this task.

    On success the application is handed to ``apply.submit`` when ``auto_apply_enabled`` is
    on. ``dry_run`` is deliberately *not* consulted here: ``Pipeline.submit`` refuses at its
    kill-switch guard before opening a browser, and letting the task run means a dry-run
    install still shows the user exactly which applications would have been sent.

    Args:
        posting_id: The posting to apply to.
        user_id: The applicant.
        session_id: The run session to credit.

    Returns:
        ``{"application_id", "status", "review_reason", "queued_for_submit"}``.

    Raises:
        LookupError: If the posting or the user does not exist.
    """
    with task_span(
        TASK_APPLY_PREPARE, posting_id=posting_id, user_id=user_id, session_id=session_id
    ) as log:

        async def _run() -> dict[str, Any]:
            """Prepare inside one unit of work, then publish."""
            from app.models.enums import ApplicationStatus
            from app.services.pipeline import Pipeline
            from app.services.session_service import SessionService

            settings = get_settings()
            async with session_scope() as session:
                # A stop asked for while this task sat on the queue. Preparing anyway would
                # spend a model call and a render on an application that rung 2 of the
                # submit ladder is about to refuse.
                halted = await SessionService(session).halt_reason(session_id)
                if halted is not None:
                    return {
                        "application_id": None,
                        "posting_id": str(posting_id),
                        "user_id": str(user_id),
                        "status": None,
                        "review_reason": None,
                        "ready": False,
                        "halted": halted.value,
                    }

                pipeline = Pipeline(session, settings)
                application = await pipeline.prepare(posting_id, user_id, session_id=session_id)
                payload: dict[str, Any] = {
                    "application_id": str(application.id),
                    "posting_id": str(posting_id),
                    "user_id": str(user_id),
                    "status": application.status.value,
                    "review_reason": (
                        application.review_reason.value
                        if application.review_reason is not None
                        else None
                    ),
                    "ready": application.status is ApplicationStatus.READY,
                    "halted": None,
                }
                if application.session_id is not None:
                    payload["session_id"] = str(application.session_id)
                await _publish_application(session, EVENT_APPLICATION_CREATED, application)
                await _publish_application(session, EVENT_APPLICATION_STATUS_CHANGED, application)
            return payload

        outcome = run_async(_run())

        if outcome["halted"] is not None:
            outcome["queued_for_submit"] = False
            log.info("apply.prepare_halted", session_id=session_id, reason=outcome["halted"])
            return outcome

        queued: str | None = None
        if outcome["ready"] and get_settings().auto_apply_enabled:
            queued = enqueue(TASK_APPLY_SUBMIT, outcome["application_id"])
        outcome["queued_for_submit"] = queued is not None

        run_async(
            record_session(
                session_id,
                **{
                    _RESUMES_COUNTER: 1 if outcome["ready"] else 0,
                    _STARTED_COUNTER: 1 if outcome["ready"] else 0,
                },
            )
        )
        log.info(
            "apply.prepared",
            application_id=outcome["application_id"],
            status=outcome["status"],
            queued_for_submit=outcome["queued_for_submit"],
        )
        return outcome


# ======================================================================================
# apply.submit
# ======================================================================================


@celery_app.task(
    name=TASK_APPLY_SUBMIT,
    bind=False,
    soft_time_limit=APPLY_SOFT_TIME_LIMIT_SECONDS,
    time_limit=APPLY_HARD_TIME_LIMIT_SECONDS,
)
@retryable()
def submit(application_id: str, *, requeued: bool = False) -> dict[str, Any]:
    """Attempt one real submission, behind the pipeline's full guard ladder.

    The task ``POST /reviews/{id}/resolve`` enqueues: a resolved review leaves the
    application at ``READY``, and this is what picks it up. It is also the second half of
    ``apply.prepare``.

    Every guard is delegated, in the order ``Pipeline.submit`` defines them — never applied
    twice, daily cap, score floor, provider ToS posture, kill switch — so this task cannot
    weaken any of them and cannot drift from the path the API takes.

    The result is **not** retried when its verdict is terminal. ``needs_review`` means a
    person is looking at that form right now; ``blocked`` means the daily cap or the kill
    switch just said no. Re-running either would be actively harmful, which is why
    :func:`app.workers.retry.is_terminal_outcome` is consulted explicitly rather than relied
    on to raise nothing.

    Args:
        application_id: The application to submit.
        requeued: ``True`` when a person asked for this application specifically — which is
            what ``POST /reviews/{id}/resolve`` is. It lifts the owning run's stop, and only
            that: an answered review is always answered *after* its run has ended, so
            without this the resolved application would leave the queue and never be sent.

    Returns:
        The :class:`~app.services.pipeline.PipelineResult` as a mapping. ``submitted`` is
        ``True`` on exactly one path.

    Raises:
        LookupError: If no such application exists.
    """
    with task_span(TASK_APPLY_SUBMIT, application_id=application_id, requeued=requeued) as log:

        async def _run() -> tuple[dict[str, Any], str | None]:
            """Submit inside one unit of work, publish, and report the owning run."""
            from app.models.application import Application
            from app.services.pipeline import Pipeline

            settings = get_settings()
            owning_session: str | None = None
            async with session_scope() as session:
                pipeline = Pipeline(session, settings)
                result = await pipeline.submit(application_id, requeued=requeued)
                payload = result.as_dict()

                if result.application_id is not None:
                    application = await session.get(Application, result.application_id)
                    if application is not None:
                        if application.session_id is not None:
                            owning_session = str(application.session_id)
                        await _publish_result(session, payload, application)
            return payload, owning_session

        outcome, owning_session = run_async(_run())
        run_async(record_session(owning_session, **_session_deltas(outcome)))

        if is_terminal_outcome(outcome):
            log.info(
                "apply.submit_terminal",
                verdict=outcome.get("verdict"),
                review_reason=outcome.get("review_reason"),
                retried=False,
            )
        else:
            log.info(
                "apply.submitted",
                verdict=outcome.get("verdict"),
                submitted=outcome.get("submitted"),
            )
        return outcome


# ======================================================================================
# apply.run_one
# ======================================================================================


@celery_app.task(
    name=TASK_APPLY_RUN_ONE,
    bind=False,
    soft_time_limit=APPLY_SOFT_TIME_LIMIT_SECONDS,
    time_limit=APPLY_HARD_TIME_LIMIT_SECONDS,
)
@retryable()
def run_one(
    posting_id: str,
    user_id: str,
    *,
    session_id: str | None = None,
    requeued: bool = False,
) -> dict[str, Any]:
    """Score, prepare and submit one posting, stopping at the first refusal.

    The whole journey as one unit of work, short-circuiting so that a posting the scorer
    rejects never reaches the resume engine and an application that failed to prepare never
    reaches a browser.

    Args:
        posting_id: The posting to apply to.
        user_id: The applicant.
        session_id: The run session to credit.
        requeued: ``True`` when a person asked for this posting specifically — which is what
            ``POST /applications/{id}/retry`` is. It lifts the owning run's stop and nothing
            else; every guard protecting the employer still applies.

    Returns:
        The verdict of whichever stage stopped, as a mapping.

    Raises:
        LookupError: If the posting or the user does not exist.
    """
    with task_span(
        TASK_APPLY_RUN_ONE, posting_id=posting_id, user_id=user_id, session_id=session_id
    ) as log:

        async def _run() -> dict[str, Any]:
            """Run the full pipeline for one posting inside one unit of work."""
            from app.models.application import Application
            from app.services.pipeline import Pipeline

            settings = get_settings()
            async with session_scope() as session:
                pipeline = Pipeline(session, settings)
                result = await pipeline.run_one(
                    posting_id, user_id, session_id=session_id, requeued=requeued
                )
                payload = result.as_dict()
                if result.application_id is not None:
                    application = await session.get(Application, result.application_id)
                    if application is not None:
                        await _publish_result(session, payload, application)
            return payload

        outcome = run_async(_run())
        run_async(record_session(session_id, **_session_deltas(outcome)))
        log.info(
            "apply.run_one_finished",
            verdict=outcome.get("verdict"),
            stage=outcome.get("stage"),
            submitted=outcome.get("submitted"),
            application_id=outcome.get("application_id"),
        )
        return outcome
