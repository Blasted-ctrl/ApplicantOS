"""The run loop — ``session.advance`` (``docs/CONTRACTS.md`` §15).

Directive §7 asks for a continuous worker: keep applying until stopped, until the cap is
reached, or until nothing eligible is left. The obvious shape is a ``while`` loop inside one
long-lived task, and it is the wrong one here. A loop that holds its state in local variables
dies with its process, and golden rule #8 says a crash resumes rather than restarts.

So the loop is turned inside out. Every iteration is one **tick**, its entire state lives in
the ``run_sessions`` and ``applications`` tables, and the scheduler supplies the ``while``.
Restart-safety then costs nothing: a worker killed mid-run leaves a row that the next tick
reads exactly as the previous tick left it. That is also why this file has no ``sleep``, no
retry loop and no in-memory queue.

One tick, in order:

1. **Stop asked for** → conclude with the reason that was asked for. First, because a stop
   the user pressed must not be relabelled by a cap the run happens to be at.
2. **Out of allowance** → conclude ``limit_reached`` or ``daily_limit_reached``. Checked
   before dispatching so a capped run stops *preparing*, rather than generating documents
   that rung 3 of the submit ladder will refuse one by one.
3. **Work still in flight** → do nothing and let the next tick look again. This is the
   branch that makes the loop sequential rather than a fan-out: a run with applications in
   ``preparing``, ``ready`` or ``submitting`` has already been given work.
4. **Eligible postings** → dispatch up to the remaining allowance.
5. **Nothing left** → conclude ``no_eligible_jobs``, or ``submission_disabled`` when the
   master switch is what emptied the list.

Every one of those five ends in a *reason*, which is the point. Before this file existed the
only ways a run could end were the user's Stop (``cancelled``) and the watchdog's fifteen
minutes of silence (``failed``) — so every unattended run in the product's history was
recorded as a failure, and the dashboard could never say why anything stopped.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import func, select

from app.api.events import EVENT_SESSION_FINISHED, bus
from app.api.tasks import TASK_APPLY_PREPARE, TASK_SESSION_ADVANCE
from app.config.settings import get_settings
from app.database.session import session_scope
from app.models.application import Application
from app.models.enums import ApplicationStatus, SessionStatus, StopReason
from app.models.session import RunSession
from app.workers import run_async, task_span
from app.workers.celery_app import celery_app, enqueue
from app.workers.retry import retryable

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "EMPTY_GRACE_SECONDS",
    "MAX_DISPATCH_PER_TICK",
    "OUTSTANDING_STATES",
    "advance",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: Applications that mean this run has already been given work and is still doing it. A run
#: with any of these outstanding dispatches nothing new, which is what makes the loop
#: sequential — the shipped behaviour fanned a hundred ``apply.prepare`` tasks out at once
#: and then had no way to stop them.
#:
#: ``NEEDS_REVIEW`` is deliberately absent: a review item is waiting on a *person*, and a run
#: that blocked on one would never end.
OUTSTANDING_STATES: Final[tuple[ApplicationStatus, ...]] = (
    ApplicationStatus.PREPARING,
    ApplicationStatus.READY,
    ApplicationStatus.SUBMITTING,
)

#: Ceiling on how many applications one tick may dispatch. The loop is meant to be paced by
#: its ticks, not to empty the candidate list in one go; a small batch keeps a stop responsive
#: and keeps the failure blast radius to one tick's worth of work.
MAX_DISPATCH_PER_TICK: Final[int] = 5

#: How long a run is given before "nothing to apply to" is allowed to end it.
#:
#: Without this the loop kills its own run: ``POST /sessions/start`` queues discovery and
#: returns, the first tick arrives while providers are still being polled, finds no scored
#: postings because none exist *yet*, and concludes ``no_eligible_jobs`` on a run that had
#: not begun. The window only ever delays a *conclusion* — dispatch, the caps and the stop
#: signal are all unaffected, so a run that does find work starts immediately.
#:
#: Sized against a real discovery pass over several boards rather than a guess: the measured
#: figure in ``docs/IMPLEMENTATION_STATUS.md`` is 245 postings across a provider list, which
#: is tens of seconds of HTTP.
EMPTY_GRACE_SECONDS: Final[int] = 180

#: Outcome names this task reports, so a caller (and a test) can assert on the branch taken
#: rather than on a log line.
OUTCOME_CONCLUDED: Final[str] = "concluded"
OUTCOME_WORKING: Final[str] = "working"
OUTCOME_DISPATCHED: Final[str] = "dispatched"


# ======================================================================================
# One tick
# ======================================================================================


async def _outstanding(session: AsyncSession, run_id: uuid.UUID) -> int:
    """Count applications this run has in flight.

    Args:
        session: The open async session.
        run_id: The run to measure.

    Returns:
        How many of this run's applications are still being worked on.
    """
    total = await session.scalar(
        select(func.count(Application.id)).where(
            Application.session_id == run_id,
            Application.status.in_(OUTSTANDING_STATES),
        )
    )
    return int(total or 0)


async def _candidates(
    session: AsyncSession,
    run: RunSession,
    *,
    limit: int,
) -> list[uuid.UUID]:
    """Return postings this run should apply to next, best first.

    Args:
        session: The open async session.
        run: The run, supplying the user and the run's own score floor.
        limit: Ceiling on how many to return.

    Returns:
        Posting ids, highest score first, excluding anything this user already has an
        application for.
    """
    from app.ai.scoring import VERDICT_APPLY
    from app.models.enums import PostingStatus
    from app.models.posting import JobPosting
    from app.models.score import JobScore

    settings = get_settings()
    floor = run.score_floor(configured_floor=int(settings.auto_apply_min_score))

    statement = (
        select(JobScore.posting_id)
        .join(JobPosting, JobPosting.id == JobScore.posting_id)
        .where(
            JobScore.user_id == run.user_id,
            JobScore.verdict == VERDICT_APPLY,
            JobScore.normalized >= floor,
            JobPosting.status == PostingStatus.SCORED,
            ~select(Application.id)
            .where(
                Application.user_id == run.user_id,
                Application.posting_id == JobScore.posting_id,
            )
            .exists(),
        )
        .order_by(JobScore.normalized.desc(), JobScore.posting_id.asc())
        .limit(max(1, int(limit)))
    )
    return list((await session.execute(statement)).scalars().all())


async def _conclude(session: AsyncSession, run: RunSession, reason: StopReason) -> dict[str, Any]:
    """Close a run for *reason* and publish the closure.

    Args:
        session: The open async session.
        run: The run to close.
        reason: Why it stopped.

    Returns:
        The tick outcome.
    """
    from app.schemas.session import SessionRead
    from app.services.session_service import SessionService

    closed = await SessionService(session).conclude(run.id, reason)
    item = SessionRead.model_validate(closed)
    bus.publish_model(EVENT_SESSION_FINISHED, item)

    logger.info(
        "run_loop.concluded",
        session_id=str(run.id),
        user_id=str(run.user_id),
        status=closed.status.value,
        stop_reason=reason.value,
        applications_completed=closed.applications_completed,
    )
    return {
        "session_id": str(run.id),
        "outcome": OUTCOME_CONCLUDED,
        "stop_reason": reason.value,
        "dispatched": 0,
    }


async def _tick(session: AsyncSession, run: RunSession) -> dict[str, Any]:
    """Advance one run by one step.

    Args:
        session: The open async session.
        run: The running session to advance.

    Returns:
        The tick outcome: which branch was taken and how much work it dispatched.
    """
    from app.services.application_service import ApplicationService
    from app.services.session_service import SessionService

    settings = get_settings()
    sessions = SessionService(session)

    # 1. A stop was asked for. Its reason wins over anything this tick would discover.
    if run.stop_requested_at is not None:
        return await _conclude(session, run, run.stop_reason or StopReason.USER_STOPPED)

    # 2. Allowance. Both caps are checked here as well as in the submit ladder, and that is
    #    not redundancy: the ladder refuses one application, this ends the run. Without it a
    #    capped run would keep tailoring résumés for applications that can never be sent.
    used_today = await ApplicationService(session).daily_count(run.user_id)
    daily_cap = int(settings.max_applications_per_day)
    if used_today >= daily_cap:
        return await _conclude(session, run, StopReason.DAILY_LIMIT_REACHED)

    submitted = await sessions.submitted_count(run.id)
    daily_remaining = daily_cap - used_today
    cap = run.application_cap(
        configured_cap=int(settings.max_applications_per_session),
        daily_remaining=daily_remaining,
    )
    remaining = cap - submitted
    if remaining <= 0:
        # Which limit actually bound matters, because the two promise different things: a
        # run cap means this run is done, and the daily cap means come back tomorrow. The
        # day is the binding one whenever what is left in it is no more than this run has
        # already spent — which is exactly when raising the run's own cap would change
        # nothing.
        return await _conclude(
            session,
            run,
            StopReason.DAILY_LIMIT_REACHED
            if daily_remaining <= submitted
            else StopReason.LIMIT_REACHED,
        )

    # 3. Still working. Nothing to decide until this run's own work lands.
    outstanding = await _outstanding(session, run.id)
    if outstanding > 0:
        logger.debug(
            "run_loop.working",
            session_id=str(run.id),
            outstanding=outstanding,
            remaining=remaining,
        )
        return {
            "session_id": str(run.id),
            "outcome": OUTCOME_WORKING,
            "outstanding": outstanding,
            "dispatched": 0,
        }

    # 4. Dispatch the next batch, never more than the run has allowance for.
    batch = min(remaining, MAX_DISPATCH_PER_TICK)
    candidates = await _candidates(session, run, limit=batch)
    if candidates:
        if not settings.auto_apply_enabled:
            # Golden rule #3's master switch. There is eligible work and the run is not
            # permitted to do it, so say that rather than claiming the list was empty.
            return await _conclude(session, run, StopReason.SUBMISSION_DISABLED)

        dispatched = 0
        for posting_id in candidates:
            if enqueue(
                TASK_APPLY_PREPARE,
                str(posting_id),
                str(run.user_id),
                session_id=str(run.id),
            ):
                dispatched += 1
        logger.info(
            "run_loop.dispatched",
            session_id=str(run.id),
            dispatched=dispatched,
            candidates=len(candidates),
            remaining=remaining,
        )
        return {
            "session_id": str(run.id),
            "outcome": OUTCOME_DISPATCHED,
            "dispatched": dispatched,
        }

    # 5. Nothing left — but "nothing yet" and "nothing at all" look identical from here, and
    #    only elapsed time tells them apart. A run younger than the grace window is still
    #    waiting on its own discovery pass.
    age = run.duration_seconds or 0.0
    if age < EMPTY_GRACE_SECONDS:
        logger.debug(
            "run_loop.waiting_for_discovery",
            session_id=str(run.id),
            age_seconds=round(age, 1),
            grace_seconds=EMPTY_GRACE_SECONDS,
        )
        return {
            "session_id": str(run.id),
            "outcome": OUTCOME_WORKING,
            "outstanding": 0,
            "dispatched": 0,
        }

    return await _conclude(session, run, StopReason.NO_ELIGIBLE_JOBS)


async def _advance_all(session_id: str | None) -> dict[str, Any]:
    """Advance every running session, or one named session.

    Args:
        session_id: A specific run, or ``None`` for every run still executing.

    Returns:
        A summary: how many runs were ticked, and each one's outcome.
    """
    async with session_scope() as session:
        statement = select(RunSession).where(RunSession.status == SessionStatus.RUNNING)
        if session_id is not None:
            statement = statement.where(RunSession.id == uuid.UUID(str(session_id)))
        runs = list(
            (await session.execute(statement.order_by(RunSession.started_at.asc())))
            .scalars()
            .all()
        )

        outcomes: list[dict[str, Any]] = []
        for run in runs:
            # One bad run never stops the sweep (directive §30). A run whose user was
            # deleted, or whose settings will not load, is closed as an infrastructure
            # failure rather than being left running forever for the watchdog to guess at.
            try:
                outcomes.append(await _tick(session, run))
            except Exception as exc:
                logger.exception(
                    "run_loop.tick_failed",
                    session_id=str(run.id),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                try:
                    outcomes.append(
                        await _conclude(session, run, StopReason.INFRASTRUCTURE_FAILURE)
                    )
                except Exception:
                    logger.exception("run_loop.conclude_failed", session_id=str(run.id))

    return {"runs": len(runs), "outcomes": outcomes}


# ======================================================================================
# session.advance
# ======================================================================================


@celery_app.task(name=TASK_SESSION_ADVANCE, bind=False)
@retryable()
def advance(session_id: str | None = None) -> dict[str, Any]:
    """Advance every running session by one step. The ``while`` of the run loop.

    Called by beat every minute, and directly by ``POST /sessions/start`` so a run does not
    have to wait a tick to begin.

    Idempotent and safe to run twice: every branch is derived from committed rows, and the
    only mutating branches either close an already-closing run or dispatch work that
    ``UNIQUE(user_id, posting_id)`` and the submit ladder deduplicate anyway.

    Args:
        session_id: Advance only this run. ``None`` — the scheduler's call — advances every
            run still executing.

    Returns:
        ``{"runs": int, "outcomes": [...]}``, one outcome per run ticked.
    """
    with task_span(TASK_SESSION_ADVANCE, session_id=session_id) as log:
        summary = run_async(_advance_all(session_id))
        log.info(
            "run_loop.swept",
            runs=summary["runs"],
            concluded=sum(
                1 for item in summary["outcomes"] if item.get("outcome") == OUTCOME_CONCLUDED
            ),
            dispatched=sum(int(item.get("dispatched", 0)) for item in summary["outcomes"]),
        )
        return summary
