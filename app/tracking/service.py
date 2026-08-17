"""The only thing in status sync that writes (``docs/CONTRACTS.md`` §17.3).

The tracker reads, the classifier interprets, the matcher binds — and this service decides
what, if anything, actually happens to the user's data as a result. Everything upstream of
here is a pure function over a message; everything downstream of here is a row.

Four properties are load-bearing, and each one is a specific failure this module exists to
prevent.

**Re-syncing is free.** ``UNIQUE(user_id, source, external_ref)`` means one message can only
ever become one ``status_signals`` row, so a re-poll of an overlapping window, a reset
cursor, a redelivered Celery message or a crash halfway through a run all converge on the
same state. The insert happens inside a **SAVEPOINT** so that the losing side of that race
rolls back alone and the rest of the run continues; without the savepoint one duplicate would
poison the transaction and lose the four hundred messages after it.

**The state machine stays authoritative.** Applying a signal goes through
:meth:`~app.services.application_service.ApplicationService.transition` — never a direct
assignment to ``Application.status``. That is what keeps golden rule #1 auditable (there is
no edge from ``submitted`` back to ``ready``, so no email can create one) and what guarantees
an :class:`~app.models.application.ApplicationEvent` for every change. A transition the
machine refuses is not an error here: it means the mailbox is telling us something the
application's history cannot accommodate, and the honest response is to show a human.

**Confidence multiplies.** A signal is applied automatically only when the classification and
the match are *both* strong, so the stored confidence is their product. Two independent
0.9-confident judgements about different questions are not a 0.9-confident conclusion, and
treating them as one is how a plausible rejection lands on the wrong job.

**Ghosting is the point.** :meth:`StatusSyncService.detect_ghosted` is the outcome the user
would otherwise never record. Most applications end in silence; if silence stays invisible
the funnel shows a hundred "submitted" forever, the response rate is unknowable, and "which
of my resumes are not landing?" — the question the whole product exists to answer — has no
data behind it.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from app.database.types import utcnow
from app.models.application import Application
from app.models.enums import (
    ApplicationStatus,
    MailProvider,
    PluginKind,
    SignalKind,
    SignalSource,
    StatusSource,
)
from app.models.tracking import EmailAccount
from app.models.tracking import StatusSignal as StatusSignalRow
from app.tracking.base import SyncReport
from app.tracking.classifier import StatusClassifier
from app.tracking.matcher import SignalMatcher

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config.settings import Settings
    from app.tracking.base import Classification, Match, StatusSignal

__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "EMAIL_TRACKER_NAME",
    "GHOSTED_ELIGIBLE_STATUSES",
    "LAST_ERROR_MAX_CHARS",
    "MANUAL_OUTCOME_STATUSES",
    "NEVER_AUTO_APPLIED",
    "StatusSyncService",
    "credential_key",
    "delete_credential",
    "store_credential",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Registry key of the tracker this service drives. A *name*, not an import: golden rule #5
#: forbids binding to a concrete plugin module, so the tracker is resolved through
#: :data:`app.plugins.registry.registry` and the only thing crossing the boundary is this
#: string. Its transport type is imported lazily, at the one call site that needs it.
EMAIL_TRACKER_NAME: Final[str] = "email"

#: Fallback for ``settings.status_sync_min_confidence`` when settings cannot be read.
DEFAULT_MIN_CONFIDENCE: Final[float] = 0.80

#: Longest message stored on ``email_accounts.last_error``. The column is ``Text``, but the
#: value is rendered beside the account in the desktop app and a stack trace helps nobody.
LAST_ERROR_MAX_CHARS: Final[int] = 500

#: Statuses an application may be ghosted from. Only the two that mean "the employer has it
#: and has said nothing since" — an application already in an outcome state has an answer,
#: and one still being prepared was never sent.
GHOSTED_ELIGIBLE_STATUSES: Final[tuple[ApplicationStatus, ...]] = (
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.CONFIRMED,
)

#: **Final** outcomes that, when the user recorded them by hand, are never overwritten
#: automatically; the signal is routed to review instead so the person who decided can change
#: their own mind. Deliberately narrow. ``interview`` and ``ghosted`` are *not* here, because
#: an interview legitimately becomes an offer or a rejection and a ghosted employer sometimes
#: writes back — blocking those would make the sync useless exactly when it is most valuable.
MANUAL_OUTCOME_STATUSES: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
        ApplicationStatus.ACCEPTED,
        ApplicationStatus.ABANDONED,
    }
)

#: Statuses a phrase match may **never** apply on its own, however confident it is.
#:
#: Narrow on purpose, and the test for membership is recoverability rather than importance.
#: A wrong `rejected` is annoying and reversible — the state machine keeps edges out of it.
#: A wrong `accepted` is neither: it is the end of the funnel, it feeds
#: `is_successful_outcome()`, the offer rate and the memory weights, and no product path can
#: undo it without a second deliberate act. Something that irreversible is worth one click.
NEVER_AUTO_APPLIED: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.ACCEPTED,
    }
)

#: Event names published to the WebSocket bus (§17.7).
_EVENT_SYNC_STARTED: Final[str] = "tracking.sync_started"
_EVENT_SYNC_PROGRESS: Final[str] = "tracking.sync_progress"
_EVENT_SIGNAL_FOUND: Final[str] = "tracking.signal_found"
_EVENT_STATUS_CHANGED: Final[str] = "tracking.status_changed"
_EVENT_SYNC_FINISHED: Final[str] = "tracking.sync_finished"

#: How often, in messages, a long run publishes ``tracking.sync_progress``.
_PROGRESS_EVERY: Final[int] = 25


# ======================================================================================
# Credentials — the keychain, never the database (§17.8.4)
# ======================================================================================


def credential_key(provider: MailProvider, address: str) -> str:
    """Return the OS-keychain key for one mailbox.

    Stored verbatim in ``email_accounts.credential_ref``. It is a *name*, not a secret: the
    password or refresh token it names lives in Windows Credential Manager, the macOS
    Keychain or the freedesktop Secret Service, and never in a column or a ``.env`` file.

    Args:
        provider: The mailbox implementation.
        address: The mailbox address.

    Returns:
        ``"<provider>:<address>"``, lowercased.
    """
    return f"{MailProvider(provider).value}:{address.strip().lower()}"


def store_credential(reference: str, secret: str) -> None:
    """Write one mailbox secret to the OS keychain.

    The counterpart of :func:`~app.tracking.email.base.load_credential`, and the only writer.
    There is deliberately no fallback: if ``keyring`` is unavailable the connection fails
    with an actionable message rather than silently persisting a mailbox password somewhere
    it must never be (§17.8.4).

    Blocking — keychain backends talk to a system service — so async callers wrap it in
    :func:`asyncio.to_thread`.

    Args:
        reference: The key from :func:`credential_key`.
        secret: The password, app password, or JSON OAuth token blob.

    Raises:
        RuntimeError: If ``keyring`` is not installed or the keychain refused the write.
    """
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "the 'keyring' package is required to store a mailbox credential in the OS "
            "keychain but is not installed — run `pip install keyring`. ApplicantOS will "
            "not fall back to storing mailbox secrets in the database or in .env "
            "(docs/CONTRACTS.md §17.8.4)."
        ) from exc

    from app.tracking.email import KEYRING_SERVICE

    try:
        keyring.set_password(KEYRING_SERVICE, reference, secret)
    except Exception as exc:  # keyring raises backend-specific errors
        raise RuntimeError(
            f"the OS keychain refused to store this credential ({type(exc).__name__}); "
            "unlock the keychain and try again"
        ) from exc


def delete_credential(reference: str) -> bool:
    """Remove one mailbox secret from the OS keychain.

    Called when a mailbox is disconnected: §17.8.6 requires that disconnecting deletes the
    stored credential reference *and* the secret it names, so that "disconnect" means what
    the user thinks it means.

    Args:
        reference: The key from :func:`credential_key`.

    Returns:
        ``True`` when an entry was removed or none existed, ``False`` when the keychain
        could not be reached. Never raises: a mailbox must be disconnectable even on a
        machine whose keychain is locked, because the row and the poller are what actually
        read mail.
    """
    if not reference:
        return True
    try:
        import keyring
    except ImportError:
        logger.debug("tracking.keyring_absent_on_delete")
        return False

    from app.tracking.email import KEYRING_SERVICE

    try:
        keyring.delete_password(KEYRING_SERVICE, reference)
    except Exception as exc:
        logger.info("tracking.credential_delete_failed", error_type=type(exc).__name__)
        return type(exc).__name__ == "PasswordDeleteError"
    return True


# ======================================================================================
# The service
# ======================================================================================


class StatusSyncService:
    """Reads mailboxes, records what they said, and moves applications when it is sure.

    Args:
        session: The unit of work. Reads and signal writes happen on it directly; status
            transitions are delegated to
            :class:`~app.services.application_service.ApplicationService`, which commits.
        settings: Runtime configuration. Resolved from the process singleton when ``None``.

    Usage::

        service = StatusSyncService(session, settings)
        reports = await service.sync_all(user.id)
        for signal in await service.pending_review(user.id):
            ...
    """

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        """Bind the service to one session and one settings object."""
        self._session = session
        self._settings_override = settings
        self._classifier = StatusClassifier(settings)
        self._matcher = SignalMatcher(session, settings)
        self.logger = logger

    # ----------------------------------------------------------------------------------
    # Configuration
    # ----------------------------------------------------------------------------------

    @property
    def settings(self) -> Any:
        """The settings this service runs against, resolved lazily."""
        if self._settings_override is not None:
            return self._settings_override
        from app.config.settings import get_settings

        return get_settings()

    @property
    def min_confidence(self) -> float:
        """The combined confidence at or above which a signal may apply itself."""
        value = getattr(self.settings, "status_sync_min_confidence", DEFAULT_MIN_CONFIDENCE)
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return DEFAULT_MIN_CONFIDENCE

    @property
    def auto_apply(self) -> bool:
        """Whether high-confidence signals may move an application without being asked."""
        return bool(getattr(self.settings, "status_sync_auto_apply", True))

    # ----------------------------------------------------------------------------------
    # Accounts
    # ----------------------------------------------------------------------------------

    async def list_accounts(self, user_id: uuid.UUID) -> list[EmailAccount]:
        """Return every mailbox this user has connected, oldest first.

        Args:
            user_id: The applicant.

        Returns:
            The account rows. Callers project them through
            :class:`~app.schemas.tracking.EmailAccountRead`, which carries no
            ``credential_ref``.
        """
        rows = await self._session.execute(
            select(EmailAccount)
            .where(EmailAccount.user_id == user_id)
            .order_by(EmailAccount.created_at.asc())
        )
        return list(rows.scalars().all())

    async def get_account(self, account_id: uuid.UUID) -> EmailAccount:
        """Load one mailbox by id.

        Args:
            account_id: The account's identifier.

        Returns:
            The row.

        Raises:
            LookupError: If no account has that id.
        """
        account = await self._session.get(EmailAccount, account_id)
        if account is None:
            raise LookupError(f"email account {account_id} not found")
        return account

    async def connect_account(
        self,
        user_id: uuid.UUID,
        *,
        provider: MailProvider,
        address: str,
        secret: str | None = None,
        folders: list[str] | None = None,
        enabled: bool = True,
    ) -> EmailAccount:
        """Connect a mailbox, or re-connect one that already exists.

        The explicit act §17.8.6 requires: nothing is read from any mailbox until this row
        exists. Re-connecting the same address **updates** the existing row rather than
        creating a second one — ``UNIQUE(user_id, address)`` guarantees that, and two rows
        for one inbox would mean two competing syncs and two cursors.

        The secret goes to the OS keychain and the row keeps only its key.

        Args:
            user_id: The applicant.
            provider: Mailbox implementation.
            address: The mailbox address.
            secret: IMAP password or app password, or a JSON OAuth token blob. Omitted when
                re-connecting without changing the credential.
            folders: Folders or labels to search. Empty means the provider's inbox scope.
            enabled: Whether the poller may read it once connected.

        Returns:
            The connected account, committed.

        Raises:
            RuntimeError: If a secret was supplied and the OS keychain could not store it.
        """
        import asyncio

        normalized = address.strip().lower()
        reference = credential_key(provider, normalized)

        if secret:
            await asyncio.to_thread(store_credential, reference, secret)

        account = await self._session.scalar(
            select(EmailAccount)
            .where(EmailAccount.user_id == user_id, EmailAccount.address == normalized)
            .limit(1)
        )
        if account is None:
            account = EmailAccount(user_id=user_id, provider=provider, address=normalized)
            self._session.add(account)

        account.provider = provider
        account.enabled = bool(enabled)
        account.folders = list(folders or [])
        account.last_error = None
        if secret or not account.credential_ref:
            account.credential_ref = reference

        await self._session.flush()
        await self._session.commit()
        logger.info(
            "tracking.account_connected",
            account_id=str(account.id),
            user_id=str(user_id),
            provider=str(provider),
        )
        return account

    async def disconnect_account(self, account_id: uuid.UUID) -> None:
        """Delete a mailbox row, its cursor, and the keychain entry it named.

        All three, because §17.8.6 says disconnecting removes the stored credential
        reference and the cursor — a row that lingered would leave a resumable sync pointing
        at a mailbox the user believes they revoked.

        Signals already read are **kept**: they are evidence of what the mailbox said, and
        the applications they moved refer to them.

        Args:
            account_id: The account to disconnect.

        Raises:
            LookupError: If no account has that id.
        """
        import asyncio

        account = await self.get_account(account_id)
        reference = account.credential_ref or ""
        await self._session.delete(account)
        await self._session.commit()

        if reference:
            await asyncio.to_thread(delete_credential, reference)
        logger.info("tracking.account_disconnected", account_id=str(account_id))

    async def test_account(self, account_id: uuid.UUID) -> tuple[bool, str | None]:
        """Probe one mailbox's credentials without reading a message.

        Args:
            account_id: The account to probe.

        Returns:
            ``(ok, error)``; the error is stored on ``last_error`` so the desktop app can
            render it beside the account without a second request.

        Raises:
            LookupError: If no account has that id.
        """
        account = await self.get_account(account_id)
        tracker = self._tracker()
        ok, error = await tracker.test_account(account)

        account.last_error = None if ok else (error or "")[:LAST_ERROR_MAX_CHARS]
        await self._session.flush()
        await self._session.commit()
        return ok, error

    # ----------------------------------------------------------------------------------
    # Syncing
    # ----------------------------------------------------------------------------------

    async def sync_all(
        self,
        user_id: uuid.UUID,
        *,
        since: datetime | None = None,
    ) -> list[SyncReport]:
        """Sync every enabled mailbox this user has connected.

        One report per account. A mailbox that fails is reported with its error and the rest
        still run: a revoked Gmail grant must not stop an IMAP account from delivering the
        rejection that arrived this morning.

        Args:
            user_id: The applicant.
            since: Override the incremental window. Clamped to
                ``settings.status_sync_lookback_days`` by the mailbox adapters.

        Returns:
            The reports, in account order. Empty when status sync is switched off or the
            user has connected nothing — neither is an error.
        """
        if not bool(getattr(self.settings, "status_sync_enabled", True)):
            logger.info("tracking.sync_disabled", user_id=str(user_id))
            return []

        tracker = self._tracker()
        accounts = await tracker.enabled_accounts(user_id, self._session)
        if not accounts:
            logger.info("tracking.no_accounts", user_id=str(user_id))
            return []

        senders = await tracker.sender_allowlist(user_id, self._session)
        reports: list[SyncReport] = []
        for account in accounts:
            reports.append(await self._sync_one(account, since=since, senders=senders))
        return reports

    async def sync_account(
        self,
        account_id: uuid.UUID,
        *,
        since: datetime | None = None,
    ) -> SyncReport:
        """Sync one mailbox.

        **Idempotent.** Re-running over the same window inserts nothing new: every message
        already seen collides with ``UNIQUE(user_id, source, external_ref)`` inside its own
        SAVEPOINT and is counted as a skip. That is what makes a redelivered Celery message,
        a reset cursor and a crash mid-run all safe.

        Args:
            account_id: The mailbox to poll.
            since: Override the incremental window.

        Returns:
            What the run did.

        Raises:
            LookupError: If no account has that id.
        """
        account = await self.get_account(account_id)
        return await self._sync_one(account, since=since, senders=None)

    async def _sync_one(
        self,
        account: EmailAccount,
        *,
        since: datetime | None,
        senders: list[str] | None,
    ) -> SyncReport:
        """Poll one mailbox and process everything it yields.

        Args:
            account: The mailbox row.
            since: Override for the incremental window.
            senders: Pre-computed sender allowlist, or ``None`` to let the tracker build it.

        Returns:
            The run's report. Failures are recorded on the report and on
            ``email_accounts.last_error`` rather than raised: a caller syncing five mailboxes
            needs all five outcomes.
        """
        from app.tracking.trackers.email_tracker import PollRun

        report = SyncReport(account_id=account.id)
        started = time.monotonic()
        self._publish(
            _EVENT_SYNC_STARTED,
            {"account_id": str(account.id), "provider": str(account.provider)},
        )

        tracker = self._tracker()
        run = PollRun()
        try:
            stream = tracker.poll_account(account, run=run, since=since, senders=senders)
            async for signal in stream:
                report.fetched += 1
                await self._process(account, signal, report)
                if report.fetched % _PROGRESS_EVERY == 0:
                    self._publish(
                        _EVENT_SYNC_PROGRESS,
                        {"account_id": str(account.id), **report.as_dict()},
                    )
        except Exception as exc:
            report.record_error(f"{type(exc).__name__}: {exc}")
            account.last_error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_CHARS]
            logger.warning(
                "tracking.sync_failed",
                account_id=str(account.id),
                provider=str(account.provider),
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
        else:
            account.last_error = None

        # The cursor advances only for a run that completed. Advancing it after a partial
        # read would skip whatever the failed half contained (golden rule #8).
        if run.completed:
            if run.cursor:
                account.cursor = run.cursor
            account.last_sync_at = utcnow()
        await self._session.flush()
        await self._session.commit()

        report.duration_seconds = time.monotonic() - started
        self._publish(_EVENT_SYNC_FINISHED, report.as_dict())
        logger.info(
            "tracking.sync_finished",
            account_id=str(account.id),
            provider=str(account.provider),
            **{
                key: value
                for key, value in report.as_dict().items()
                if key not in ("account_id", "errors")
            },
        )
        return report

    async def _process(
        self,
        account: EmailAccount,
        signal: StatusSignal,
        report: SyncReport,
    ) -> None:
        """Classify, match, persist and possibly apply one message.

        Messages that classify as ``unknown`` are counted as skipped and **not persisted**.
        Most mail in any lookback window is not about an application, and writing a row for
        every one of them would turn ``status_signals`` into a copy of the user's inbox
        metadata for no benefit — the opposite of the minimal-retention rule in §17.8.3.

        Args:
            account: The mailbox the message came from.
            signal: The transport-shaped message.
            report: The run's counters, mutated in place.
        """
        classification = await self._classifier.classify(signal)
        if classification.kind is SignalKind.UNKNOWN:
            report.skipped += 1
            return
        report.classified += 1

        match = await self._matcher.match(account.user_id, signal, classification)
        if match.matched:
            report.matched += 1

        # The product, not the minimum: two independent 0.9-confident judgements about two
        # different questions do not add up to a 0.9-confident conclusion.
        combined = round(classification.confidence * match.confidence, 4)
        row = await self._persist(account, signal, classification, match, combined)
        if row is None:
            report.skipped += 1
            return

        self._publish(
            _EVENT_SIGNAL_FOUND,
            {
                "id": str(row.id),
                "kind": row.kind.value,
                "application_id": str(row.application_id) if row.application_id else None,
                "confidence": row.confidence,
            },
        )

        if classification.status is None:
            # A kind that carries information but implies no status change — a "your
            # application was viewed" notice. It is recorded for analytics and then left
            # alone: parking it for review would ask the user to confirm something that
            # would change nothing, and a queue full of those is a queue nobody reads.
            row.needs_review = False
        elif await self._maybe_apply(row, classification, combined):
            report.applied += 1
        else:
            row.needs_review = True
            report.needs_review += 1
        await self._session.flush()
        await self._session.commit()

    async def _persist(
        self,
        account: EmailAccount,
        signal: StatusSignal,
        classification: Classification,
        match: Match,
        combined: float,
    ) -> StatusSignalRow | None:
        """Insert one ``status_signals`` row, or report that it already exists.

        The insert runs inside a **SAVEPOINT**. A duplicate is the expected outcome of any
        overlapping re-sync, and without the savepoint that collision would abort the outer
        transaction and discard every message processed before it.

        Only the 500-character :attr:`~app.tracking.base.StatusSignal.snippet` is written —
        never the body (§17.8.3).

        Args:
            account: The mailbox the message came from.
            signal: The transport-shaped message.
            classification: What it means.
            match: Which application it is about.
            combined: The product of the two confidences.

        Returns:
            The new row, or ``None`` when this message had already been recorded.
        """
        row = StatusSignalRow(
            user_id=account.user_id,
            application_id=match.application_id,
            source=SignalSource(signal.source),
            kind=classification.kind,
            external_ref=signal.external_ref,
            sender=signal.sender,
            sender_domain=signal.sender_domain,
            subject=signal.subject,
            snippet=signal.snippet,
            received_at=signal.received_at,
            detected_status=classification.status,
            confidence=combined,
            applied=False,
            needs_review=False,
            match_evidence={
                **dict(match.evidence),
                "classification": {
                    "kind": classification.kind.value,
                    "confidence": classification.confidence,
                    "evidence": classification.evidence,
                    "matched_rule": classification.matched_rule,
                    "model_used": classification.model_used,
                },
                "combined_confidence": combined,
            },
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            try:
                self._session.expunge(row)
            except InvalidRequestError:  # pragma: no cover - the savepoint already detached it
                logger.debug("tracking.signal_already_detached")
            logger.debug("tracking.signal_already_seen", **signal.log_context())
            return None
        return row

    async def _maybe_apply(
        self,
        row: StatusSignalRow,
        classification: Classification,
        combined: float,
    ) -> bool:
        """Decide whether this signal may move its application, and do it if so.

        Every one of these gates is a way of saying "do not guess":

        * auto-apply switched off in settings,
        * nothing matched,
        * a kind that implies no status change (``viewed``),
        * combined confidence below ``settings.status_sync_min_confidence``,
        * an outcome the *user* recorded by hand, which a phrase match never overrides,
        * a transition the state machine forbids.

        Args:
            row: The persisted signal.
            classification: What the message means.
            combined: The product of classification and match confidence.

        Returns:
            ``True`` when the application's status actually changed.
        """
        if not self.auto_apply:
            return False
        if row.application_id is None or classification.status is None:
            return False
        if combined < self.min_confidence:
            return False
        if classification.status in NEVER_AUTO_APPLIED:
            # Golden rule #2, applied to the one outcome nothing can walk back. Every other
            # status a signal can produce is recoverable: a mistaken `rejected` still has
            # edges to `interview` and `offer`, a mistaken `ghosted` has three. `accepted`
            # is the end of the funnel, and a wrong one silently records a hire that did not
            # happen — in the analytics, in the memory weights, and in the figure the user
            # reads on the dashboard.
            #
            # So it parks. The signal is already persisted with its evidence, and
            # `POST /tracking/signals/{id}/resolve` applies it in one click. Asking is
            # cheaper than being wrong here by a wide margin.
            logger.info(
                "tracking.status_needs_confirmation",
                application_id=str(row.application_id),
                proposed=classification.status.value,
                confidence=round(combined, 3),
            )
            return False
        return await self._transition(row, classification.status, combined, StatusSource.EMAIL)

    async def _transition(
        self,
        row: StatusSignalRow,
        status: ApplicationStatus,
        confidence: float,
        source: StatusSource,
    ) -> bool:
        """Move the signal's application, through the state machine, and stamp provenance.

        Args:
            row: The signal driving the change.
            status: The status to move to.
            confidence: Recorded on ``applications.status_confidence``.
            source: Recorded on ``applications.status_source``.

        Returns:
            ``True`` when the transition succeeded. ``False`` when the application is gone
            or the machine refused the move — both of which mean "ask a human", never
            "force it".
        """
        from app.services.application_service import ApplicationService, InvalidTransition

        if row.application_id is None:
            return False
        application = await self._session.get(Application, row.application_id)
        if application is None:
            return False

        if (
            application.status_source is StatusSource.MANUAL
            and application.status in MANUAL_OUTCOME_STATUSES
            and application.status is not status
        ):
            logger.info(
                "tracking.manual_status_preserved",
                application_id=str(application.id),
                current=application.status.value,
                proposed=status.value,
            )
            return False

        application.status_source = source
        application.status_confidence = confidence
        application.last_synced_at = utcnow()

        try:
            await ApplicationService(self._session).transition(
                application,
                status,
                message=f"Status read from {source.value}.",
                payload={
                    "signal_id": str(row.id),
                    "signal_kind": row.kind.value,
                    "confidence": confidence,
                    "sender_domain": row.sender_domain,
                },
            )
        except InvalidTransition as exc:
            # Not an error. The mailbox said something the application's history cannot
            # accommodate — an assessment request for an application already rejected, a
            # rejection for one still in draft — and a human should look at it.
            self._session.expire(application)
            logger.info(
                "tracking.transition_refused",
                application_id=str(row.application_id),
                signal_id=str(row.id),
                reason=str(exc),
            )
            return False

        row.applied = True
        row.needs_review = False
        self._publish(
            _EVENT_STATUS_CHANGED,
            {
                "application_id": str(application.id),
                "status": status.value,
                "source": source.value,
                "confidence": confidence,
                "signal_id": str(row.id),
            },
        )
        return True

    # ----------------------------------------------------------------------------------
    # The review queue
    # ----------------------------------------------------------------------------------

    async def apply_signal(self, signal_id: uuid.UUID) -> Application | None:
        """Apply one already-stored signal now.

        The path a worker or an operator takes to act on a signal that was parked — because
        auto-apply was off at the time, or because it was below the threshold and a human
        has since raised it. The confidence gate still applies: this method does not
        *override* the decision, it re-runs it.

        Args:
            signal_id: The stored signal.

        Returns:
            The application, when a status change was made. ``None`` when the signal carries
            no status, matched nothing, is below the threshold, or has already been applied.

        Raises:
            LookupError: If no signal has that id.
        """
        row = await self.get_signal(signal_id)
        if row.applied or row.application_id is None or row.detected_status is None:
            return None
        if float(row.confidence or 0.0) < self.min_confidence:
            return None

        changed = await self._transition(
            row,
            ApplicationStatus(row.detected_status),
            float(row.confidence or 0.0),
            StatusSource.EMAIL,
        )
        await self._session.flush()
        await self._session.commit()
        if not changed:
            return None
        return await self._session.get(Application, row.application_id)

    async def pending_review(self, user_id: uuid.UUID) -> list[StatusSignalRow]:
        """Return the signals waiting for one-click human confirmation, newest first.

        Newest first, unlike the application review queue: an unresolved signal is a
        *question about a fact*, and the most recent mail is the most likely to still be
        actionable. A three-week-old ambiguous rejection has usually been overtaken.

        Args:
            user_id: The applicant.

        Returns:
            Signals with ``needs_review`` set that have not been applied.
        """
        rows = await self._session.execute(
            select(StatusSignalRow)
            .where(
                StatusSignalRow.user_id == user_id,
                StatusSignalRow.needs_review.is_(True),
                StatusSignalRow.applied.is_(False),
            )
            .order_by(StatusSignalRow.received_at.desc())
        )
        return list(rows.scalars().all())

    async def get_signal(self, signal_id: uuid.UUID) -> StatusSignalRow:
        """Load one signal by id.

        Args:
            signal_id: The signal's identifier.

        Returns:
            The row.

        Raises:
            LookupError: If no signal has that id.
        """
        row = await self._session.get(StatusSignalRow, signal_id)
        if row is None:
            raise LookupError(f"status signal {signal_id} not found")
        return row

    async def resolve(
        self,
        signal_id: uuid.UUID,
        *,
        application_id: uuid.UUID,
        status: ApplicationStatus,
    ) -> Application:
        """Record a human's decision about a parked signal.

        Both arguments are required by :class:`~app.schemas.tracking.SignalResolve` for the
        same reason they are required here: the signal is in this queue precisely because
        the system would have had to guess at one of them.

        The resulting status is stamped ``StatusSource.MANUAL`` with confidence ``1.0``. A
        person looking at the evidence and deciding *is* the manual path, and marking it so
        is what stops a later, weaker signal from overwriting their answer.

        Args:
            signal_id: The signal being resolved.
            application_id: The application it actually refers to.
            status: The status that application should now be in.

        Returns:
            The application, committed.

        Raises:
            LookupError: If the signal or the application does not exist, or they belong to
                different users — indistinguishable from "not found" to the caller, and
                deliberately so.
            InvalidTransition: If the state machine forbids the move — mapped to ``400``.
        """
        from app.services.application_service import ApplicationService

        row = await self.get_signal(signal_id)
        application = await self._session.get(Application, application_id)
        if application is None or application.user_id != row.user_id:
            raise LookupError(f"application {application_id} not found")

        target = ApplicationStatus(status)
        application.status_source = StatusSource.MANUAL
        application.status_confidence = 1.0
        application.last_synced_at = utcnow()

        await ApplicationService(self._session).transition(
            application,
            target,
            message="Status confirmed by the user from an email signal.",
            payload={
                "signal_id": str(row.id),
                "signal_kind": row.kind.value,
                "confirmed_by_user": True,
            },
        )

        row.application_id = application.id
        row.applied = True
        row.needs_review = False
        row.detected_status = target
        row.match_evidence = {
            **dict(row.match_evidence or {}),
            "resolved_by_user": True,
            "resolved_at": utcnow().isoformat(),
        }
        await self._session.flush()
        await self._session.commit()

        self._publish(
            _EVENT_STATUS_CHANGED,
            {
                "application_id": str(application.id),
                "status": target.value,
                "source": StatusSource.MANUAL.value,
                "confidence": 1.0,
                "signal_id": str(row.id),
            },
        )
        logger.info(
            "tracking.signal_resolved",
            signal_id=str(signal_id),
            application_id=str(application_id),
            status=target.value,
        )
        return application

    async def dismiss(self, signal_id: uuid.UUID) -> None:
        """Take a signal off the review queue without changing anything.

        The row is kept. A dismissed signal is still the record of what the mailbox said,
        and deleting it would let the next sync of an overlapping window re-read the same
        message and ask the same question again — the unique constraint is what prevents
        that, and it only works while the row exists.

        Args:
            signal_id: The signal to dismiss.

        Raises:
            LookupError: If no signal has that id.
        """
        row = await self.get_signal(signal_id)
        row.needs_review = False
        row.match_evidence = {
            **dict(row.match_evidence or {}),
            "dismissed": True,
            "dismissed_at": utcnow().isoformat(),
        }
        await self._session.flush()
        await self._session.commit()
        logger.info("tracking.signal_dismissed", signal_id=str(signal_id))

    # ----------------------------------------------------------------------------------
    # Ghosting — the outcome nobody types in
    # ----------------------------------------------------------------------------------

    async def detect_ghosted(self, user_id: uuid.UUID) -> int:
        """Mark long-silent applications ``ghosted``.

        Most applications end in silence, and silence is data: an application submitted
        ``settings.ghosted_after_days`` ago with nothing heard since is not "still pending",
        it is over. Recording that is what lets the analytics answer the question the product
        exists to answer — which resumes, which companies, which titles actually get replies.

        Silence is measured from the **later** of the submission and the most recent signal
        matched to that application, so an employer who acknowledged and then went quiet
        starts their clock from the acknowledgement.

        Applications whose current status the user set by hand are left alone: they said
        something about this application, and an inference from silence does not outrank it.

        No ``status_signals`` row is written. Every :class:`~app.models.enums.SignalSource`
        names a channel a message arrived on, and this outcome arrives on none of them; the
        :class:`~app.models.application.ApplicationEvent` that
        :meth:`~app.services.application_service.ApplicationService.transition` writes is the
        audit trail.

        Args:
            user_id: The applicant.

        Returns:
            How many applications were moved to ``ghosted``.
        """
        from app.services.application_service import ApplicationService, InvalidTransition

        days = max(1, int(getattr(self.settings, "ghosted_after_days", 45) or 45))
        cutoff = utcnow() - timedelta(days=days)

        latest_signal = (
            select(func.max(StatusSignalRow.received_at))
            .where(StatusSignalRow.application_id == Application.id)
            .scalar_subquery()
        )
        statement: Select[Any] = (
            select(Application)
            .where(
                Application.user_id == user_id,
                Application.status.in_(GHOSTED_ELIGIBLE_STATUSES),
                Application.submitted_at.is_not(None),
                Application.submitted_at <= cutoff,
                or_(latest_signal.is_(None), latest_signal <= cutoff),
            )
            .order_by(Application.submitted_at.asc())
        )
        rows = list((await self._session.execute(statement)).scalars().all())

        service = ApplicationService(self._session)
        ghosted = 0
        for application in rows:
            application.status_source = StatusSource.INFERRED
            application.status_confidence = None
            application.last_synced_at = utcnow()
            try:
                await service.transition(
                    application,
                    ApplicationStatus.GHOSTED,
                    message=f"No response for {days} days.",
                    payload={"ghosted_after_days": days, "inferred": True},
                )
            except InvalidTransition:
                self._session.expire(application)
                continue
            ghosted += 1
            self._publish(
                _EVENT_STATUS_CHANGED,
                {
                    "application_id": str(application.id),
                    "status": ApplicationStatus.GHOSTED.value,
                    "source": StatusSource.INFERRED.value,
                    "confidence": None,
                    "signal_id": None,
                },
            )

        if ghosted:
            logger.info("tracking.ghosted", user_id=str(user_id), count=ghosted, days=days)
        return ghosted

    # ----------------------------------------------------------------------------------
    # Aggregate state
    # ----------------------------------------------------------------------------------

    async def stats(self, user_id: uuid.UUID) -> dict[str, Any]:
        """Return the aggregate the tracking panel renders in one request.

        Args:
            user_id: The applicant.

        Returns:
            A mapping matching :class:`~app.schemas.tracking.TrackingStats`.
        """
        accounts = await self.list_accounts(user_id)
        by_kind_rows = await self._session.execute(
            select(StatusSignalRow.kind, func.count())
            .where(StatusSignalRow.user_id == user_id)
            .group_by(StatusSignalRow.kind)
        )
        by_kind = {SignalKind(kind).value: int(count or 0) for kind, count in by_kind_rows.all()}

        applied = int(
            await self._session.scalar(
                select(func.count(StatusSignalRow.id)).where(
                    StatusSignalRow.user_id == user_id,
                    StatusSignalRow.applied.is_(True),
                )
            )
            or 0
        )
        pending = int(
            await self._session.scalar(
                select(func.count(StatusSignalRow.id)).where(
                    StatusSignalRow.user_id == user_id,
                    StatusSignalRow.needs_review.is_(True),
                    StatusSignalRow.applied.is_(False),
                )
            )
            or 0
        )
        synced = int(
            await self._session.scalar(
                select(func.count(Application.id)).where(
                    Application.user_id == user_id,
                    Application.status_source.in_(
                        (StatusSource.EMAIL, StatusSource.PORTAL, StatusSource.INFERRED)
                    ),
                )
            )
            or 0
        )
        last_sync = max(
            (account.last_sync_at for account in accounts if account.last_sync_at is not None),
            default=None,
        )

        return {
            "accounts": len(accounts),
            "accounts_enabled": sum(1 for account in accounts if account.is_pollable),
            "signals_total": sum(by_kind.values()),
            "signals_applied": applied,
            "signals_pending_review": pending,
            "signals_by_kind": by_kind,
            "applications_synced": synced,
            "last_sync_at": last_sync,
        }

    # ----------------------------------------------------------------------------------
    # Plumbing
    # ----------------------------------------------------------------------------------

    def _tracker(self) -> Any:
        """Resolve the email tracker from the plugin registry.

        Through the registry rather than by importing the class, per golden rule #5. The
        return type is deliberately loose: the registry is typed to
        :class:`~app.plugins.base.Plugin`, and the account-level polling methods this
        service uses are part of :class:`~app.tracking.trackers.email_tracker.EmailTracker`'s
        own surface rather than of the :class:`~app.tracking.base.StatusTracker` contract.

        Returns:
            The shared :class:`~app.tracking.trackers.email_tracker.EmailTracker` instance.

        Raises:
            PluginNotFound: If no tracker is registered under ``"email"``.
        """
        from app.plugins.registry import registry

        return registry.get(PluginKind.TRACKER, EMAIL_TRACKER_NAME)

    @staticmethod
    def _publish(event: str, payload: dict[str, Any]) -> None:
        """Broadcast one ``tracking.*`` event, never failing the sync if it cannot.

        Args:
            event: One of the five §17.7 event names.
            payload: JSON-ready body.
        """
        try:
            from app.api.events import bus

            bus.publish(event, json.loads(json.dumps(payload, default=str)))
        except Exception as exc:  # pragma: no cover - the bus itself never raises
            logger.debug("tracking.publish_failed", event=event, error=str(exc))
