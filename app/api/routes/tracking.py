"""Application status sync — mailboxes, signals, and the review queue for them (§17.7).

The HTTP surface of the loop that closes itself. A user connects a mailbox once; from then
on their rejections, interview invitations and offers find their own way into the database,
and this group is how the desktop app connects, inspects, corrects and triggers that.

Two rules govern every response body here.

**No credential, and no key to one, ever leaves this process.**
:class:`~app.schemas.tracking.EmailAccountRead` has no ``credential_ref`` field — not
optional, not redacted, *absent* — because a reference is the exact lookup key for a secret
in the OS keychain and publishing it would hand a reader the one thing they need. The client
gets :attr:`~app.schemas.tracking.EmailAccountRead.connected`, a boolean derived from the ORM
model's own property, and that is the entire answer it needs. The write side accepts a
:class:`~pydantic.SecretStr` which goes straight to the keychain and is never persisted.

**No message body, ever.** ``StatusSignalRead.snippet`` is capped at 500 characters by the
column, by a model validator and by the schema, and there is no column anywhere holding a
full body to leak (§17.8.3).

Syncing is queued, never awaited. Reading a mailbox is network work measured in seconds to
minutes, and ``POST /tracking/sync`` returns ``202`` with a
:class:`~app.api.tasks.Dispatch` outcome exactly like every other work-triggering endpoint —
including when the broker is down, which is a degraded ``202`` and not a ``500``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, SettingsDep
from app.api.routes._support import require_owner
from app.api.tasks import (
    TASK_SYNC_DETECT_GHOSTED,
    TASK_SYNC_POLL_ACCOUNT,
    TASK_SYNC_POLL_ALL,
    dispatch,
)
from app.models.tracking import StatusSignal as StatusSignalRow
from app.schemas.application import ApplicationRead
from app.schemas.common import OkResponse, Page, paginate
from app.schemas.tracking import (
    EmailAccountCreate,
    EmailAccountRead,
    SignalFilter,
    SignalResolve,
    StatusSignalRead,
    SyncRequest,
    TrackingStats,
)
from app.tracking.service import StatusSyncService

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/tracking"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["tracking"]

#: Detail returned when a mailbox is connected without the credential it needs.
_MISSING_SECRET_DETAIL: Final[str] = (
    "A generic IMAP mailbox needs a password or app-specific password. It is written to the "
    "OS keychain and never stored in the database."
)

router = APIRouter()


def get_status_sync_service(session: DbSession, settings: SettingsDep) -> StatusSyncService:
    """Provide the status-sync service.

    Declared here rather than in :mod:`app.api.deps` because it is the only route group that
    uses it, and because ``app.tracking`` pulls in the mailbox adapters — a cost the other
    eleven groups should not pay on import.

    Args:
        session: The request's database session.
        settings: Runtime configuration.

    Returns:
        A service bound to that session.
    """
    return StatusSyncService(session, settings)


#: Status sync: mailboxes, signals and the review queue for them.
SyncServiceDep = Annotated[StatusSyncService, Depends(get_status_sync_service)]


# ======================================================================================
# Mailboxes
# ======================================================================================


@router.get(
    "/accounts",
    response_model=list[EmailAccountRead],
    summary="Mailboxes this user has connected",
)
async def list_accounts(
    user: CurrentUser,
    service: SyncServiceDep,
) -> list[EmailAccountRead]:
    """Return every connected mailbox, oldest first.

    Not a :class:`~app.schemas.common.Page`: a person connects one or two mailboxes, and
    paginating a two-item list would be ceremony.

    Args:
        user: The acting user.
        service: The status-sync service.

    Returns:
        The accounts, carrying ``connected`` but never a keychain reference.
    """
    accounts = await service.list_accounts(user.id)
    return [EmailAccountRead.model_validate(account) for account in accounts]


@router.post(
    "/accounts",
    response_model=EmailAccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Connect a mailbox, read-only",
)
async def connect_account(
    payload: EmailAccountCreate,
    user: CurrentUser,
    service: SyncServiceDep,
) -> EmailAccountRead:
    """Connect one mailbox so status sync may read it.

    This is the explicit act §17.8.6 requires: no mailbox is read until this row exists.
    Re-posting the same address updates the existing row rather than creating a second one —
    ``UNIQUE(user_id, address)`` — so reconnecting after a password change is idempotent.

    ``secret`` is written to the OS keychain and the row keeps only its key. Gmail and
    Outlook omit it: their tokens arrive from an OAuth callback that requests read-only
    scopes (``gmail.readonly``, ``Mail.Read``).

    Args:
        payload: Provider, address, optional secret, folders and enabled flag.
        user: The acting user.
        service: The status-sync service.

    Returns:
        The connected account.

    Raises:
        HTTPException: ``400`` when an IMAP mailbox is submitted with no secret and none is
            already stored, and ``503`` when the OS keychain is unavailable — there is no
            fallback to a database column, by design.
    """
    from app.models.enums import MailProvider

    secret = payload.secret.get_secret_value() if payload.secret is not None else None
    if payload.provider is MailProvider.IMAP and not secret:
        existing = await service.list_accounts(user.id)
        already = any(
            account.address == payload.address and account.connected for account in existing
        )
        if not already:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_MISSING_SECRET_DETAIL,
            )

    try:
        account = await service.connect_account(
            user.id,
            provider=payload.provider,
            address=payload.address,
            secret=secret,
            folders=payload.folders,
            enabled=payload.enabled,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    logger.info(
        "api.mailbox_connected",
        user_id=str(user.id),
        account_id=str(account.id),
        provider=str(account.provider),
    )
    return EmailAccountRead.model_validate(account)


@router.delete(
    "/accounts/{account_id}",
    response_model=OkResponse,
    summary="Disconnect a mailbox and delete its stored credential",
)
async def disconnect_account(
    account_id: uuid.UUID,
    user: CurrentUser,
    service: SyncServiceDep,
) -> OkResponse:
    """Disconnect one mailbox.

    Deletes the row, its cursor and the OS-keychain entry the row named, so that
    "disconnect" means what the user thinks it means (§17.8.6). Signals already read are
    kept: they are the record of what the mailbox said, and applications refer to them.

    Args:
        account_id: The mailbox to disconnect.
        user: The acting user.
        service: The status-sync service.

    Returns:
        An acknowledgement.

    Raises:
        HTTPException: ``404`` when no such mailbox exists for this user.
        LookupError: If the id names nothing — mapped to ``404``.
    """
    account = await service.get_account(account_id)
    require_owner(account.user_id, user.id, "email account")

    await service.disconnect_account(account_id)
    logger.info("api.mailbox_disconnected", user_id=str(user.id), account_id=str(account_id))
    return OkResponse(message="Mailbox disconnected and its stored credential removed.")


@router.post(
    "/accounts/{account_id}/test",
    response_model=OkResponse,
    summary="Check a mailbox's credentials without reading any mail",
)
async def test_account(
    account_id: uuid.UUID,
    user: CurrentUser,
    service: SyncServiceDep,
) -> OkResponse:
    """Connect to the mailbox, then disconnect. No message is fetched.

    The question being asked is "did my credential work?", and answering it must not pull
    mail into memory, advance a cursor, or create a signal.

    Args:
        account_id: The mailbox to probe.
        user: The acting user.
        service: The status-sync service.

    Returns:
        ``ok`` with the outcome in ``data``: ``{"connected": bool, "error": str | None}``.
        A failed probe is a ``200`` with ``connected=false``, not an error status — the
        request succeeded, the mailbox did not.

    Raises:
        LookupError: If the id names nothing — mapped to ``404``.
    """
    account = await service.get_account(account_id)
    require_owner(account.user_id, user.id, "email account")

    connected, error = await service.test_account(account_id)
    return OkResponse(
        message="Mailbox reachable." if connected else "Mailbox could not be reached.",
        data={"connected": connected, "error": error},
    )


# ======================================================================================
# Syncing
# ======================================================================================


@router.post(
    "/sync",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a sync now",
)
async def run_sync(
    payload: SyncRequest,
    user: CurrentUser,
    service: SyncServiceDep,
) -> OkResponse:
    """Queue a status sync and return immediately.

    Queued rather than awaited: reading a mailbox is seconds to minutes of network work, and
    the desktop app fires this on launch. An unreachable broker is still a ``202`` with
    ``data.degraded`` set — the request was fine, the background worker is not running, and
    reporting that as a ``500`` would make a perfectly usable install look broken.

    Args:
        payload: Optional ``account_id`` to sync one mailbox, and optional ``since``.
        user: The acting user.
        service: The status-sync service, used for the ownership check.

    Returns:
        202 with the dispatch outcome.

    Raises:
        LookupError: If ``account_id`` names nothing — mapped to ``404``.
    """
    since = payload.since.isoformat() if payload.since is not None else None

    if payload.account_id is not None:
        account = await service.get_account(payload.account_id)
        require_owner(account.user_id, user.id, "email account")
        outcome = await dispatch(TASK_SYNC_POLL_ACCOUNT, str(payload.account_id), since)
    else:
        outcome = await dispatch(TASK_SYNC_POLL_ALL, str(user.id), since)

    logger.info(
        "api.sync_requested",
        user_id=str(user.id),
        account_id=str(payload.account_id) if payload.account_id else None,
        degraded=outcome.degraded,
    )
    return OkResponse(message="Status sync queued.", data=outcome.as_dict())


@router.post(
    "/detect-ghosted",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Mark long-silent applications ghosted",
)
async def run_detect_ghosted(user: CurrentUser) -> OkResponse:
    """Queue the ghosting sweep for this user.

    Also runs daily on the beat schedule. Exposed here because "why does my funnel show
    forty applications still pending?" is a question a user asks in the moment, and the
    answer is a sweep they should be able to trigger.

    Args:
        user: The acting user.

    Returns:
        202 with the dispatch outcome.
    """
    outcome = await dispatch(TASK_SYNC_DETECT_GHOSTED, str(user.id))
    return OkResponse(message="Ghost detection queued.", data=outcome.as_dict())


# ======================================================================================
# Signals
# ======================================================================================


@router.get(
    "/signals",
    response_model=Page[StatusSignalRead],
    summary="Classified messages, newest first",
)
async def list_signals(
    user: CurrentUser,
    session: DbSession,
    filters: Annotated[SignalFilter, Depends()],
) -> Page[StatusSignalRead]:
    """Return one page of status signals.

    Newest first: a signal is a message, and the most recent mail is the most likely to
    still be actionable.

    Args:
        user: The acting user.
        session: The request's database session.
        filters: ``source``, ``kind``, ``application_id``, ``applied``, ``needs_review``,
            ``since`` and ``until``, plus the inherited ``limit``/``offset``.

    Returns:
        The page, with a true unpaginated total.
    """
    conditions: list[Any] = [StatusSignalRow.user_id == user.id]
    if filters.source is not None:
        conditions.append(StatusSignalRow.source == filters.source)
    if filters.kind is not None:
        conditions.append(StatusSignalRow.kind == filters.kind)
    if filters.application_id is not None:
        conditions.append(StatusSignalRow.application_id == filters.application_id)
    if filters.applied is not None:
        conditions.append(StatusSignalRow.applied.is_(filters.applied))
    if filters.needs_review is not None:
        conditions.append(StatusSignalRow.needs_review.is_(filters.needs_review))
    if filters.since is not None:
        conditions.append(StatusSignalRow.received_at >= filters.since)
    if filters.until is not None:
        conditions.append(StatusSignalRow.received_at <= filters.until)

    total = int(
        await session.scalar(select(func.count(StatusSignalRow.id)).where(*conditions)) or 0
    )
    rows = await session.execute(
        select(StatusSignalRow)
        .where(*conditions)
        .order_by(StatusSignalRow.received_at.desc())
        .limit(filters.limit)
        .offset(filters.offset)
    )
    items = [StatusSignalRead.model_validate(row) for row in rows.scalars().all()]
    return paginate(items, total=total, params=filters)


@router.post(
    "/signals/{signal_id}/resolve",
    response_model=ApplicationRead,
    summary="Confirm what a signal meant and which application it was about",
)
async def resolve_signal(
    signal_id: uuid.UUID,
    payload: SignalResolve,
    user: CurrentUser,
    service: SyncServiceDep,
) -> ApplicationRead:
    """Apply a human's decision about a parked signal.

    Both fields are required because the signal is in the queue precisely because the system
    would have had to guess at one of them. The resulting status is recorded as
    ``manual`` with full confidence: a person who looked at the evidence and decided is the
    highest authority there is, and marking it so stops a later, weaker signal from
    overwriting their answer.

    Args:
        signal_id: The signal being resolved.
        payload: The application it refers to, and the status it implies.
        user: The acting user.
        service: The status-sync service.

    Returns:
        The application, in its new status.

    Raises:
        LookupError: If the signal or the application does not exist — mapped to ``404``.
        InvalidTransition: If the state machine forbids the move — mapped to ``400``.
    """
    signal = await service.get_signal(signal_id)
    require_owner(signal.user_id, user.id, "status signal")

    application = await service.resolve(
        signal_id,
        application_id=payload.application_id,
        status=payload.status,
    )
    logger.info(
        "api.signal_resolved",
        user_id=str(user.id),
        signal_id=str(signal_id),
        application_id=str(payload.application_id),
        status=payload.status.value,
    )
    return ApplicationRead.model_validate(application)


@router.post(
    "/signals/{signal_id}/dismiss",
    response_model=OkResponse,
    summary="Take a signal off the review queue without changing anything",
)
async def dismiss_signal(
    signal_id: uuid.UUID,
    user: CurrentUser,
    service: SyncServiceDep,
) -> OkResponse:
    """Dismiss one signal.

    The row is kept, not deleted. It is still the record of what the mailbox said, and it is
    what stops the next sync of an overlapping window from re-reading the same message and
    asking the same question again.

    Args:
        signal_id: The signal to dismiss.
        user: The acting user.
        service: The status-sync service.

    Returns:
        An acknowledgement.

    Raises:
        LookupError: If the id names nothing — mapped to ``404``.
    """
    signal = await service.get_signal(signal_id)
    require_owner(signal.user_id, user.id, "status signal")

    await service.dismiss(signal_id)
    return OkResponse(message="Signal dismissed.")


# ======================================================================================
# Aggregate state
# ======================================================================================


@router.get(
    "/stats",
    response_model=TrackingStats,
    summary="Everything the tracking panel renders, in one request",
)
async def tracking_stats(
    user: CurrentUser,
    service: SyncServiceDep,
) -> TrackingStats:
    """Return connected-mailbox counts, signal counts by kind, and the last sync time.

    Args:
        user: The acting user.
        service: The status-sync service.

    Returns:
        The aggregate.
    """
    return TrackingStats.model_validate(await service.stats(user.id))
