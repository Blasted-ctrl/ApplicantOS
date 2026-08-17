"""A run that owns its applications, honours its limits, and can actually be stopped.

Four defects motivated every test in this file, and each one was observed on the shipped
build before it was fixed:

1. **``Application.session_id`` was never written.** Eleven applications in the development
   database, none attributed to a run. Every per-run counter, cap and stop condition is a
   ``WHERE session_id = ...``, and a NULL matches none of them, so a run could not count,
   cap or stop its own work even in principle.
2. **Stop did not stop.** ``POST /sessions/{id}/stop`` marked the row ``cancelled``, and
   nothing anywhere read that. Up to a hundred prepared applications stayed on the queue and
   went on submitting minutes after the user was told the run was over.
3. **Neither application cap existed in code.** ``SessionStartRequest.max_applications`` was
   stored in a JSON blob nothing queried; ``settings.max_applications_per_session`` was
   declared, rendered in Settings, and read by no one.
4. **No run ever completed.** The only terminal writes were the user's Stop (``cancelled``)
   and the watchdog (``failed``). Four sessions existed on the development machine: two
   cancelled, two failed, zero completed.

The spy provider is the load-bearing part of the stop and cap tests, for the same reason it
is in ``test_golden_never_apply_twice.py``: asserting on a returned verdict would pass
against a pipeline that submits the application and *then* reports that it was blocked. If
``spy.calls`` is not zero, an employer received something after the user pressed Stop, and
no return value undoes that.
"""

from __future__ import annotations

import pytest

from app.models.enums import (
    STOP_REASON_SENTENCES,
    ApplicationStatus,
    SessionStatus,
    StopReason,
)
from app.models.session import RunSession
from app.services.pipeline import VERDICT_BLOCKED, VERDICT_SKIPPED, Pipeline
from app.services.session_service import SessionService, status_for


class SpyProvider:
    """An ATS provider that records whether it was ever asked to apply."""

    def __init__(self) -> None:
        self.calls: int = 0

    async def apply(self, ctx: object):
        """Record the attempt and report success, so a leak is loud rather than silent."""
        self.calls += 1
        from app.jobs.base import ApplyResult

        return ApplyResult(ok=True, status=ApplicationStatus.SUBMITTED)


@pytest.fixture
async def run(session, user) -> RunSession:
    """One running session belonging to the fixture user."""
    return await SessionService(session).start(user.id, "manual")


# ======================================================================================
# 1. A run owns the applications it produced
# ======================================================================================


async def test_prepare_attributes_the_application_to_its_run(
    session, settings, user, posting, run, monkeypatch
) -> None:
    """``prepare(session_id=...)`` writes ``Application.session_id``.

    Without this every other test in this file is untestable and every counter is a lie.
    """
    pipeline = Pipeline(session, settings)
    monkeypatch.setattr(
        Pipeline,
        "_generate_documents",
        _fake_generate,
    )

    application = await pipeline.prepare(posting.id, user.id, session_id=run.id)

    assert application.session_id == run.id


async def test_an_orphaned_application_is_adopted_by_the_first_run_that_touches_it(
    session, user, posting, run
) -> None:
    """A row created outside a run is back-filled, so review resolutions still count."""
    from app.services.application_service import ApplicationService

    applications = ApplicationService(session)
    orphan, created = await applications.create_or_get(user.id, posting.id)
    assert created is True
    assert orphan.session_id is None

    adopted, created_again = await applications.create_or_get(
        user.id, posting.id, session_id=run.id
    )

    assert created_again is False
    assert adopted.id == orphan.id
    assert adopted.session_id == run.id


async def test_an_application_is_never_re_attributed_to_a_second_run(
    session, user, posting, run
) -> None:
    """Yesterday's work stays credited to yesterday's run.

    Otherwise a run that merely passed over an old posting would steal its application, and
    both runs' figures would be wrong at once.
    """
    from app.services.application_service import ApplicationService

    applications = ApplicationService(session)
    await applications.create_or_get(user.id, posting.id, session_id=run.id)

    await SessionService(session).finish(run.id, SessionStatus.COMPLETED)
    second = await SessionService(session).start(user.id, "manual")
    assert second.id != run.id

    again, _ = await applications.create_or_get(user.id, posting.id, session_id=second.id)

    assert again.session_id == run.id


# ======================================================================================
# 2. Stop stops
# ======================================================================================


async def test_stop_refuses_a_submission_already_queued(
    session, submission_allowed, user, posting, make_application, make_score, run, monkeypatch
) -> None:
    """The whole point: a prepared, ready, fully qualified application is not sent.

    This is the shape of the real failure — the user presses Stop while a queue of prepared
    applications is in flight, and every one of them was still being submitted.
    """
    await make_score(posting, normalized=95)
    application = await make_application(posting, session_id=run.id)
    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    await SessionService(session).request_stop(run.id, StopReason.USER_STOPPED)
    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert spy.calls == 0
    assert result.verdict == VERDICT_BLOCKED
    assert result.submitted is False
    assert application.status is ApplicationStatus.READY


async def test_a_finished_run_refuses_a_submission_even_without_a_stop_request(
    session, submission_allowed, posting, make_application, make_score, run, monkeypatch
) -> None:
    """Ending is as good as stopping. A run that is over may not send anything more."""
    await make_score(posting, normalized=95)
    application = await make_application(posting, session_id=run.id)
    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    await SessionService(session).finish(run.id, SessionStatus.COMPLETED)
    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert spy.calls == 0
    assert result.verdict == VERDICT_BLOCKED


async def test_an_application_outside_any_run_is_not_blocked(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """A hand-created application belongs to no run, so no run's stop can apply to it.

    Refusing here would break ``POST /applications`` and every review resolution.
    """
    await make_score(posting, normalized=95)
    application = await make_application(posting, session_id=None)
    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert spy.calls == 1
    assert result.submitted is True


async def test_request_stop_keeps_the_run_running_until_it_is_closed(session, run) -> None:
    """The signal and the close are separate, so in-flight work can observe the signal."""
    service = SessionService(session)

    stopped = await service.request_stop(run.id, StopReason.USER_STOPPED)

    assert stopped.status is SessionStatus.RUNNING
    assert stopped.stop_requested_at is not None
    assert stopped.stop_reason is StopReason.USER_STOPPED
    assert stopped.is_halting is True


async def test_the_first_stop_reason_wins(session, run) -> None:
    """A user's stop is not relabelled by a limit the run hits while winding down."""
    service = SessionService(session)

    await service.request_stop(run.id, StopReason.USER_STOPPED)
    await service.request_stop(run.id, StopReason.LIMIT_REACHED)

    assert (await service.get(run.id)).stop_reason is StopReason.USER_STOPPED


async def test_halt_reason_is_none_for_work_outside_a_run(session) -> None:
    """``None`` in, ``None`` out — there is no run whose stop could be violated."""
    assert await SessionService(session).halt_reason(None) is None


async def test_halt_reason_survives_a_pruned_run(session, user) -> None:
    """A run deleted under a live application must not strand that application."""
    service = SessionService(session)
    import uuid as _uuid

    assert await service.halt_reason(_uuid.uuid4()) is None


# ======================================================================================
# 3. The caps are real
# ======================================================================================


async def test_the_runs_own_cap_blocks_the_next_submission(
    session,
    submission_allowed,
    make_posting,
    make_application,
    make_score,
    user,
    monkeypatch,
) -> None:
    """``max_applications=1`` means exactly one application is sent, not two."""
    service = SessionService(session)
    run = await service.start(user.id, "manual", max_applications=1)

    first = await make_posting(external_id="cap-1")
    second = await make_posting(external_id="cap-2")
    await make_score(first, normalized=95)
    await make_score(second, normalized=95)
    already = await make_application(
        first, session_id=run.id, status=ApplicationStatus.SUBMITTED
    )
    assert already.status is ApplicationStatus.SUBMITTED
    pending = await make_application(second, session_id=run.id)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))
    result = await Pipeline(session, submission_allowed).submit(pending.id)

    assert spy.calls == 0
    assert result.verdict == VERDICT_BLOCKED
    assert "limit of 1" in (result.message or "")


async def test_hitting_the_cap_stops_the_run_and_says_why(
    session,
    submission_allowed,
    make_posting,
    make_application,
    make_score,
    user,
    monkeypatch,
) -> None:
    """Reaching the cap winds the run down rather than letting it keep preparing.

    Directive §31: the UI must be able to say *"Stopped because the application limit of 1
    was reached."* — never just "Done."
    """
    service = SessionService(session)
    run = await service.start(user.id, "manual", max_applications=1)

    first = await make_posting(external_id="stop-1")
    second = await make_posting(external_id="stop-2")
    await make_score(second, normalized=95)
    await make_application(first, session_id=run.id, status=ApplicationStatus.SUBMITTED)
    pending = await make_application(second, session_id=run.id)

    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: SpyProvider()))
    await Pipeline(session, submission_allowed).submit(pending.id)

    stopped = await service.get(run.id)
    assert stopped.stop_reason is StopReason.LIMIT_REACHED
    assert stopped.stop_sentence == "Stopped because the application limit of 1 was reached."


async def test_the_configured_session_cap_applies_without_a_per_run_cap(
    session,
    submission_allowed,
    make_posting,
    make_application,
    make_score,
    user,
    monkeypatch,
) -> None:
    """``max_applications_per_session`` was dead code. It is now enforced."""
    monkeypatch.setattr(submission_allowed, "max_applications_per_session", 1)
    service = SessionService(session)
    run = await service.start(user.id, "manual")

    first = await make_posting(external_id="cfg-1")
    second = await make_posting(external_id="cfg-2")
    await make_score(second, normalized=95)
    await make_application(first, session_id=run.id, status=ApplicationStatus.SUBMITTED)
    pending = await make_application(second, session_id=run.id)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))
    result = await Pipeline(session, submission_allowed).submit(pending.id)

    assert spy.calls == 0
    assert result.verdict == VERDICT_BLOCKED


@pytest.mark.parametrize(
    ("requested", "configured", "expected"),
    [
        (None, 200, 200),
        (10, 200, 10),
        (500, 200, 200),
        (500, 20, 20),
        (0, 200, 0),
    ],
)
def test_a_request_may_narrow_the_cap_and_never_widen_it(
    requested: int | None, configured: int, expected: int
) -> None:
    """The security-shaped case is the 500-against-200 rows.

    If a request could raise the cap, ``max_applications_per_session`` would be advisory,
    which is not what a field called a maximum means.
    """
    run = RunSession(max_applications=requested)

    assert run.application_cap(configured_cap=configured) == expected


def test_the_cap_is_a_total_and_the_day_is_a_remainder() -> None:
    """The arithmetic bug this signature exists to prevent.

    ``application_cap`` used to take ``daily_remaining`` and fold it into the same ``min`` as
    the two run *totals*. Every application the run had already sent is inside both numbers —
    the day's usage and the run's own count — so subtracting the run's count from that
    minimum subtracted the same work twice. With the shipped defaults it halved the daily
    allowance: after 25 submissions a run reported "this run's limit of 25 was reached",
    a limit nobody had configured, and the next tick then blamed a daily limit of 50.

    Each allowance now belongs to its own count, and the caller intersects the remainders.
    """
    run = RunSession(max_applications=None)
    configured, daily_cap = 200, 50

    for submitted in range(daily_cap):
        # One run, nothing else today, so the day's usage *is* this run's output.
        cap = run.application_cap(configured_cap=configured)
        remaining = min(cap - submitted, daily_cap - submitted)
        assert remaining > 0, f"refused at {submitted} of a daily {daily_cap}"

    assert min(run.application_cap(configured_cap=configured) - daily_cap, 0) <= 0


@pytest.mark.parametrize(
    ("requested", "configured", "expected"),
    [(None, 70, 70), (90, 70, 90), (10, 70, 70), (0, 70, 70)],
)
def test_the_score_floor_can_only_be_raised(
    requested: int | None, configured: int, expected: int
) -> None:
    """A run may be pickier than the setting, never less picky.

    A lowerable floor would be a way to route around ``auto_apply_min_score`` by starting a
    run with a threshold of zero.
    """
    run = RunSession(match_threshold=requested)

    assert run.score_floor(configured_floor=configured) == expected


async def test_a_run_threshold_refuses_a_posting_the_setting_would_have_allowed(
    session, submission_allowed, posting, make_application, make_score, user, monkeypatch
) -> None:
    """The raised floor is enforced at the submit ladder, not merely stored."""
    monkeypatch.setattr(submission_allowed, "auto_apply_min_score", 50)
    run = await SessionService(session).start(user.id, "manual", match_threshold=90)
    await make_score(posting, normalized=60)
    application = await make_application(posting, session_id=run.id)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))
    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert spy.calls == 0
    assert result.verdict == VERDICT_SKIPPED
    assert "90" in (result.message or "")


async def test_a_negative_narrowing_is_refused(session, user) -> None:
    """A negative cap is always a caller bug, never a request."""
    with pytest.raises(ValueError, match="max_applications"):
        await SessionService(session).start(user.id, "manual", max_applications=-1)


async def test_a_threshold_above_the_band_is_refused(session, user) -> None:
    """Nothing can score above 100, so a run asking for 101 would apply to nothing forever."""
    with pytest.raises(ValueError, match="match_threshold"):
        await SessionService(session).start(user.id, "manual", match_threshold=101)


async def test_none_and_zero_are_different_requests(session, user) -> None:
    """``None`` means "use the setting"; zero means "apply to nothing"."""
    service = SessionService(session)
    unset = await service.start(user.id, "manual")
    assert unset.max_applications is None

    await service.finish(unset.id, SessionStatus.COMPLETED)
    zeroed = await service.start(user.id, "manual", max_applications=0)
    assert zeroed.max_applications == 0
    assert zeroed.application_cap(configured_cap=200) == 0


# ======================================================================================
# 4. A run can end for a reason, and say so
# ======================================================================================


@pytest.mark.parametrize("reason", list(StopReason))
def test_every_stop_reason_has_a_sentence(reason: StopReason) -> None:
    """Directive §31: never "Done." Adding a reason without copy is a gap, not a default."""
    assert reason in STOP_REASON_SENTENCES
    assert STOP_REASON_SENTENCES[reason].endswith((".", "!"))


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (StopReason.LIMIT_REACHED, SessionStatus.COMPLETED),
        (StopReason.DAILY_LIMIT_REACHED, SessionStatus.COMPLETED),
        (StopReason.NO_ELIGIBLE_JOBS, SessionStatus.COMPLETED),
        (StopReason.USER_STOPPED, SessionStatus.CANCELLED),
        (StopReason.STALLED, SessionStatus.FAILED),
        (StopReason.INFRASTRUCTURE_FAILURE, SessionStatus.FAILED),
    ],
)
def test_the_reason_decides_the_status(reason: StopReason, expected: SessionStatus) -> None:
    """One mapping, so a run cannot be ``completed`` with a reason meaning it broke."""
    assert status_for(reason) is expected


async def test_conclude_derives_the_status_from_the_reason(session, run) -> None:
    """A run that ran out of postings finished its work — it did not fail."""
    concluded = await SessionService(session).conclude(run.id, StopReason.NO_ELIGIBLE_JOBS)

    assert concluded.status is SessionStatus.COMPLETED
    assert concluded.stop_reason is StopReason.NO_ELIGIBLE_JOBS
    assert concluded.ended_at is not None
    assert "no eligible postings" in (concluded.stop_sentence or "")


async def test_the_watchdog_labels_a_silent_run_stalled(session, user) -> None:
    """A reaped run says why it was reaped, instead of being an unexplained failure."""
    from datetime import timedelta

    from app.database.types import utcnow

    service = SessionService(session)
    run = await service.start(user.id, "manual")
    run.updated_at = utcnow() - timedelta(hours=2)
    await session.commit()

    assert await service.watchdog(stale_after_minutes=1) == 1

    reaped = await service.get(run.id)
    assert reaped.status is SessionStatus.FAILED
    assert reaped.stop_reason is StopReason.STALLED


async def test_the_watchdog_does_not_relabel_a_run_that_already_knows_why(
    session, user
) -> None:
    """A run winding down after a user stop is not posthumously called stalled."""
    from datetime import timedelta

    from app.database.types import utcnow

    service = SessionService(session)
    run = await service.start(user.id, "manual")
    await service.request_stop(run.id, StopReason.USER_STOPPED)
    run.updated_at = utcnow() - timedelta(hours=2)
    await session.commit()

    await service.watchdog(stale_after_minutes=1)

    assert (await service.get(run.id)).stop_reason is StopReason.USER_STOPPED


def test_a_running_run_has_no_stop_sentence() -> None:
    """Nothing to explain yet, and an empty explanation beats an invented one."""
    assert RunSession().stop_sentence is None


# ======================================================================================
# 5. The submitted count is measured, not reported
# ======================================================================================


async def test_submitted_count_reads_the_rows_not_the_counter(
    session, make_posting, make_application, user
) -> None:
    """The cap is a safety limit, so it counts rows; a rollup counter is only a report.

    A counter increment lost to a crash between the submission and the ``UPDATE`` would
    silently raise the cap. A ``COUNT`` over the applications cannot.
    """
    service = SessionService(session)
    run = await service.start(user.id, "manual")

    for index in range(3):
        target = await make_posting(external_id=f"count-{index}")
        await make_application(target, session_id=run.id, status=ApplicationStatus.SUBMITTED)
    ignored = await make_posting(external_id="count-ready")
    await make_application(ignored, session_id=run.id, status=ApplicationStatus.READY)

    assert (await service.get(run.id)).applications_completed == 0
    assert await service.submitted_count(run.id) == 3


async def _fake_generate(self, application, user, posting) -> dict[str, int]:
    """Stand in for document generation, which is not what this file is testing."""
    return {"bullets": 4, "facts": 4}


# ======================================================================================
# 6. A run's stop is not a life sentence on the applications it left behind
# ======================================================================================


async def test_resolving_a_review_can_still_submit_after_its_run_ended(
    session, submission_allowed, posting, make_application, make_score, run, monkeypatch
) -> None:
    """The review queue is not a place applications go to die.

    Found by an adversarial review of the commit that introduced the stop rung, and the
    failure is quiet, which is the worst kind. A run concludes as soon as its remaining work
    is *parked* — ``run_loop.OUTSTANDING_STATES`` excludes ``NEEDS_REVIEW`` precisely so a run
    never waits on a human — so every review item is answered *after* its run has ended. The
    ended run read as halting forever, so resolving the review moved the application to
    ``READY``, out of the queue, and then the submit ladder refused it. The user was told
    they had applied and nothing had been sent.
    """
    await make_score(posting, normalized=95)
    application = await make_application(posting, session_id=run.id)
    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))
    await SessionService(session).conclude(run.id, StopReason.NO_ELIGIBLE_JOBS)

    refused = await Pipeline(session, submission_allowed).submit(application.id)
    assert refused.verdict == VERDICT_BLOCKED
    assert spy.calls == 0

    result = await Pipeline(session, submission_allowed).submit(application.id, requeued=True)

    assert spy.calls == 1
    assert result.submitted is True


async def test_a_requeue_still_obeys_every_guard_that_protects_the_employer(
    session, submission_allowed, posting, make_application, make_score, run, monkeypatch
) -> None:
    """``requeued`` lifts the run's stop and nothing else.

    Never-apply-twice in particular: a person pressing Retry on an application that was
    already sent must not send it again, whatever else they are permitted to override.
    """
    await make_score(posting, normalized=95)
    application = await make_application(
        posting, session_id=run.id, status=ApplicationStatus.SUBMITTED
    )
    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    result = await Pipeline(session, submission_allowed).submit(application.id, requeued=True)

    assert spy.calls == 0
    assert result.verdict == "already_applied"


async def test_a_requeue_still_obeys_the_daily_cap(
    session, submission_allowed, make_posting, make_application, make_score, run, monkeypatch
) -> None:
    """The cap that protects the user from themselves is not a run-level limit."""
    from app.database.types import utcnow

    monkeypatch.setattr(submission_allowed, "max_applications_per_day", 1)
    spent = await make_posting(external_id="requeue-daily-spent")
    await make_application(
        spent,
        session_id=run.id,
        status=ApplicationStatus.SUBMITTED,
        submitted_at=utcnow(),
    )
    target = await make_posting(external_id="requeue-daily-next")
    await make_score(target, normalized=95)
    application = await make_application(target, session_id=run.id)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))
    result = await Pipeline(session, submission_allowed).submit(application.id, requeued=True)

    assert spy.calls == 0
    assert result.verdict == VERDICT_BLOCKED
    assert "Daily limit" in (result.message or "")


async def test_an_ended_runs_cap_does_not_outlive_it(
    session, submission_allowed, make_posting, make_application, make_score, user, monkeypatch
) -> None:
    """An application left ``ready`` when its run stopped can still be sent later.

    Leaving it ``ready`` rather than failing it is the whole point of a cap refusal. If the
    ended run's cap kept applying, "ready" would mean "ready forever, for nobody".
    """
    service = SessionService(session)
    ended = await service.start(user.id, "manual", max_applications=1)
    spent = await make_posting(external_id="ended-cap-spent")
    await make_application(spent, session_id=ended.id, status=ApplicationStatus.SUBMITTED)
    left = await make_posting(external_id="ended-cap-left")
    await make_score(left, normalized=95)
    application = await make_application(left, session_id=ended.id)
    await service.conclude(ended.id, StopReason.LIMIT_REACHED)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))
    result = await Pipeline(session, submission_allowed).submit(application.id, requeued=True)

    assert spy.calls == 1
    assert result.submitted is True
