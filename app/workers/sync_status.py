"""Status-sync tasks — ``sync.*`` on the ``maintenance`` queue (``§17.7``).

These four tasks are what make the promise in §17 true without anybody opening the app: a
Microsoft rejection updates itself because ``sync.poll_all`` ran at 03:30 and the mailbox had
it. Everything they do is delegated to
:class:`~app.tracking.service.StatusSyncService`; this module is scheduling, fan-out,
reporting and events.

Why fan-out rather than one long task
-------------------------------------

``sync.poll_all`` enqueues one ``sync.poll_account`` per mailbox instead of polling them in a
loop. Three consequences, all of them the point: a user with a slow IMAP server does not
delay their Gmail account, a failure is isolated to the mailbox that caused it, and each
child fits comfortably inside the worker's time limit no matter how many accounts exist.

Idempotency
-----------

Every task here is safe to run twice, which is what ``task_acks_late=True`` requires.
``UNIQUE(user_id, source, external_ref)`` means a redelivered ``sync.poll_account`` inserts
nothing it inserted before; ``detect_ghosted`` is a no-op on an application already
``ghosted`` because the state machine's same-status move is permitted and the query no longer
selects it. Nothing here retries a ``needs_review`` outcome — a signal waiting for a human is
a *result*, not a failure (golden rule #2).

The switch
----------

``settings.status_sync_enabled`` gates all of it, and
:func:`on_launch` additionally honours ``settings.status_sync_on_launch``. A user who has
connected no mailbox reads nothing, is polled for nothing, and costs nothing — §17.8.6 is
enforced by the absence of an ``email_accounts`` row, not by a flag.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select

from app.api.tasks import (
    TASK_SYNC_DETECT_GHOSTED,
    TASK_SYNC_ON_LAUNCH,
    TASK_SYNC_POLL_ACCOUNT,
    TASK_SYNC_POLL_ALL,
)
from app.config.settings import get_settings
from app.database.session import session_scope
from app.workers import run_async, task_span
from app.workers.celery_app import celery_app, enqueue
from app.workers.retry import retryable

__all__ = [
    "detect_ghosted",
    "on_launch",
    "poll_account",
    "poll_all",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Helpers
# ======================================================================================


def _as_uuid(value: str | uuid.UUID | None, label: str) -> uuid.UUID | None:
    """Coerce a task argument to a UUID.

    Task arguments cross a process boundary as JSON, so an id arrives as a string. A
    malformed one is logged and treated as absent rather than raising: a scheduled sweep
    must not crash-loop on one bad message.

    Args:
        value: The identifier, or ``None``.
        label: What it names, for the log line.

    Returns:
        The parsed UUID, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        logger.warning("sync.malformed_id", label=label, value=str(value)[:64])
        return None


def _as_datetime(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 task argument into a datetime.

    Args:
        value: An ISO-8601 string, a datetime, or ``None``.

    Returns:
        The parsed datetime, or ``None`` when absent or unparseable. Unparseable degrades to
        "use the configured window", which is the safe direction — the adapters clamp it
        anyway.
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        logger.warning("sync.malformed_since", value=str(value)[:64])
        return None


async def _user_ids_with_mailboxes(explicit: uuid.UUID | None) -> list[uuid.UUID]:
    """Return the users a sweep should cover.

    Args:
        explicit: One user, or ``None`` for everybody who has connected a mailbox.

    Returns:
        The user ids. Empty is the normal state of a fresh install and is not an error.
    """
    if explicit is not None:
        return [explicit]

    from app.models.tracking import EmailAccount

    async with session_scope() as session:
        rows = await session.execute(
            select(EmailAccount.user_id)
            .where(
                EmailAccount.enabled.is_(True),
                EmailAccount.credential_ref.is_not(None),
            )
            .distinct()
        )
        return [value for value in rows.scalars().all() if value is not None]


# ======================================================================================
# sync.poll_all
# ======================================================================================


@celery_app.task(name=TASK_SYNC_POLL_ALL, bind=False)
@retryable()
def poll_all(user_id: str | None = None, since: str | None = None) -> dict[str, Any]:
    """Fan out one ``sync.poll_account`` per connected mailbox.

    The beat entry, running every ``settings.status_sync_interval_minutes``. It reads no
    mail itself: it finds the accounts and enqueues one child per account, so a slow mailbox
    cannot delay a fast one and a failure stays where it happened.

    Args:
        user_id: Sync only this user. ``None`` covers everybody with a connected mailbox,
            which is what the schedule passes.
        since: ISO-8601 override for the incremental window, forwarded to each child and
            clamped there to ``settings.status_sync_lookback_days``.

    Returns:
        ``{"enabled", "users", "accounts", "queued", "account_ids"}``. ``enabled`` is
        ``False`` — and every other count zero — when ``status_sync_enabled`` is off.
    """
    with task_span(TASK_SYNC_POLL_ALL) as log:
        if not get_settings().status_sync_enabled:
            log.info("sync.disabled")
            return {"enabled": False, "users": 0, "accounts": 0, "queued": 0, "account_ids": []}

        async def _run() -> dict[str, Any]:
            """Collect every pollable account across the selected users.

            Queried directly rather than through the tracker plugin: this task schedules,
            it does not poll, and "which rows exist?" is a question about the model rather
            than about a mailbox.
            """
            from app.models.tracking import EmailAccount

            users = await _user_ids_with_mailboxes(_as_uuid(user_id, "user id"))
            identifiers: list[str] = []
            async with session_scope() as session:
                for identifier in users:
                    rows = await session.execute(
                        select(EmailAccount.id)
                        .where(
                            EmailAccount.user_id == identifier,
                            EmailAccount.enabled.is_(True),
                            EmailAccount.credential_ref.is_not(None),
                        )
                        .order_by(EmailAccount.created_at.asc())
                    )
                    identifiers.extend(str(value) for value in rows.scalars().all())
            return {"users": len(users), "account_ids": identifiers}

        outcome = run_async(_run())
        account_ids: list[str] = outcome["account_ids"]

        queued = 0
        for account_id in account_ids:
            if enqueue(TASK_SYNC_POLL_ACCOUNT, account_id, since) is not None:
                queued += 1

        result = {
            "enabled": True,
            "users": outcome["users"],
            "accounts": len(account_ids),
            "queued": queued,
            "account_ids": account_ids,
        }
        log.info(
            "sync.poll_all_finished",
            users=result["users"],
            accounts=result["accounts"],
            queued=queued,
        )
        return result


# ======================================================================================
# sync.poll_account
# ======================================================================================


@celery_app.task(name=TASK_SYNC_POLL_ACCOUNT, bind=False)
@retryable()
def poll_account(account_id: str, since: str | None = None) -> dict[str, Any]:
    """Read one mailbox, classify what it holds, and apply what is certain.

    Idempotent by construction: ``UNIQUE(user_id, source, external_ref)`` means a redelivered
    message re-reads the same mail and inserts nothing, so ``task_acks_late`` redelivery is
    safe. The account's cursor advances only for a run that completed, so a crash resumes
    rather than restarts (golden rule #8).

    Args:
        account_id: The ``email_accounts`` row to poll.
        since: ISO-8601 override for the incremental window.

    Returns:
        The serialised :class:`~app.tracking.base.SyncReport`, plus ``"found": False`` when
        the account no longer exists — a mailbox disconnected between fan-out and execution
        is an ordinary race, not an error.
    """
    identifier = _as_uuid(account_id, "account id")

    with task_span(TASK_SYNC_POLL_ACCOUNT, account_id=str(account_id)) as log:
        if identifier is None:
            return {"found": False, "reason": "malformed account id"}
        if not get_settings().status_sync_enabled:
            log.info("sync.disabled")
            return {"found": False, "reason": "status sync is disabled"}

        async def _run() -> dict[str, Any]:
            """Sync the one account, tolerating its disappearance."""
            from app.tracking.service import StatusSyncService

            async with session_scope() as session:
                service = StatusSyncService(session, get_settings())
                try:
                    report = await service.sync_account(identifier, since=_as_datetime(since))
                except LookupError:
                    return {"found": False, "reason": "account no longer exists"}
                return {"found": True, **report.as_dict()}

        outcome = run_async(_run())
        if outcome.get("found"):
            log.info(
                "sync.poll_account_finished",
                fetched=outcome.get("fetched", 0),
                classified=outcome.get("classified", 0),
                matched=outcome.get("matched", 0),
                applied=outcome.get("applied", 0),
                needs_review=outcome.get("needs_review", 0),
            )
        return outcome


# ======================================================================================
# sync.detect_ghosted
# ======================================================================================


@celery_app.task(name=TASK_SYNC_DETECT_GHOSTED, bind=False)
@retryable()
def detect_ghosted(user_id: str | None = None) -> dict[str, Any]:
    """Mark long-silent applications ``ghosted``, daily.

    This is the outcome nobody types in. Most applications end in silence, and until silence
    is recorded the funnel shows a hundred applications "still pending" forever and the
    response rate is unknowable — which makes "which of my resumes are not landing?"
    unanswerable, and that question is the product.

    Args:
        user_id: Sweep only this user. ``None`` covers everybody with a connected mailbox,
            which is what the daily schedule passes.

    Returns:
        ``{"users", "ghosted", "days"}``.
    """
    with task_span(TASK_SYNC_DETECT_GHOSTED) as log:
        settings = get_settings()

        async def _run() -> dict[str, Any]:
            """Run the sweep for each selected user in its own unit of work."""
            from app.tracking.service import StatusSyncService

            users = await _user_ids_with_mailboxes(_as_uuid(user_id, "user id"))
            total = 0
            for identifier in users:
                async with session_scope() as session:
                    total += await StatusSyncService(session, settings).detect_ghosted(identifier)
            return {"users": len(users), "ghosted": total}

        outcome = run_async(_run())
        outcome["days"] = int(settings.ghosted_after_days)
        if outcome["ghosted"]:
            log.info(
                "sync.ghosted",
                ghosted=outcome["ghosted"],
                users=outcome["users"],
                days=outcome["days"],
            )
        return outcome


# ======================================================================================
# sync.on_launch
# ======================================================================================


@celery_app.task(name=TASK_SYNC_ON_LAUNCH, bind=False)
@retryable()
def on_launch(user_id: str | None = None) -> dict[str, Any]:
    """Sync everything once, because the desktop app just started.

    Fired by the Tauri shell on startup. A person opening the app expects it to already know
    what arrived while it was closed, and waiting up to
    ``settings.status_sync_interval_minutes`` for the next scheduled tick would make the
    first screen they see stale.

    Separate from :func:`poll_all` rather than an argument to it, because it answers to its
    own switch: an operator who wants background polling but no startup burst sets
    ``status_sync_on_launch=false`` and this task becomes a no-op while the schedule keeps
    running.

    Args:
        user_id: Sync only this user. ``None`` covers everybody with a connected mailbox.

    Returns:
        The result of the delegated :func:`poll_all`, plus ``"on_launch": True``. When the
        launch sync is switched off, ``{"on_launch": False, "enabled": False, ...}``.
    """
    with task_span(TASK_SYNC_ON_LAUNCH) as log:
        settings = get_settings()
        if not settings.status_sync_enabled or not settings.status_sync_on_launch:
            log.info(
                "sync.on_launch_skipped",
                status_sync_enabled=settings.status_sync_enabled,
                status_sync_on_launch=settings.status_sync_on_launch,
            )
            return {
                "on_launch": False,
                "enabled": bool(settings.status_sync_enabled),
                "users": 0,
                "accounts": 0,
                "queued": 0,
                "account_ids": [],
            }

        # Called directly rather than enqueued: this task is already on the maintenance
        # queue, and one more hop would only add latency to the thing whose whole purpose is
        # to be fast at startup.
        outcome = poll_all(user_id=user_id)
        return {**outcome, "on_launch": True}
