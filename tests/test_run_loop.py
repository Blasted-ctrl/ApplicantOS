"""The run loop — ``session.advance``, one tick at a time.

Every test here drives ``_tick`` directly rather than the Celery task, because the tick *is*
the loop body: the task around it only picks which runs to tick and swallows one bad row. A
test that went through the task would be testing the scheduler.

The branch order is load-bearing and is asserted as such. A stop must win over a cap, a cap
must win over dispatch, and outstanding work must win over concluding — get any of those the
wrong way round and the run either ends while it is still working or keeps working after it
was told to stop.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.database.types import utcnow
from app.models.enums import ApplicationStatus, SessionStatus, StopReason
from app.services.session_service import SessionService
from app.workers.run_loop import (
    EMPTY_GRACE_SECONDS,
    MAX_DISPATCH_PER_TICK,
    OUTCOME_CONCLUDED,
    OUTCOME_DISPATCHED,
    OUTCOME_WORKING,
    _tick,
)


@pytest.fixture
def dispatched(monkeypatch) -> list[tuple]:
    """Capture what the tick handed to the queue, without a broker."""
    calls: list[tuple] = []

    def _enqueue(name: str, *args, **kwargs) -> str:
        calls.append((name, args, kwargs))
        return f"task-{len(calls)}"

    monkeypatch.setattr("app.workers.run_loop.enqueue", _enqueue)
    return calls


@pytest.fixture
async def aged_run(session, user):
    """A run old enough that the discovery grace window has passed.

    Most tests are about what the loop decides, not about waiting, so they start from a run
    the grace window no longer protects.
    """
    run = await SessionService(session).start(user.id, "manual")
    run.started_at = utcnow() - timedelta(seconds=EMPTY_GRACE_SECONDS + 60)
    await session.commit()
    return run


@pytest.fixture
def applying(settings, monkeypatch):
    """Settings with the master switch on, so the loop is permitted to dispatch."""
    monkeypatch.setattr(settings, "auto_apply_enabled", True)
    return settings


# ======================================================================================
# Concluding — the thing that never happened before
# ======================================================================================


async def test_a_run_with_nothing_left_completes_instead_of_going_stale(
    session, applying, aged_run, dispatched
) -> None:
    """The headline fix.

    Before the loop existed nothing ever wrote ``completed``. A run that had finished its
    work simply stopped updating, and fifteen minutes later the watchdog recorded it as a
    **failure**. Every unattended run in the product's history ended that way.
    """
    outcome = await _tick(session, aged_run)

    assert outcome["outcome"] == OUTCOME_CONCLUDED
    assert outcome["stop_reason"] == StopReason.NO_ELIGIBLE_JOBS.value

    closed = await SessionService(session).get(aged_run.id)
    assert closed.status is SessionStatus.COMPLETED
    assert closed.ended_at is not None
    assert dispatched == []


async def test_a_young_empty_run_waits_rather_than_killing_itself(
    session, applying, run_now, dispatched
) -> None:
    """Discovery has not landed yet, and "nothing yet" is not "nothing at all".

    Without the grace window the first tick after ``POST /sessions/start`` would conclude a
    run that had not begun — the API queues discovery and returns, so at that instant there
    are legitimately no scored postings.
    """
    outcome = await _tick(session, run_now)

    assert outcome["outcome"] == OUTCOME_WORKING
    assert (await SessionService(session).get(run_now.id)).status is SessionStatus.RUNNING


async def test_a_stop_request_concludes_the_run_with_its_own_reason(
    session, applying, aged_run, dispatched
) -> None:
    """Branch 1 wins over every later branch, so a stop is never relabelled."""
    await SessionService(session).request_stop(aged_run.id, StopReason.USER_STOPPED)

    outcome = await _tick(session, aged_run)

    assert outcome["stop_reason"] == StopReason.USER_STOPPED.value
    assert (await SessionService(session).get(aged_run.id)).status is SessionStatus.CANCELLED


async def test_the_master_switch_gets_its_own_reason(
    session, settings, aged_run, make_posting, make_score, dispatched, monkeypatch
) -> None:
    """A fresh install ships with auto-apply off, so this is the *common* ending.

    Reporting it as "no eligible postings" would be false — there were eligible postings and
    the run was not allowed to touch them.
    """
    monkeypatch.setattr(settings, "auto_apply_enabled", False)
    target = await make_posting(external_id="switch-off")
    await make_score(target, normalized=95)

    outcome = await _tick(session, aged_run)

    assert outcome["stop_reason"] == StopReason.SUBMISSION_DISABLED.value
    assert dispatched == []
    closed = await SessionService(session).get(aged_run.id)
    assert closed.status is SessionStatus.COMPLETED
    assert "auto-apply is switched off" in (closed.stop_sentence or "")


# ======================================================================================
# Pacing — one batch at a time, not a hundred at once
# ======================================================================================


async def test_the_tick_dispatches_the_best_candidates(
    session, applying, aged_run, make_posting, make_score, dispatched, user
) -> None:
    """Highest score first, so a capped run spends its allowance on the best postings.

    The 60 is deliberately below ``auto_apply_min_score`` and must not be dispatched at all:
    the loop's candidate query applies the floor itself rather than leaving it to the submit
    ladder, so a run never spends a résumé generation on a posting it cannot send.
    """
    for index, score in enumerate((60, 99, 80)):
        target = await make_posting(external_id=f"rank-{index}")
        await make_score(target, normalized=score)

    outcome = await _tick(session, aged_run)

    assert outcome["outcome"] == OUTCOME_DISPATCHED
    assert outcome["dispatched"] == 2
    assert {call[0] for call in dispatched} == {"apply.prepare"}
    assert all(call[2]["session_id"] == str(aged_run.id) for call in dispatched)


async def test_one_tick_never_dispatches_more_than_its_batch(
    session, applying, aged_run, make_posting, make_score, dispatched
) -> None:
    """The loop is paced by ticks. Emptying the list in one go is the fan-out it replaced."""
    for index in range(MAX_DISPATCH_PER_TICK + 4):
        target = await make_posting(external_id=f"batch-{index}")
        await make_score(target, normalized=90)

    outcome = await _tick(session, aged_run)

    assert outcome["dispatched"] == MAX_DISPATCH_PER_TICK


async def test_a_tick_dispatches_nothing_while_the_run_still_has_work(
    session, applying, aged_run, make_posting, make_score, make_application, dispatched
) -> None:
    """Branch 3: sequential, not concurrent. This is what "one at a time" means in code."""
    busy = await make_posting(external_id="busy")
    await make_application(busy, session_id=aged_run.id, status=ApplicationStatus.PREPARING)
    waiting = await make_posting(external_id="waiting")
    await make_score(waiting, normalized=95)

    outcome = await _tick(session, aged_run)

    assert outcome["outcome"] == OUTCOME_WORKING
    assert outcome["outstanding"] == 1
    assert dispatched == []


async def test_a_review_item_does_not_hold_the_run_open_forever(
    session, applying, aged_run, make_posting, make_application, dispatched
) -> None:
    """A review is waiting on a *person*. A run that blocked on one would never end."""
    stuck = await make_posting(external_id="in-review")
    await make_application(stuck, session_id=aged_run.id, status=ApplicationStatus.NEEDS_REVIEW)

    outcome = await _tick(session, aged_run)

    assert outcome["outcome"] == OUTCOME_CONCLUDED
    assert outcome["stop_reason"] == StopReason.NO_ELIGIBLE_JOBS.value


async def test_the_tick_never_dispatches_beyond_the_remaining_allowance(
    session, applying, user, make_posting, make_score, make_application, dispatched
) -> None:
    """Two left in the cap means two dispatched, not five."""
    run = await SessionService(session).start(user.id, "manual", max_applications=3)
    run.started_at = utcnow() - timedelta(seconds=EMPTY_GRACE_SECONDS + 60)
    await session.commit()

    for index in range(1):
        done = await make_posting(external_id=f"spent-{index}")
        await make_application(done, session_id=run.id, status=ApplicationStatus.SUBMITTED)
    for index in range(6):
        target = await make_posting(external_id=f"left-{index}")
        await make_score(target, normalized=90)

    outcome = await _tick(session, run)

    assert outcome["dispatched"] == 2


async def test_a_posting_below_the_runs_own_threshold_is_not_dispatched(
    session, applying, user, make_posting, make_score, dispatched, monkeypatch
) -> None:
    """The run's raised floor governs what the loop even considers, not just what it sends."""
    monkeypatch.setattr(applying, "auto_apply_min_score", 50)
    run = await SessionService(session).start(user.id, "manual", match_threshold=95)
    run.started_at = utcnow() - timedelta(seconds=EMPTY_GRACE_SECONDS + 60)
    await session.commit()

    below = await make_posting(external_id="below-run-floor")
    await make_score(below, normalized=70)

    outcome = await _tick(session, run)

    assert dispatched == []
    assert outcome["stop_reason"] == StopReason.NO_ELIGIBLE_JOBS.value


# ======================================================================================
# Caps end the run, they do not just refuse one application
# ======================================================================================


async def test_a_spent_run_cap_ends_the_run(
    session, applying, user, make_posting, make_application, make_score, dispatched
) -> None:
    """Otherwise a capped run keeps tailoring résumés nothing is permitted to send."""
    run = await SessionService(session).start(user.id, "manual", max_applications=1)
    run.started_at = utcnow() - timedelta(seconds=EMPTY_GRACE_SECONDS + 60)
    await session.commit()

    done = await make_posting(external_id="cap-spent")
    await make_application(done, session_id=run.id, status=ApplicationStatus.SUBMITTED)
    waiting = await make_posting(external_id="cap-waiting")
    await make_score(waiting, normalized=95)

    outcome = await _tick(session, run)

    assert outcome["stop_reason"] == StopReason.LIMIT_REACHED.value
    assert dispatched == []
    closed = await SessionService(session).get(run.id)
    assert closed.status is SessionStatus.COMPLETED
    assert closed.stop_sentence == "Stopped because the application limit of 1 was reached."


async def test_the_daily_cap_ends_the_run_and_says_it_resumes_tomorrow(
    session, applying, aged_run, make_posting, make_application, dispatched, monkeypatch
) -> None:
    """The daily cap spans runs, so its sentence has to promise tomorrow rather than never.

    ``submitted_at`` is stamped explicitly because that is the column
    :meth:`~app.services.application_service.ApplicationService.daily_count` measures — the
    cap bounds what was *sent*, not what was prepared.
    """
    monkeypatch.setattr(applying, "max_applications_per_day", 1)
    spent = await make_posting(external_id="daily-spent")
    await make_application(
        spent,
        session_id=aged_run.id,
        status=ApplicationStatus.SUBMITTED,
        submitted_at=utcnow(),
    )

    outcome = await _tick(session, aged_run)

    assert outcome["stop_reason"] == StopReason.DAILY_LIMIT_REACHED.value
    closed = await SessionService(session).get(aged_run.id)
    assert "resume tomorrow" in (closed.stop_sentence or "")


async def test_a_run_is_not_stopped_at_half_the_daily_cap(
    session, applying, aged_run, make_posting, make_application, make_score, dispatched, monkeypatch
) -> None:
    """Regression: the day's remainder and the run's total used to be subtracted together.

    Twenty-five submissions against a daily cap of fifty left the run with a *computed*
    allowance of zero, so it stopped at half of what the user had configured and then blamed
    a limit of fifty it had never reached.
    """
    monkeypatch.setattr(applying, "max_applications_per_day", 50)
    for index in range(25):
        done = await make_posting(external_id=f"half-{index}")
        await make_application(
            done,
            session_id=aged_run.id,
            status=ApplicationStatus.SUBMITTED,
            submitted_at=utcnow(),
        )
    waiting = await make_posting(external_id="half-next")
    await make_score(waiting, normalized=95)

    outcome = await _tick(session, aged_run)

    assert outcome["outcome"] == OUTCOME_DISPATCHED
    assert outcome["dispatched"] == 1


async def test_a_stop_beats_a_cap_when_both_apply(
    session, applying, user, make_posting, make_application, dispatched
) -> None:
    """Branch order, asserted directly: the user's reason is the one recorded."""
    run = await SessionService(session).start(user.id, "manual", max_applications=1)
    spent = await make_posting(external_id="both-apply")
    await make_application(spent, session_id=run.id, status=ApplicationStatus.SUBMITTED)
    await SessionService(session).request_stop(run.id, StopReason.USER_STOPPED)

    outcome = await _tick(session, run)

    assert outcome["stop_reason"] == StopReason.USER_STOPPED.value


# ======================================================================================
# The sweep survives a bad run
# ======================================================================================


async def test_one_broken_run_does_not_stop_the_sweep(session, applying, user, monkeypatch) -> None:
    """Directive §30: never let one bad row kill the whole thing.

    The broken run is closed as an infrastructure failure rather than left ``running``
    forever for the watchdog to guess at.
    """
    from app.workers import run_loop

    service = SessionService(session)
    broken = await service.start(user.id, "manual")

    calls: list[str] = []

    async def _explode(_session, run):
        calls.append(str(run.id))
        raise RuntimeError("the database went away")

    monkeypatch.setattr(run_loop, "_tick", _explode)
    monkeypatch.setattr(run_loop, "session_scope", _scope_returning(session))

    summary = await run_loop._advance_all(None)

    assert calls == [str(broken.id)]
    assert summary["runs"] == 1
    closed = await service.get(broken.id)
    assert closed.status is SessionStatus.FAILED
    assert closed.stop_reason is StopReason.INFRASTRUCTURE_FAILURE


def _scope_returning(session):
    """Build a ``session_scope`` replacement yielding the test's own session.

    The sweep opens its own unit of work, which a test cannot see into. Handing it the
    session the fixtures wrote through is what lets the assertions read the result.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield session

    return _scope


@pytest.fixture
async def run_now(session, user):
    """A run that has only just started, still inside the discovery grace window."""
    return await SessionService(session).start(user.id, "manual")
