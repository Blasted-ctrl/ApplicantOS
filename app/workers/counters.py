"""Recording a run's counters, and telling the desktop app that they moved.

Three task modules used to carry their own private copy of this function — byte-identical in
``poll_jobs`` and ``apply_jobs``, and about to be needed by a third. They were identical in
the wrong way too: none of them published anything, so a run's counters changed in the
database and the live dashboard never heard about it.

That was the whole of the "live" in the live session panel. ``session.updated`` is declared
in ``docs/CONTRACTS.md`` §14, is in the closed :data:`~app.api.events.EVENT_NAMES` set, and
is handled by ``desktop/src/lib/query/ws.ts`` — which merges the frame into the session
detail *and* every cached list page. The producer was the only missing piece, so a run
displayed the zeros it was started with until it ended and the ``session.finished`` frame
replaced them all at once.

Publishing here rather than inside
:meth:`~app.services.session_service.SessionService.record` is deliberate: the service layer
does not import from ``app.api``, and inverting that to save one call would put the event bus
underneath every service in the project.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.api.events import EVENT_SESSION_UPDATED, bus
from app.database.session import session_scope

__all__ = ["record_session"]

logger = structlog.get_logger(__name__)


async def record_session(session_id: str | None, **deltas: int) -> dict[str, Any] | None:
    """Add *deltas* to a run's counters and publish the result.

    Args:
        session_id: The run session id, or ``None`` for work that belongs to no run.
        **deltas: Counter increments, validated by
            :meth:`~app.services.session_service.SessionService.record`. Zero-valued deltas
            are dropped by the service, so a call that changes nothing emits no ``UPDATE``
            and — because the counters did not move — no event either.

    Returns:
        The serialised session, or ``None`` when there was nothing to record.
    """
    if not session_id or not any(deltas.values()):
        return None

    from app.schemas.session import SessionRead
    from app.services.session_service import SessionService

    try:
        async with session_scope() as session:
            run = await SessionService(session).record(session_id, **deltas)
            item = SessionRead.model_validate(run)
    except (LookupError, ValueError) as exc:
        # A run that finished, or was reaped by the watchdog, while its tasks were still in
        # flight. Losing a counter is not a reason to fail the work it was counting.
        logger.warning("workers.session_record_failed", session_id=session_id, error=str(exc))
        return None

    bus.publish_model(EVENT_SESSION_UPDATED, item)
    return item.model_dump(mode="json")
