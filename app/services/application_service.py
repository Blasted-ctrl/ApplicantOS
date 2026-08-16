"""Applications — the state machine that owns every lifecycle change (§13).

One rule governs this module: **an application's status only ever changes through
:meth:`ApplicationService.transition`**, and that method only ever accepts a move listed in
:data:`ALLOWED_TRANSITIONS`. Nothing else in the codebase assigns
``Application.status``. That is what makes golden rule #1 auditable rather than merely
intended — ``submitted`` has no outgoing edge back to any pre-submit state, so no sequence
of calls, however confused, can walk an already-sent application back to ``ready`` and
re-send it. The ``UNIQUE(user_id, posting_id)`` index is the other half; a constraint stops
a *second row*, a state machine stops a *second send from the same row*.

Two consequences of that design are worth stating explicitly:

* **Every transition writes an :class:`~app.models.application.ApplicationEvent`.** The
  audit trail is not a nice-to-have that a busy code path may skip: "why did this
  application end up in review at 3am?" has to be answerable from the database months
  later, and events are the only record that outlives log rotation.
* **Every transition commits.** This service is the one exception to the flush-never-commit
  convention the rest of ``app/services`` follows, and deliberately so. A status change is
  the durable fact that a later run reads to decide whether work is still outstanding; a
  ``submitting`` that was never committed because the process died mid-browser-flow is
  indistinguishable from an application that was never attempted, and golden rule #8
  (everything is resumable) depends on the difference.

Errors follow the package convention: :class:`LookupError` for an id that does not exist
(``404``) and :class:`InvalidTransition` — a :class:`ValueError` — for a move the machine
forbids (``400``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm.attributes import set_committed_value

from app.database.types import utcnow
from app.models.application import Application
from app.models.enums import (
    APPLICATION_POST_SUBMIT_STATES,
    ApplicationStatus,
    ReviewReason,
)
from app.models.posting import JobPosting
from app.observability.metrics import record_application

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_TIMESERIES_DAYS",
    "LAST_ERROR_MAX_CHARS",
    "MAX_TIMESERIES_DAYS",
    "ApplicationService",
    "InvalidTransition",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: Longest error message stored on ``applications.last_error``. The column is ``Text`` and
#: could hold more, but a 40kB stack trace pasted into a desktop review card helps nobody,
#: and the full detail is already in the structured log.
LAST_ERROR_MAX_CHARS: Final[int] = 2000

#: Default width of :meth:`ApplicationService.timeseries`, matching the dashboard's chart.
DEFAULT_TIMESERIES_DAYS: Final[int] = 30

#: Hard ceiling on :meth:`ApplicationService.timeseries`, so a hand-crafted query string
#: cannot ask the process to build a ten-year day-by-day series in memory.
MAX_TIMESERIES_DAYS: Final[int] = 365

#: Event kind written when a transition is recorded without a more specific name.
_EVENT_KIND_STATUS: Final[str] = "status_changed"

#: Event kind written by :meth:`ApplicationService.record_error`.
_EVENT_KIND_ERROR: Final[str] = "error"


#: **The state machine.** Keys are the current status, values are every status that may
#: follow it. Read it as the product's actual lifecycle, because it is:
#:
#: * The pre-submit band (``draft`` → ``preparing`` → ``ready`` → ``submitting``) is where
#:   documents are generated and nothing has been sent.
#: * ``submitting`` is the only door into the post-submit band, and once through it there
#:   is **no edge back**. That is golden rule #1 expressed as data rather than as an ``if``.
#: * ``needs_review`` is reachable from every pre-submit state (golden rule #2: stopping to
#:   ask is always legal) and leaves only to ``ready``/``preparing`` — a human's answers
#:   re-enter the normal flow, they do not skip straight to a submission.
#: * ``failed`` and ``abandoned`` are terminal for the *automation*, but an operator may
#:   retry (``POST /applications/{id}/retry``) or revive a dismissal. Both are pre-submit
#:   states, so neither edge can produce a second submission.
#: * The outcome states model reality: a ``ghosted`` employer sometimes writes back, and an
#:   ``interview`` ends in an offer or a rejection. ``offer`` is the end of the funnel.
ALLOWED_TRANSITIONS: Final[dict[ApplicationStatus, set[ApplicationStatus]]] = {
    ApplicationStatus.DRAFT: {
        ApplicationStatus.PREPARING,
        ApplicationStatus.READY,
        ApplicationStatus.NEEDS_REVIEW,
        ApplicationStatus.FAILED,
        ApplicationStatus.ABANDONED,
    },
    ApplicationStatus.PREPARING: {
        ApplicationStatus.READY,
        ApplicationStatus.NEEDS_REVIEW,
        ApplicationStatus.FAILED,
        ApplicationStatus.ABANDONED,
    },
    ApplicationStatus.READY: {
        ApplicationStatus.PREPARING,
        ApplicationStatus.SUBMITTING,
        ApplicationStatus.NEEDS_REVIEW,
        ApplicationStatus.FAILED,
        ApplicationStatus.ABANDONED,
    },
    ApplicationStatus.SUBMITTING: {
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.NEEDS_REVIEW,
        ApplicationStatus.FAILED,
        ApplicationStatus.ABANDONED,
    },
    # ---- the point of no return ----------------------------------------------------
    ApplicationStatus.SUBMITTED: {
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.OFFER,
        ApplicationStatus.GHOSTED,
    },
    ApplicationStatus.CONFIRMED: {
        ApplicationStatus.REJECTED,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.OFFER,
        ApplicationStatus.GHOSTED,
    },
    # ---- off-path --------------------------------------------------------------------
    ApplicationStatus.NEEDS_REVIEW: {
        ApplicationStatus.PREPARING,
        ApplicationStatus.READY,
        ApplicationStatus.FAILED,
        ApplicationStatus.ABANDONED,
    },
    ApplicationStatus.FAILED: {
        ApplicationStatus.PREPARING,
        ApplicationStatus.READY,
        ApplicationStatus.ABANDONED,
    },
    ApplicationStatus.ABANDONED: {
        ApplicationStatus.DRAFT,
    },
    # ---- outcomes ---------------------------------------------------------------------
    ApplicationStatus.REJECTED: {
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.OFFER,
    },
    ApplicationStatus.INTERVIEW: {
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
        ApplicationStatus.GHOSTED,
    },
    ApplicationStatus.OFFER: set(),
    ApplicationStatus.GHOSTED: {
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
    },
}


class InvalidTransition(ValueError):
    """A status change the state machine forbids.

    Derives from :class:`ValueError` so ``app.api.errors`` maps it to ``400`` with no
    special case: asking to move an application from ``submitted`` back to ``ready`` is a
    malformed request, not a server fault.

    Attributes:
        application_id: The application the move was attempted on, when known.
        current: The status the application is actually in.
        target: The status that was asked for.
    """

    def __init__(
        self,
        current: ApplicationStatus,
        target: ApplicationStatus,
        *,
        application_id: uuid.UUID | None = None,
    ) -> None:
        """Record both ends of the refused move.

        Args:
            current: The application's present status.
            target: The status that was requested.
            application_id: The application involved, when it has an id.
        """
        allowed = sorted(status.value for status in ALLOWED_TRANSITIONS.get(current, set()))
        super().__init__(
            f"cannot move application from {current.value!r} to {target.value!r}"
            f" (allowed from {current.value!r}: {', '.join(allowed) or 'nothing'})"
        )
        self.application_id = application_id
        self.current = current
        self.target = target


def _resolve_provider(application: Application, session: AsyncSession) -> str | None:
    """Return the ATS provider behind *application*, without ever emitting a query.

    ``applicantos_applications_total`` is labelled by provider, and the provider lives on
    the posting. Two hazards make this more than ``application.posting.provider``:

    * ``posting`` is a ``lazy="selectin"`` relationship, so it is normally populated — but
      an application this session just *inserted* has never been through a load, and reading
      an unloaded relationship inside a coroutine raises ``MissingGreenlet``. A telemetry
      label must never be able to fail a transition.
    * Reporting ``unknown`` instead is safe but not free: the Grafana panel filters on
      ``provider=~"$provider"``, so an unlabelled sample drops out of the chart entirely.

    So it tries the loaded relationship, then the session's identity map — where
    :meth:`Pipeline.prepare`'s own ``JobPosting`` almost always already sits, having been
    loaded a few lines earlier — and only then gives up. Both lookups are pure memory.

    Args:
        application: The row being transitioned.
        session: The unit of work, consulted only for its identity map.

    Returns:
        The provider's value, or ``None`` when it cannot be read without I/O.
    """
    try:
        if "posting" not in sa_inspect(application).unloaded:
            posting = application.posting
            if posting is not None:
                return str(posting.provider)

        posting_id = application.posting_id
        if posting_id is None:
            return None
        key = sa_inspect(JobPosting).identity_key_from_primary_key((posting_id,))
        known = session.identity_map.get(key)
        if known is None or "provider" in sa_inspect(known).unloaded:
            return None
        return str(known.provider)
    except Exception as exc:  # pragma: no cover - defensive; inspection does not raise
        logger.debug("application.provider_label_unavailable", error=str(exc))
        return None


class ApplicationService:
    """Owns the application row: its identity, its status, and its counters.

    Args:
        session: The unit of work. Unlike the other services this one **commits**, for the
            reason given in the module docstring.

    Usage::

        service = ApplicationService(session)
        application, created = await service.create_or_get(user.id, posting.id)
        await service.transition(application, ApplicationStatus.PREPARING)
    """

    #: The state machine, exposed on the class so a caller can ask "what can I do next?"
    #: without importing the module-level constant.
    ALLOWED_TRANSITIONS: Final[dict[ApplicationStatus, set[ApplicationStatus]]] = (
        ALLOWED_TRANSITIONS
    )

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to one session."""
        self._session = session

    # ----------------------------------------------------------------------------------
    # Identity
    # ----------------------------------------------------------------------------------

    async def get(self, application_id: uuid.UUID | str) -> Application:
        """Load one application by id.

        Args:
            application_id: The application's identifier, as a UUID or its string form.

        Returns:
            The row, with its eager relationships loaded.

        Raises:
            LookupError: If the identifier is malformed or names no application.
        """
        identifier = _as_uuid(application_id, "application id")
        application = await self._session.scalar(
            select(Application).where(Application.id == identifier).limit(1)
        )
        if application is None:
            raise LookupError(f"application {identifier} not found")
        return application

    async def create_or_get(
        self,
        user_id: uuid.UUID | str,
        posting_id: uuid.UUID | str,
        *,
        session_id: uuid.UUID | str | None = None,
    ) -> tuple[Application, bool]:
        """Return this user's application to this posting, creating it if there is none.

        ``UNIQUE(user_id, posting_id)`` is golden rule #1's first half, so this method
        never tries to be clever about it: it selects, and if the select missed it inserts
        inside a **SAVEPOINT**. A concurrent worker that wins the race raises
        :class:`~sqlalchemy.exc.IntegrityError` inside that savepoint, which rolls back
        alone and leaves the outer transaction usable; the winner's row is then re-selected
        and returned with ``created=False``. Without the savepoint the loser's whole
        transaction would be poisoned, and a batch run would lose every application after
        the first collision.

        ``company_id`` is denormalised onto the row from the posting so analytics can group
        by employer without a join.

        ``session_id`` is what attributes the application to the run that produced it. It is
        set on insert **and** back-filled on an existing row that has none, because an
        application can legitimately be created outside a run — by ``POST /applications`` or
        by a review being resolved — and later be picked up by one. It is never *moved*: an
        application already credited to a run stays credited to that run, so re-running over
        an old posting cannot re-attribute yesterday's work to today's session.

        Args:
            user_id: The applicant.
            posting_id: The posting being applied to.
            session_id: The run this application belongs to, when there is one. Without it
                the row is orphaned: every per-run counter, cap and stop condition is
                computed by ``WHERE session_id = ...``, and a NULL matches none of them.

        Returns:
            ``(application, created)`` — the row and whether this call inserted it.

        Raises:
            LookupError: If either identifier is malformed, or the posting does not exist.
            IntegrityError: If the insert loses a race *and* the winning row cannot then be
                found. That combination is not the conflict this method understands, and
                swallowing it would silently drop an application.
        """
        user_uuid = _as_uuid(user_id, "user id")
        posting_uuid = _as_uuid(posting_id, "posting id")
        session_uuid = _as_uuid(session_id, "session id") if session_id is not None else None

        existing = await self._find(user_uuid, posting_uuid)
        if existing is not None:
            if session_uuid is not None and existing.session_id is None:
                existing.session_id = session_uuid
                await self._session.flush()
                await self._session.commit()
                logger.info(
                    "application.session_attributed",
                    application_id=str(existing.id),
                    session_id=str(session_uuid),
                )
            return existing, False

        posting = await self._session.scalar(
            select(JobPosting).where(JobPosting.id == posting_uuid).limit(1)
        )
        if posting is None:
            raise LookupError(f"posting {posting_uuid} not found")

        application = Application(
            user_id=user_uuid,
            posting_id=posting_uuid,
            company_id=posting.company_id,
            session_id=session_uuid,
            status=ApplicationStatus.DRAFT,
        )
        application.add_event(
            "created",
            message="Application row created.",
            payload={"posting_id": str(posting_uuid)},
        )
        try:
            async with self._session.begin_nested():
                self._session.add(application)
                await self._session.flush()
        except IntegrityError:
            self._detach(application)
            logger.info(
                "application.create_raced",
                user_id=str(user_uuid),
                posting_id=str(posting_uuid),
            )
            raced = await self._find(user_uuid, posting_uuid)
            if raced is None:
                raise
            return raced, False

        await self._session.commit()
        logger.info(
            "application.created",
            application_id=str(application.id),
            user_id=str(user_uuid),
            posting_id=str(posting_uuid),
            session_id=str(session_uuid) if session_uuid is not None else None,
        )
        return application, True

    async def _find(self, user_id: uuid.UUID, posting_id: uuid.UUID) -> Application | None:
        """Select the one application a ``(user, posting)`` pair may have.

        Args:
            user_id: The applicant.
            posting_id: The posting.

        Returns:
            The row, or ``None``.
        """
        return await self._session.scalar(
            select(Application)
            .where(Application.user_id == user_id, Application.posting_id == posting_id)
            .limit(1)
        )

    def _detach(self, instance: object) -> None:
        """Remove a failed insert from the session so a later flush does not retry it.

        Args:
            instance: The object whose insert lost a race.
        """
        try:
            self._session.expunge(instance)
        except InvalidRequestError:
            logger.debug("application.already_detached")

    # ----------------------------------------------------------------------------------
    # The state machine
    # ----------------------------------------------------------------------------------

    @staticmethod
    def can_transition(current: ApplicationStatus, target: ApplicationStatus) -> bool:
        """Return whether *target* may follow *current*.

        A move to the status the application is already in is always permitted: re-asserting
        ``needs_review`` with a fresh reason, or re-confirming a submission a poller just
        re-verified, is a real operation and must not be an error.

        Args:
            current: The application's present status.
            target: The status being considered.

        Returns:
            Whether :meth:`transition` would accept the move.
        """
        if current is target:
            return True
        return target in ALLOWED_TRANSITIONS.get(current, set())

    async def claim(
        self,
        application: Application,
        *,
        expected: ApplicationStatus,
        target: ApplicationStatus,
        message: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        """Atomically move *application* from *expected* to *target*, or refuse.

        The difference from :meth:`transition` is one ``WHERE`` clause, and it is the
        difference between golden rule #1 holding and merely appearing to.
        :meth:`transition` sets an attribute on an object that was loaded earlier; the guards
        in :meth:`~app.services.pipeline.Pipeline.submit` read that same in-memory status.
        Between the read and the write sit a résumé render and a PDF — seconds — so two
        executors that both read ``READY`` both proceed, both stamp ``SUBMITTING``, and both
        drive a browser to the employer. The status ladder closes every *sequential*
        double-run and none of the concurrent ones.

        This issues ``UPDATE ... WHERE id = :id AND status = :expected`` and treats the
        affected row count as the claim: exactly one caller can see 1, whatever else is
        racing it and whichever process it lives in. The loser is told to stand down, which
        the caller reports as "already applied" rather than as a failure.

        Args:
            application: The row to claim. Must belong to this service's session.
            expected: The status the row must still be in for the claim to succeed.
            target: The status to move it to.
            message: Human-readable one-liner for the desktop timeline.
            payload: Structured context for the event.

        Returns:
            ``True`` when this caller won the claim and the move is committed. ``False``
            when the row had already moved on — nothing was written.

        Raises:
            InvalidTransition: If ``expected -> target`` is not an allowed move.
        """
        from sqlalchemy import update

        if not self.can_transition(expected, target):
            raise InvalidTransition(expected, target, application_id=application.id)

        # Read before the UPDATE: on the losing path the row is untouched, but keeping the
        # id in a local means the failure can always be logged without touching the ORM.
        identifier = str(application.id)

        stamp = utcnow()
        values: dict[str, Any] = {"status": target, "review_reason": None}
        if target in APPLICATION_POST_SUBMIT_STATES:
            # Matches `transition`: stamped on first entry to a post-submit state, which is
            # what makes `daily_count` and therefore the daily cap trustworthy.
            values["submitted_at"] = stamp

        result: CursorResult[Any] = await self._session.execute(  # type: ignore[assignment]
            update(Application)
            .where(Application.id == application.id, Application.status == expected)
            .values(**values)
        )
        if result.rowcount != 1:
            # No rollback. The UPDATE matched nothing, so there is nothing of this claim to
            # undo — and rolling back would discard whatever else the caller's session was
            # holding, then expire every attribute on `application`, so that even reading its
            # id to log the loss becomes a lazy load from synchronous code.
            logger.info(
                "application.claim_lost",
                application_id=identifier,
                expected=expected.value,
                target=target.value,
            )
            return False

        # The UPDATE bypassed the ORM, so the in-memory row is stale. `set_committed_value`
        # rather than plain assignment or `refresh`: assignment would mark the attributes
        # dirty and have the next flush write them a second time, and `refresh` expires the
        # *whole* object — including the loaded `events` collection, which `add_event` then
        # lazy-loads from a synchronous attribute access and raises `MissingGreenlet`.
        # This records the values as already-persisted, which is exactly what they are.
        set_committed_value(application, "status", target)
        set_committed_value(application, "review_reason", None)
        if "submitted_at" in values:
            set_committed_value(application, "submitted_at", values["submitted_at"])

        event_payload = dict(payload or {})
        event_payload["from"] = expected.value
        event_payload["to"] = target.value
        application.add_event(target.value, message=message, payload=event_payload)

        await self._session.flush()
        await self._session.commit()

        record_application(target, _resolve_provider(application, self._session))
        logger.info(
            "application.claimed",
            application_id=identifier,
            user_id=str(application.user_id),
            expected=expected.value,
            target=target.value,
        )
        return True

    async def transition(
        self,
        application: Application,
        status: ApplicationStatus,
        *,
        reason: ReviewReason | None = None,
        message: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Application:
        """Move *application* to *status*, recording why, and commit.

        The single sanctioned mutation point for ``applications.status``. Besides the guard
        itself it maintains three invariants that would otherwise be re-derived (and
        eventually got wrong) at every call site:

        * ``review_reason`` tracks the status. Entering ``needs_review`` stores *reason*;
          leaving it clears the reason, so :attr:`Application.needs_review` and
          ``review_reason`` can never disagree. ``review_payload`` is *kept* on the way out
          — it is evidence of what the reviewer was shown.
        * ``submitted_at`` is stamped the first time the application enters a post-submit
          status, which is what makes :meth:`daily_count` — and therefore the daily rate
          limit — trustworthy no matter which code path did the submitting.
        * An :class:`~app.models.application.ApplicationEvent` is appended, always.

        Being the one chokepoint every status change passes through also makes it the only
        honest producer of ``applicantos_applications_total{status,provider}``
        (``docs/CONTRACTS.md`` §16). It is incremented **after** the commit, so the counter
        only ever reflects state that is durable.

        Args:
            application: The row to move. Must belong to this service's session.
            status: The status to move to.
            reason: Why review is needed. Only meaningful when *status* is
                ``needs_review``; recorded on the event either way.
            message: Human-readable one-liner for the desktop timeline.
            payload: Structured context for the event. When *status* is ``needs_review``
                this also becomes ``review_payload``, because it is exactly what the review
                screen has to render.

        Returns:
            The same application, committed.

        Raises:
            InvalidTransition: If the move is not in :data:`ALLOWED_TRANSITIONS`.
        """
        target = ApplicationStatus(status)
        current = application.status or ApplicationStatus.DRAFT

        if not self.can_transition(current, target):
            logger.warning(
                "application.invalid_transition",
                application_id=str(application.id),
                current=current.value,
                target=target.value,
            )
            raise InvalidTransition(current, target, application_id=application.id)

        context: dict[str, Any] = dict(payload or {})

        application.status = target
        if target is ApplicationStatus.NEEDS_REVIEW:
            application.review_reason = reason
            # Reassigned wholesale: JSON columns are not change tracked.
            application.review_payload = dict(context)
        else:
            application.review_reason = None

        if target in APPLICATION_POST_SUBMIT_STATES and application.submitted_at is None:
            application.submitted_at = utcnow()

        event_payload = dict(context)
        event_payload["from"] = current.value
        event_payload["to"] = target.value
        if reason is not None:
            event_payload["review_reason"] = reason.value

        application.add_event(
            target.value if target is not current else _EVENT_KIND_STATUS,
            message=message,
            payload=event_payload,
        )

        await self._session.flush()
        await self._session.commit()

        record_application(target, _resolve_provider(application, self._session))

        logger.info(
            "application.transitioned",
            application_id=str(application.id),
            user_id=str(application.user_id),
            **{"from": current.value, "to": target.value},
            review_reason=reason.value if reason is not None else None,
        )
        return application

    async def mark_needs_review(
        self,
        application: Application,
        reason: ReviewReason,
        payload: Mapping[str, Any] | None = None,
    ) -> Application:
        """Park *application* in the human review queue.

        Golden rule #2's write path. Every ``ReviewReason`` exists because guessing would
        have been worse than asking, so this is a *normal* outcome — not an error, not a
        failure, and never retried automatically.

        Args:
            application: The row to park.
            reason: Why the automation stopped.
            payload: Everything the reviewer needs to decide: unanswered fields, candidate
                answers, the posting's apply URL. Stored on ``review_payload``.

        Returns:
            The same application, committed.

        Raises:
            InvalidTransition: If ``needs_review`` cannot follow the present status — which
                is the case for every post-submit state. An application the employer already
                has is not something a human can un-send.
        """
        return await self.transition(
            application,
            ApplicationStatus.NEEDS_REVIEW,
            reason=reason,
            message=f"Escalated to manual review: {reason.value}.",
            payload=payload,
        )

    async def record_error(
        self,
        application: Application,
        exc: BaseException | str,
    ) -> Application:
        """Record a failed attempt without changing the application's status.

        Increments ``attempt_count`` and stores the message on ``last_error``, then commits.
        The status change is deliberately *not* made here: whether a failure means ``failed``
        (give up) or ``needs_review`` (ask a human) is a policy decision that belongs to
        :class:`app.services.pipeline.Pipeline`, and a method that silently decided it would
        make the two indistinguishable in the audit trail.

        Args:
            application: The row that failed.
            exc: The exception, or a pre-formatted message.

        Returns:
            The same application, committed.
        """
        detail = _error_text(exc)
        application.attempt_count = int(application.attempt_count or 0) + 1
        application.last_error = detail

        application.add_event(
            _EVENT_KIND_ERROR,
            message=detail[:200],
            payload={
                "attempt": application.attempt_count,
                "error_type": type(exc).__name__ if isinstance(exc, BaseException) else "str",
            },
        )
        await self._session.flush()
        await self._session.commit()

        logger.warning(
            "application.error_recorded",
            application_id=str(application.id),
            attempt=application.attempt_count,
            status=application.status.value,
            error=detail[:500],
        )
        return application

    # ----------------------------------------------------------------------------------
    # Counters
    # ----------------------------------------------------------------------------------

    async def daily_count(self, user_id: uuid.UUID | str) -> int:
        """Return how many applications this user has had submitted today (UTC).

        Counted on ``submitted_at``, which :meth:`transition` stamps on the way into any
        post-submit status, rather than on ``created_at``: preparing fifty resumes costs the
        user nothing, sending fifty applications is what the cap in
        ``settings.max_applications_per_day`` exists to bound.

        The window is the UTC calendar day. A local-timezone window would make the cap
        depend on where the laptop is, and a rolling 24-hour window would make "how many are
        left today?" un-answerable in the UI.

        Args:
            user_id: The applicant.

        Returns:
            The count, ``0`` when nothing has been submitted today.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        start = _start_of_utc_day()
        total = await self._session.scalar(
            select(func.count())
            .select_from(Application)
            .where(
                Application.user_id == identifier,
                Application.submitted_at.is_not(None),
                Application.submitted_at >= start,
            )
        )
        return int(total or 0)

    async def stats(self, user_id: uuid.UUID | str) -> dict[str, Any]:
        """Return this user's application funnel as a JSON-ready mapping.

        Args:
            user_id: The applicant.

        Returns:
            ``by_status`` (every status with a non-zero count, keyed by its wire value) plus
            the derived figures the dashboard shows: ``total``, ``submitted`` (everything
            the employer holds), ``confirmed``, ``needs_review``, ``failed``, ``interviews``,
            ``offers``, ``rejected``, ``active``, ``today``, and the two rates. Rates are
            ``0.0`` rather than ``None`` when nothing has been submitted, so a fresh install
            renders a chart instead of an error.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        rows = await self._session.execute(
            select(Application.status, func.count())
            .where(Application.user_id == identifier)
            .group_by(Application.status)
        )
        by_status: dict[str, int] = {}
        counts: dict[ApplicationStatus, int] = {}
        for status, count in rows.all():
            resolved = ApplicationStatus(status)
            counts[resolved] = int(count or 0)
            by_status[resolved.value] = int(count or 0)

        total = sum(counts.values())
        submitted = sum(counts.get(state, 0) for state in APPLICATION_POST_SUBMIT_STATES)
        interviews = counts.get(ApplicationStatus.INTERVIEW, 0)
        offers = counts.get(ApplicationStatus.OFFER, 0)
        rejected = counts.get(ApplicationStatus.REJECTED, 0)
        responded = interviews + offers + rejected

        return {
            "total": total,
            "by_status": by_status,
            "submitted": submitted,
            "confirmed": counts.get(ApplicationStatus.CONFIRMED, 0),
            "needs_review": counts.get(ApplicationStatus.NEEDS_REVIEW, 0),
            "failed": counts.get(ApplicationStatus.FAILED, 0),
            "abandoned": counts.get(ApplicationStatus.ABANDONED, 0),
            "interviews": interviews,
            "offers": offers,
            "rejected": rejected,
            "active": sum(count for status, count in counts.items() if status.is_active()),
            "today": await self.daily_count(identifier),
            "response_rate": _ratio(responded, submitted),
            "interview_rate": _ratio(interviews + offers, submitted),
        }

    async def timeseries(
        self,
        user_id: uuid.UUID | str,
        days: int = DEFAULT_TIMESERIES_DAYS,
    ) -> list[dict[str, Any]]:
        """Return a day-by-day activity series, oldest first.

        Bucketing happens in Python rather than in SQL on purpose. ``date_trunc`` is
        PostgreSQL-only and ``strftime`` is SQLite-only, so a dialect-specific query would
        make the dashboard work on one backend and 500 on the other; the row count here is
        bounded by :data:`MAX_TIMESERIES_DAYS` of one user's applications, which is small.

        Every day in the window is present, including days with no activity — a chart with
        gaps silently rescales its x-axis and lies about the shape of a run.

        Args:
            user_id: The applicant.
            days: Width of the window, clamped to ``1..``:data:`MAX_TIMESERIES_DAYS`.

        Returns:
            One mapping per day: ``date`` (``YYYY-MM-DD``, UTC), ``created``, ``submitted``,
            ``confirmed``, ``needs_review`` and ``failed``.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        window = max(1, min(int(days or DEFAULT_TIMESERIES_DAYS), MAX_TIMESERIES_DAYS))
        start = _start_of_utc_day() - timedelta(days=window - 1)

        rows = await self._session.execute(
            select(
                Application.created_at,
                Application.submitted_at,
                Application.status,
                Application.updated_at,
            ).where(
                Application.user_id == identifier,
                or_(
                    Application.created_at >= start,
                    Application.submitted_at >= start,
                ),
            )
        )

        buckets: dict[str, dict[str, Any]] = {}
        for offset in range(window):
            day = (start + timedelta(days=offset)).date().isoformat()
            buckets[day] = {
                "date": day,
                "created": 0,
                "submitted": 0,
                "confirmed": 0,
                "needs_review": 0,
                "failed": 0,
            }

        for created_at, submitted_at, status, updated_at in rows.all():
            resolved = ApplicationStatus(status)
            created_key = _day_key(created_at)
            if created_key in buckets:
                buckets[created_key]["created"] += 1

            submitted_key = _day_key(submitted_at)
            if submitted_key is not None and submitted_key in buckets:
                buckets[submitted_key]["submitted"] += 1
                if resolved is ApplicationStatus.CONFIRMED:
                    buckets[submitted_key]["confirmed"] += 1

            if resolved in {ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.FAILED}:
                # An off-path application is counted on the day it *became* off-path, which
                # `updated_at` approximates and `created_at` backstops for a row that has
                # never been updated since insertion.
                key = _day_key(updated_at) or created_key
                if key in buckets:
                    buckets[key][resolved.value] += 1

        return [buckets[key] for key in sorted(buckets)]


# ======================================================================================
# Helpers
# ======================================================================================


def _as_uuid(value: uuid.UUID | str, label: str) -> uuid.UUID:
    """Coerce an identifier to a :class:`~uuid.UUID`.

    Args:
        value: The identifier, already a UUID or its string form.
        label: What the identifier names, used in the error message.

    Returns:
        The parsed UUID.

    Raises:
        LookupError: If the value is not a well-formed UUID. Malformed and missing are the
            same outcome for a caller — ``404`` — so they raise the same class.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise LookupError(f"{value!r} is not a valid {label}") from exc


def _error_text(exc: BaseException | str) -> str:
    """Render an exception as the message stored on ``last_error``.

    Args:
        exc: The exception, or an already-formatted message.

    Returns:
        ``"TypeName: message"`` for an exception, the string itself otherwise, truncated to
        :data:`LAST_ERROR_MAX_CHARS`.
    """
    if isinstance(exc, BaseException):
        detail = str(exc).strip() or exc.__class__.__name__
        text = f"{type(exc).__name__}: {detail}"
    else:
        text = str(exc).strip()
    return text[:LAST_ERROR_MAX_CHARS]


def _start_of_utc_day(now: datetime | None = None) -> datetime:
    """Return midnight UTC of the day *now* falls in.

    Args:
        now: The instant to bucket. Defaults to the current UTC time.

    Returns:
        An aware UTC datetime at ``00:00:00``.
    """
    moment = now if now is not None else utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _day_key(value: datetime | None) -> str | None:
    """Return the ``YYYY-MM-DD`` UTC bucket a timestamp belongs to.

    Args:
        value: The timestamp, or ``None``.

    Returns:
        The ISO date string, or ``None`` when *value* is ``None``. A naive value is read as
        UTC, which is what :class:`~app.database.types.UTCDateTime` guarantees it is.
    """
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date().isoformat()


def _ratio(numerator: int, denominator: int) -> float:
    """Return ``numerator / denominator`` rounded to four places, or ``0.0``.

    Args:
        numerator: The top of the fraction.
        denominator: The bottom; ``0`` yields ``0.0`` rather than raising.

    Returns:
        The ratio, never ``None`` — a dashboard renders a number, not a null.
    """
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)
