"""The live session panel is only live if somebody publishes.

``session.updated`` has been in ``docs/CONTRACTS.md`` §14 and in the closed
:data:`~app.api.events.EVENT_NAMES` set since the beginning, and
``desktop/src/lib/query/ws.ts`` handles it — merging the frame into the session detail *and*
into every cached list page. What was missing was a producer. Nothing in the backend ever
called ``bus.publish(EVENT_SESSION_UPDATED, ...)``, so a run displayed the zeros it started
with for its whole life and then jumped straight to its final figures when
``session.finished`` arrived.

That is the defect these tests pin down: not "does the counter increment" — that was already
true and already tested — but "does the increment leave the process".
"""

from __future__ import annotations

import pytest

from app.api.events import EVENT_SESSION_UPDATED, bus
from app.models.enums import SessionStatus
from app.services.session_service import SessionService
from app.workers.counters import record_session


@pytest.fixture
async def mailbox():
    """One subscriber listening for session updates, cleaned up afterwards.

    Async because a :class:`~app.api.events.Subscription` binds to the running loop at
    construction — the wakeup has to be schedulable onto the loop that will drain it.
    """
    subscription = bus.subscribe(events=[EVENT_SESSION_UPDATED])
    try:
        yield subscription
    finally:
        bus.unsubscribe(subscription)


async def _frames(subscription) -> list[dict]:
    """Return every frame waiting in *subscription* as its wire form."""
    if subscription.pending == 0:
        return []
    return [event.to_wire() for event in await subscription.drain()]


async def test_recording_a_counter_publishes_it(session, user, mailbox, monkeypatch) -> None:
    """The whole point: an increment reaches the wire, not just the table."""
    monkeypatch.setattr("app.workers.counters.session_scope", _scope_returning(session))
    run = await SessionService(session).start(user.id, "manual")

    payload = await record_session(str(run.id), jobs_found=12, jobs_qualified=3)

    assert payload is not None
    assert payload["jobs_found"] == 12
    assert payload["jobs_qualified"] == 3

    frames = await _frames(mailbox)
    assert [frame["event"] for frame in frames] == [EVENT_SESSION_UPDATED]
    assert frames[0]["payload"]["id"] == str(run.id)
    assert frames[0]["payload"]["jobs_found"] == 12


async def test_a_no_op_record_publishes_nothing(session, user, mailbox, monkeypatch) -> None:
    """Zero deltas are not news. A frame per no-op would flood the socket for nothing."""
    monkeypatch.setattr("app.workers.counters.session_scope", _scope_returning(session))
    run = await SessionService(session).start(user.id, "manual")

    assert await record_session(str(run.id), jobs_found=0) is None
    assert await _frames(mailbox) == []


async def test_work_outside_a_run_publishes_nothing(session, mailbox) -> None:
    """The scheduler's own polling belongs to no run, so there is no panel to update."""
    assert await record_session(None, jobs_found=5) is None
    assert await _frames(mailbox) == []


async def test_a_finished_run_swallows_the_counter_instead_of_failing(
    session, user, mailbox, monkeypatch
) -> None:
    """A task still in flight when the watchdog reaped its run must not fail because of it.

    Losing a counter is not a reason to fail the work the counter was describing.
    """
    monkeypatch.setattr("app.workers.counters.session_scope", _scope_returning(session))
    service = SessionService(session)
    run = await service.start(user.id, "manual")
    await service.finish(run.id, SessionStatus.COMPLETED)
    await session.delete(await service.get(run.id))
    await session.commit()

    assert await record_session(str(run.id), jobs_found=1) is None
    assert await _frames(mailbox) == []


async def test_the_published_frame_carries_the_stop_reason(
    session, user, mailbox, monkeypatch
) -> None:
    """The frame is a whole ``SessionRead``, so the UI never needs a second request."""
    from app.models.enums import StopReason

    monkeypatch.setattr("app.workers.counters.session_scope", _scope_returning(session))
    service = SessionService(session)
    run = await service.start(user.id, "manual", max_applications=7)
    await service.request_stop(run.id, StopReason.LIMIT_REACHED)

    await record_session(str(run.id), applications_completed=1)

    frame = (await _frames(mailbox))[0]
    assert frame["payload"]["stop_reason"] == StopReason.LIMIT_REACHED.value
    assert frame["payload"]["max_applications"] == 7
    assert (
        frame["payload"]["stop_sentence"]
        == "Stopped because the application limit of 7 was reached."
    )


def _scope_returning(session):
    """Build a ``session_scope`` replacement yielding the test's own session."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield session

    return _scope
