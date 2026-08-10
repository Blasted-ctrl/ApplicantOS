"""The onboarding wizard (``docs/CONTRACTS.md`` §14).

Onboarding is *server-driven*: ``GET /onboarding/steps`` returns
:class:`~app.schemas.onboarding.OnboardingStep` descriptors carrying their own field
definitions, and the desktop app renders whatever it is given. Adding a question, rewording
one, or making one conditional is a backend change, not a coordinated release of two
codebases.

That design decides the shape of ``POST /onboarding/steps/{step}``: the body's schema depends
on the path parameter. So the body is declared as an object and validated against
:data:`~app.schemas.onboarding.ONBOARDING_PAYLOAD_MODELS` for the named step, with the
failure re-raised as FastAPI's own :class:`~fastapi.exceptions.RequestValidationError` — the
client gets the identical 422 shape it would have got from a statically-typed body, and the
service still receives a validated model rather than a raw dictionary.

``step`` is typed :data:`~app.schemas.onboarding.OnboardingStepKey`, so an unknown step is
rejected by FastAPI before any handler runs.

``POST /onboarding/complete`` marks the user onboarded and returns the knowledge sources it
registered, then queues the first indexing pass. Indexing is queued rather than awaited
because a first index crawls GitHub, a portfolio site and a resume — minutes of work that
must not hold a request open.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from fastapi import APIRouter, Body, HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from app.api.deps import CurrentUser, OnboardingServiceDep, OptionalUser
from app.api.tasks import TASK_KNOWLEDGE_INDEX_ALL, dispatch
from app.schemas.common import OkResponse
from app.schemas.onboarding import (
    ONBOARDING_PAYLOAD_MODELS,
    OnboardingStatus,
    OnboardingStep,
    OnboardingStepKey,
)

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/onboarding"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["onboarding"]

router = APIRouter()


def _validate_step_payload(step: str, payload: dict[str, Any]) -> BaseModel:
    """Validate a step body against the model that step declares.

    Args:
        step: The step key, already constrained to
            :data:`~app.schemas.onboarding.ONBOARDING_STEP_KEYS` by the path parameter's type.
        payload: The raw request body.

    Returns:
        The validated payload model.

    Raises:
        RequestValidationError: If the body does not match the step's model. Re-raised as
            FastAPI's own error so the response is the same 422 shape as any other
            validation failure, rather than a bespoke one for this endpoint.
    """
    model = ONBOARDING_PAYLOAD_MODELS[step]
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


@router.get(
    "/status",
    response_model=OnboardingStatus,
    summary="Where the user is in the wizard",
)
async def onboarding_status(
    user: OptionalUser,
    service: OnboardingServiceDep,
) -> OnboardingStatus:
    """Return completion state, the step to resume at, and per-step progress.

    Answers on a first run too. With no account the honest reply is "not started" — the
    state the client redirects into the wizard on — not a 404 the client has to interpret.

    Args:
        user: The acting user, or ``None`` when the install has no account yet.
        service: The onboarding service.

    Returns:
        The wizard's state for this user, or a zero-progress state on a first run.
    """
    return await service.status(None if user is None else user.id)


@router.get(
    "/steps",
    response_model=list[OnboardingStep],
    summary="The wizard definition",
    description="Server-driven form descriptors; the client renders whatever it is given.",
)
async def onboarding_steps(
    user: OptionalUser,
    service: OnboardingServiceDep,
) -> list[OnboardingStep]:
    """Return every step with its fields and completion state.

    Not a :class:`~app.schemas.common.Page`: the wizard is a fixed, short, ordered
    definition, and paginating it would let a client render half a form.

    **Answers without an account, and must.** This endpoint is the wizard's definition, and
    the wizard is what creates the account: 404ing here deadlocked first run outright — the
    client rendered zero steps, so the form that would have created the user could never be
    filled in, and the app fell back to a dashboard on which every screen 404s.

    Args:
        user: The acting user, or ``None`` when the install has no account yet.
        service: The onboarding service.

    Returns:
        The steps in presentation order, all incomplete on a first run.
    """
    return await service.steps(None if user is None else user.id)


@router.post(
    "/steps/{step}",
    response_model=OnboardingStatus,
    summary="Submit one step",
    description="The body's schema is the payload model that step declares.",
)
async def submit_step(
    step: OnboardingStepKey,
    user: OptionalUser,
    service: OnboardingServiceDep,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> OnboardingStatus:
    """Apply one step's answers and return the updated wizard state.

    **This is the endpoint that creates the account.** On a first run there is no user, and
    the ``identity`` step brings the name and the email a user row needs; any other step
    submitted first is rejected, because there is nothing yet to write it onto.

    Args:
        step: Which step is being submitted.
        user: The acting user, or ``None`` when the install has no account yet.
        service: The onboarding service.
        payload: The step's answers, validated against that step's model.

    Returns:
        The wizard's state after the write, so the client can advance without a second
        request.

    Raises:
        RequestValidationError: If the body does not match the step's model.
        HTTPException: ``422`` when a step other than ``identity`` is submitted before an
            account exists, or when ``identity`` omits the email one needs.
    """
    model = _validate_step_payload(step, payload)
    user_id = None if user is None else user.id
    logger.info("api.onboarding_step_submitted", step=step, user_id=str(user_id))
    try:
        return await service.submit_step(user_id, step, model)
    except ValueError as exc:
        if user is not None:
            raise
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post(
    "/complete",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Finish onboarding and queue the first knowledge index",
)
async def complete_onboarding(
    user: CurrentUser,
    service: OnboardingServiceDep,
) -> OkResponse:
    """Mark onboarding complete and start indexing everything the user registered.

    Indexing is queued rather than awaited: a first pass crawls repositories, a portfolio
    site and a parsed resume, which is minutes of work and cannot hold a request open.

    Args:
        user: The acting user.
        service: The onboarding service.

    Returns:
        202 with the registered source ids and the dispatch outcome. When the broker is
        unreachable, ``data.degraded`` is true and the account is still onboarded — the
        indexing pass can be re-run later, and refusing to finish the wizard over a missing
        worker would strand the user in it.
    """
    source_ids = await service.complete(user.id)
    outcome = await dispatch(TASK_KNOWLEDGE_INDEX_ALL, str(user.id))

    # The message tracks the outcome rather than asserting success. This endpoint is the last
    # thing a new user sees, and it used to end the wizard with "3 knowledge source(s) queued
    # for indexing" whether or not anything would ever index them — which is precisely how an
    # install ends up with three sources at `pending`, zero facts, and a user who was told it
    # worked. A resume cannot be generated from a graph that was never built.
    count = len(source_ids)
    message = (
        f"Onboarding complete. {count} knowledge source(s) queued for indexing."
        if not outcome.degraded
        else (
            f"Onboarding complete, but the {count} knowledge source(s) have not started "
            "indexing: no background worker is available. Re-run it from the Knowledge screen."
        )
    )
    return OkResponse(
        message=message,
        data={"source_ids": [str(identifier) for identifier in source_ids], **outcome.as_dict()},
    )
