"""Golden rule #8 — everything is resumable; a crash resumes rather than restarts.

The failure this rule exists to prevent is not "work is lost". It is a row left **in flight
forever**: a checkpoint stuck in ``running``, or an application stuck in ``submitting``, that
no recovery pass can distinguish from work still happening. Nobody is ever going to notice.

Cancellation is where that happens, and it is the case an ``except Exception`` handler
silently misses — :class:`asyncio.CancelledError` derives from :class:`BaseException`. A
Celery warm shutdown, a Ctrl-C and a task revocation all arrive as exactly that. So both
tests here cancel rather than merely raise:

* :class:`~app.services.checkpoint_service.CheckpointService.step` must mark the checkpoint
  ``failed`` on a cancellation and re-raise it untouched.
* ``Pipeline.submit`` cancelled between its ``SUBMITTING`` commit and the provider's return
  must park the application in review with ``VERIFICATION_FAILED`` — because at that point
  nobody knows whether the employer received it, and guessing either way is worse than
  asking. It must **not** be left in ``submitting``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.enums import (
    ApplicationStatus,
    CheckpointStatus,
    ReviewReason,
)
from app.services.checkpoint_service import (
    OWNER_APPLY,
    CheckpointService,
    owner_key,
    step_key,
)


@pytest.fixture
def checkpoints(session) -> CheckpointService:
    """A checkpoint service over the test session."""
    return CheckpointService(session)


def _keys(application_id, step: str = "submit") -> tuple[str, str]:
    """An ``(owner, key)`` pair for one application's step."""
    owner = owner_key(OWNER_APPLY, application_id)
    return owner, step_key(owner, step)


# ======================================================================================
# CheckpointService.step
# ======================================================================================


async def test_step_completes_on_a_clean_exit(checkpoints, application) -> None:
    """The happy path, so the failure assertions below are not vacuous."""
    owner, key = _keys(application.id)

    async with checkpoints.step(key, owner, "submit", {"attempt": 1}) as checkpoint:
        assert checkpoint.status is CheckpointStatus.RUNNING

    stored = await checkpoints.load(key)
    assert stored is not None
    assert stored.status is CheckpointStatus.SUCCEEDED


async def test_step_marks_failed_on_an_exception_and_reraises(checkpoints, application) -> None:
    """An ordinary failure is recorded and the exception propagates unchanged."""
    owner, key = _keys(application.id)

    with pytest.raises(RuntimeError, match="provider exploded"):
        async with checkpoints.step(key, owner, "submit"):
            raise RuntimeError("provider exploded")

    stored = await checkpoints.load(key)
    assert stored is not None
    assert stored.status is CheckpointStatus.FAILED
    assert "provider exploded" in (stored.last_error or "")


async def test_step_marks_failed_on_cancellation_and_reraises(checkpoints, application) -> None:
    """**The one that matters.** ``CancelledError`` must not slip past as ``BaseException``.

    Without the explicit handler the step stays ``running`` forever, and a recovery pass
    cannot tell it from a step still in flight.
    """
    owner, key = _keys(application.id)

    with pytest.raises(asyncio.CancelledError):
        async with checkpoints.step(key, owner, "submit"):
            raise asyncio.CancelledError()

    stored = await checkpoints.load(key)
    assert stored is not None
    assert stored.status is CheckpointStatus.FAILED, "a cancelled step was left running"
    assert stored.status is not CheckpointStatus.RUNNING


async def test_a_cancelled_step_is_left_resumable(checkpoints, application) -> None:
    """Failure is a resumable state — that is the whole point of the checkpoint."""
    owner, key = _keys(application.id)

    with pytest.raises(asyncio.CancelledError):
        async with checkpoints.step(key, owner, "submit", {"stage": "opening"}):
            raise asyncio.CancelledError()

    resumable = await checkpoints.resume_all(owner)
    assert [c.key for c in resumable] == [key]


async def test_a_real_task_cancellation_is_recorded(checkpoints, application) -> None:
    """The realistic shape: an outer ``task.cancel()`` rather than a manual raise.

    This is what a Celery warm shutdown actually does, and it exercises the cancellation
    arriving *at an await point* inside the body rather than as a synchronous raise.
    """
    owner, key = _keys(application.id, step="render")
    started = asyncio.Event()

    async def _work() -> None:
        async with checkpoints.step(key, owner, "render"):
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(_work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await checkpoints.load(key)
    assert stored is not None
    assert stored.status is CheckpointStatus.FAILED


async def test_state_assigned_inside_the_block_survives(checkpoints, application) -> None:
    """Progress written into the checkpoint is what makes a resume cheaper than a restart."""
    owner, key = _keys(application.id, step="fill")

    with pytest.raises(RuntimeError):
        async with checkpoints.step(key, owner, "fill", {"fields_done": 0}) as checkpoint:
            checkpoint.state = {"fields_done": 7}
            raise RuntimeError("page closed")

    stored = await checkpoints.load(key)
    assert stored is not None
    assert stored.state.get("fields_done") == 7


# ======================================================================================
# Pipeline.submit under cancellation
# ======================================================================================


class CancellingProvider:
    """A provider whose ``apply`` is cancelled mid-flight, after ``SUBMITTING`` committed."""

    def __init__(self) -> None:
        self.calls = 0

    async def apply(self, _ctx):
        self.calls += 1
        raise asyncio.CancelledError()


@pytest.fixture
def submittable(monkeypatch):
    """Stub the document materialisation so the cancellation test reaches the provider."""
    from app.services.pipeline import Pipeline

    async def _materialize(self, application, posting):
        return None, None

    monkeypatch.setattr(Pipeline, "_materialize_documents", _materialize)


async def test_cancelled_submission_is_parked_for_review_not_left_submitting(
    session,
    submission_allowed,
    monkeypatch,
    submittable,
    make_posting,
    make_application,
    make_score,
) -> None:
    """A cancellation mid-submission must reach the review queue, never stay in flight.

    ``VERIFICATION_FAILED`` is exactly right: the outcome is genuinely unknown — the provider
    may never have run, or it may have submitted and been interrupted while recording the
    result — and a human settles it from their inbox in seconds.
    """
    from app.services.pipeline import Pipeline

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    provider = CancellingProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: provider))

    with pytest.raises(asyncio.CancelledError):
        await Pipeline(session, submission_allowed).submit(application.id)

    assert provider.calls == 1, "the test did not reach the provider, so it proves nothing"

    await session.refresh(application)
    assert application.status is not ApplicationStatus.SUBMITTING, (
        "a cancelled submission was stranded in 'submitting' with no path out"
    )
    assert application.status is ApplicationStatus.NEEDS_REVIEW
    assert application.review_reason is ReviewReason.VERIFICATION_FAILED


async def test_the_parked_row_explains_that_the_outcome_is_unknown(
    session,
    submission_allowed,
    monkeypatch,
    submittable,
    make_posting,
    make_application,
    make_score,
) -> None:
    """The review payload has to tell the human what to check; a bare status does not."""
    from app.services.pipeline import Pipeline

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: CancellingProvider()))

    with pytest.raises(asyncio.CancelledError):
        await Pipeline(session, submission_allowed).submit(application.id)

    await session.refresh(application)
    payload = application.review_payload or {}
    assert payload.get("cause") == "cancelled_mid_submission"
    assert "email" in (payload.get("note") or "").lower()


async def test_a_stranded_row_can_never_be_silently_resubmitted(
    session,
    submission_allowed,
    monkeypatch,
    submittable,
    make_posting,
    make_application,
    make_score,
) -> None:
    """Guard 3 refuses anything that is not ``READY``, so the parked row is not re-sent.

    Belt and braces on top of golden rule #1: even if the cleanup above regressed, the
    interrupted application must not become a second real submission on the next pass.
    """
    from app.services.pipeline import Pipeline

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    cancelling = CancellingProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: cancelling))

    with pytest.raises(asyncio.CancelledError):
        await Pipeline(session, submission_allowed).submit(application.id)

    # A second pass, with a provider that would happily submit.
    class WouldSubmit:
        def __init__(self) -> None:
            self.calls = 0

        async def apply(self, _ctx):
            self.calls += 1
            from app.jobs.base import ApplyResult

            return ApplyResult(ok=True, status=ApplicationStatus.SUBMITTED)

    eager = WouldSubmit()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: eager))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert eager.calls == 0, "a cancelled application was re-submitted on the next pass"
    assert result.submitted is False


async def test_an_ordinary_provider_failure_does_not_strand_the_row(
    session,
    submission_allowed,
    monkeypatch,
    submittable,
    make_posting,
    make_application,
    make_score,
) -> None:
    """A plain exception must also leave the application out of ``submitting``."""
    from app.services.pipeline import Pipeline

    posting = await make_posting()
    await make_score(posting, normalized=91)
    application = await make_application(posting, status=ApplicationStatus.READY)

    class Exploding:
        async def apply(self, _ctx):
            raise RuntimeError("network died")

    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: Exploding()))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    await session.refresh(application)
    assert application.status is not ApplicationStatus.SUBMITTING
    assert result.submitted is False
