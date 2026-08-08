"""The email tracker — one pipeline that covers every ATS at once (``§17``).

Email is the channel, and this is the plugin that reads it. Every applicant-tracking system,
and LinkedIn itself, notifies by email, so a single mailbox reader covers LinkedIn,
Glassdoor, Indeed, Greenhouse, Lever, Ashby, Workday and every system ApplicantOS was never
integrated with — including ones that do not exist yet. That is why §17 makes email the
design premise rather than one source among several, and why Glassdoor (no status surface at
all) and LinkedIn (terms prohibit scraping) are **not scraped** (golden rule #10).

What this module is, and is not
-------------------------------

It is a *reader*. It resolves an ``email_accounts`` row to the right mailbox adapter, hands
that adapter a bounded query, and converts each :class:`~app.tracking.email.base.RawMessage`
into a :class:`~app.tracking.base.StatusSignal`. It does not classify, it does not match, and
it writes nothing to the database — those are
:class:`~app.tracking.classifier.StatusClassifier`,
:class:`~app.tracking.matcher.SignalMatcher` and
:class:`~app.tracking.service.StatusSyncService` respectively, and keeping them apart is what
makes each one testable with a constructor call.

The narrow-search guarantee, concretely
---------------------------------------

§17.8.2 forbids a full mailbox sweep, and :func:`~app.tracking.email.base.build_query`
*raises* rather than build an unbounded query. :meth:`EmailTracker.sender_allowlist` is what
keeps that from being an obstacle: it derives the allowlist from the domains of companies the
user actually applied to, plus :data:`~app.tracking.base.ATS_RELAY_DOMAINS`. The relay entries
are not an optimisation — a Greenhouse rejection arrives from ``no-reply@greenhouse.io`` and
would be invisible to a query built from employer domains alone.

Cursors and resumability
------------------------

Each adapter defines its own opaque cursor (``UIDVALIDITY:UID``, a Gmail ``historyId``, a
Graph delta token). :class:`PollRun` carries it back out of the generator, and the service
persists it **only after the generator has been fully consumed** — a cursor advanced early
would skip mail, and golden rule #8 says a crash resumes rather than restarts. Correctness
does not rest on the cursor in any case: ``UNIQUE(user_id, source, external_ref)`` means a
replayed window inserts nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog
from sqlalchemy import select

from app.models.enums import MailProvider, PluginKind
from app.models.tracking import EmailAccount
from app.plugins.base import PluginMeta
from app.plugins.registry import plugin
from app.tracking.base import (
    ATS_RELAY_DOMAINS,
    StatusSignal,
    StatusTracker,
    TrackerAuthError,
    TrackerConfigurationError,
    TrackerError,
    TrackerUnavailableError,
    is_relay_domain,
)
from app.tracking.email import (
    GmailMailbox,
    ImapMailbox,
    MailboxAuthError,
    MailboxConfigurationError,
    MailboxError,
    OutlookMailbox,
    domain_of,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.tracking.email.base import MailBox, RawMessage

__all__ = ["EMAIL_TRACKER_NAME", "EmailTracker", "PollRun"]

logger = structlog.get_logger(__name__)

#: Registered plugin name. The service resolves the tracker by this string rather than by
#: importing the class (golden rule #5).
EMAIL_TRACKER_NAME: Final[str] = "email"


@dataclass(slots=True)
class PollRun:
    """Out-parameter for one account poll: what the run reached, and how far it got.

    An async generator cannot return a value, and the cursor is only meaningful *after* the
    generator has been drained — a Gmail ``historyId`` captured at connect, an IMAP
    ``UIDVALIDITY:UID`` pair only valid once every folder has been walked. Passing a handle
    in is therefore clearer than stashing the cursor on the tracker, which is a
    registry-held singleton that two syncs may drive at once.

    Attributes:
        cursor: The adapter's resume token after a completed run. ``""`` when the run did
            not complete or the provider has none, in which case the caller must leave the
            stored cursor untouched.
        fetched: Messages the adapter yielded.
        completed: Whether the generator was drained without an error. The service persists
            the cursor only when this is ``True``.
        folders: Folders or labels the account was searched in, for the log line.
    """

    cursor: str = ""
    fetched: int = 0
    completed: bool = False
    folders: tuple[str, ...] = field(default_factory=tuple)


@plugin
class EmailTracker(StatusTracker):
    """Reads application-status signals out of the user's connected mailboxes.

    Registered as ``PluginKind.TRACKER`` under the name ``"email"``. One instance per
    process, held by the registry; it carries no per-run state, so concurrent syncs of
    different accounts do not interfere.

    Read-only by construction (§17.8.1): the three adapters it can build expose no send,
    delete, move or flag-modifying operation, and this module never calls one because none
    exists to call.

    Class attributes:
        meta: Plugin identity.
    """

    meta: ClassVar[PluginMeta] = PluginMeta(
        kind=PluginKind.TRACKER,
        name=EMAIL_TRACKER_NAME,
        version="1.0.0",
        display_name="Email",
        description=(
            "Reads application outcomes from a connected Gmail, Outlook or IMAP mailbox. "
            "Read-only, narrowed to the domains the user applied to plus known ATS relays, "
            "and never storing more than a 500-character excerpt of any message."
        ),
        author="ApplicantOS",
        capabilities=frozenset({"poll", "gmail", "outlook", "imap", "read_only"}),
    )

    # ----------------------------------------------------------------------------------
    # StatusTracker
    # ----------------------------------------------------------------------------------

    async def poll(
        self,
        user_id: uuid.UUID,
        *,
        since: datetime | None = None,
    ) -> AsyncIterator[StatusSignal]:
        """Yield every status signal for *user_id* across all of their enabled mailboxes.

        The whole-user entry point required by :class:`~app.tracking.base.StatusTracker`. It
        opens its own read-only unit of work to find the accounts, because a caller holding
        a tracker from the registry has no session to give it. The service uses
        :meth:`poll_account` instead, so that it can persist a cursor per account.

        A user with no connected mailbox yields nothing and reads nothing — §17.8.6: no
        mailbox is touched until the user explicitly connects one.

        Args:
            user_id: The applicant.
            since: Lower bound on arrival time, clamped to
                ``settings.status_sync_lookback_days`` by the adapters.

        Yields:
            :class:`~app.tracking.base.StatusSignal` values, oldest first within each
            account.
        """
        from app.database.session import session_scope

        async with session_scope() as session:
            accounts = await self.enabled_accounts(user_id, session)
            senders = await self.sender_allowlist(user_id, session)

        for account in accounts:
            run = PollRun()
            try:
                stream = self.poll_account(account, run=run, since=since, senders=senders)
                async for signal in stream:
                    yield signal
            except TrackerError as exc:
                # One unreachable mailbox must not cost the user the others.
                logger.warning(
                    "tracking.account_poll_failed",
                    account_id=str(account.id),
                    provider=str(account.provider),
                    error=str(exc),
                    transient=exc.transient,
                )

    async def healthcheck(self) -> bool:
        """Report whether this tracker is usable.

        Always ``True``. A tracker with no connected mailbox is not unhealthy — it simply
        has nothing to do — and reporting otherwise would make ``GET /ready`` red for every
        user who has not connected one. Per-account reachability is
        :meth:`test_account`'s job, and it is asked explicitly.

        Returns:
            ``True``.
        """
        return True

    # ----------------------------------------------------------------------------------
    # Per-account polling
    # ----------------------------------------------------------------------------------

    async def poll_account(
        self,
        account: EmailAccount,
        *,
        run: PollRun,
        since: datetime | None = None,
        senders: Sequence[str] | None = None,
    ) -> AsyncIterator[StatusSignal]:
        """Yield the signals in one mailbox, and report the resume cursor through *run*.

        Args:
            account: The mailbox row. Must be enabled and carry a ``credential_ref``.
            run: Out-parameter receiving the cursor, the count and the completion flag.
            since: Lower bound on arrival time. ``None`` resumes from ``account.cursor``,
                and either way the adapter clamps to the configured lookback window.
            senders: The sender allowlist. Computed from the account's owner when omitted,
                which costs one extra query — pass it when polling several accounts.

        Yields:
            :class:`~app.tracking.base.StatusSignal` values, oldest first.

        Raises:
            TrackerConfigurationError: If the account has no credential reference, or the
                provider is not configured (missing OAuth client, missing ``keyring``).
            TrackerAuthError: If the mail server rejected the stored credential.
            TrackerUnavailableError: If the mail server could not be reached.
        """
        if not account.is_pollable:
            raise TrackerConfigurationError(
                "this mailbox is not connected: enable it and store its credential in the "
                "OS keychain before polling",
                tracker=EMAIL_TRACKER_NAME,
            )

        allowlist = list(senders) if senders else None
        if allowlist is None:
            from app.database.session import session_scope

            async with session_scope() as session:
                allowlist = await self.sender_allowlist(account.user_id, session)

        mailbox = self._mailbox(account)
        run.folders = tuple(account.folders or ())
        source = MailProvider(account.provider).signal_source()

        try:
            await mailbox.connect()
            async for message in mailbox.search(
                since=since,
                cursor=account.cursor,
                senders=allowlist,
            ):
                run.fetched += 1
                yield self._to_signal(account, message, source)
            run.cursor = await mailbox.cursor()
            run.completed = True
        except MailboxAuthError as exc:
            raise TrackerAuthError(str(exc), tracker=EMAIL_TRACKER_NAME) from exc
        except MailboxConfigurationError as exc:
            raise TrackerConfigurationError(str(exc), tracker=EMAIL_TRACKER_NAME) from exc
        except MailboxError as exc:
            raise TrackerUnavailableError(
                str(exc), tracker=EMAIL_TRACKER_NAME, transient=exc.transient
            ) from exc
        finally:
            await mailbox.close()

        logger.info(
            "tracking.account_polled",
            account_id=str(account.id),
            provider=str(account.provider),
            fetched=run.fetched,
            folders=len(run.folders),
        )

    async def test_account(self, account: EmailAccount) -> tuple[bool, str | None]:
        """Connect to one mailbox and disconnect, without reading a single message.

        Backs ``POST /tracking/accounts/{id}/test``. Deliberately does not search: the
        question the user is asking is "did my credential work?", and answering it must not
        pull mail into memory or advance a cursor.

        Args:
            account: The mailbox to probe.

        Returns:
            ``(ok, error)``. *error* is an operator-facing message when *ok* is ``False``,
            never a token, an address or a body.
        """
        try:
            mailbox = self._mailbox(account)
        except TrackerConfigurationError as exc:
            return False, str(exc)

        try:
            await mailbox.connect()
        except MailboxError as exc:
            logger.info(
                "tracking.account_test_failed",
                account_id=str(account.id),
                provider=str(account.provider),
                error_type=type(exc).__name__,
            )
            return False, str(exc)
        finally:
            await mailbox.close()
        return True, None

    # ----------------------------------------------------------------------------------
    # Queries this tracker needs (reads only)
    # ----------------------------------------------------------------------------------

    @staticmethod
    async def enabled_accounts(
        user_id: uuid.UUID,
        session: AsyncSession,
    ) -> list[EmailAccount]:
        """Return the user's mailboxes the poller is allowed to read.

        Args:
            user_id: The applicant.
            session: A read-only unit of work.

        Returns:
            Enabled accounts that carry a keychain reference, oldest first so that polling
            order is stable across runs.
        """
        rows = await session.execute(
            select(EmailAccount)
            .where(
                EmailAccount.user_id == user_id,
                EmailAccount.enabled.is_(True),
                EmailAccount.credential_ref.is_not(None),
            )
            .order_by(EmailAccount.created_at.asc())
        )
        return list(rows.scalars().all())

    async def sender_allowlist(
        self,
        user_id: uuid.UUID,
        session: AsyncSession,
    ) -> list[str]:
        """Build the sender allowlist that bounds every mailbox query (§17.8.2).

        Two parts, and both are necessary:

        * **The employers the user actually applied to**, by ``companies.domain``. This is
          what makes the query narrow — it can only ever surface mail from organisations
          already in the user's own application history.
        * **Every known ATS relay.** A Greenhouse rejection arrives from
          ``no-reply@greenhouse.io``, a Lever interview invite from ``@hire.lever.co``. Omit
          these and the search misses most outcomes while still reading the mailbox, which
          is the worst of both.

        Args:
            user_id: The applicant.
            session: A read-only unit of work.

        Returns:
            Lowercase domains, relays first so that a chunked query always issues the
            highest-yield chunk. Never empty — the relay list alone guarantees that, which
            is what keeps :func:`~app.tracking.email.base.build_query` from refusing.
        """
        from app.models.application import Application
        from app.models.company import Company

        rows = await session.execute(
            select(Company.domain)
            .join(Application, Application.company_id == Company.id)
            .where(
                Application.user_id == user_id,
                Application.submitted_at.is_not(None),
                Company.domain.is_not(None),
            )
            .distinct()
        )

        ordered: dict[str, None] = dict.fromkeys(sorted(ATS_RELAY_DOMAINS))
        for value in rows.scalars().all():
            domain = str(value or "").strip().lower().lstrip("@").rstrip(".")
            if domain:
                ordered.setdefault(domain, None)
        return list(ordered)

    # ----------------------------------------------------------------------------------
    # Adapter construction and conversion
    # ----------------------------------------------------------------------------------

    def _mailbox(self, account: EmailAccount) -> MailBox:
        """Build the read-only mailbox adapter for one account.

        The one place ``EmailAccount.provider`` becomes a concrete class. The adapters are
        reached through :mod:`app.tracking.email`'s package door rather than by importing
        their modules, so the mail layer stays swappable behind its protocol.

        Args:
            account: The mailbox row.

        Returns:
            A :class:`~app.tracking.email.base.MailBox`, not yet connected.

        Raises:
            TrackerConfigurationError: If the provider is unknown, the account has no
                credential reference, or generic IMAP has no host configured.
        """
        reference = (account.credential_ref or "").strip()
        if not reference:
            raise TrackerConfigurationError(
                "this mailbox has no keychain reference; reconnect the account",
                tracker=EMAIL_TRACKER_NAME,
            )

        provider = MailProvider(account.provider)
        folders = tuple(account.folders or ())
        try:
            if provider is MailProvider.GMAIL:
                return GmailMailbox(credential_ref=reference, settings=self.settings)
            if provider is MailProvider.OUTLOOK:
                return OutlookMailbox(credential_ref=reference, settings=self.settings)
            return ImapMailbox(
                host=self._imap_host(account),
                username=account.address,
                credential_ref=reference,
                port=getattr(self.settings, "imap_port", None),
                use_ssl=bool(getattr(self.settings, "imap_use_ssl", True)),
                folders=folders or None,
                settings=self.settings,
            )
        except MailboxConfigurationError as exc:
            raise TrackerConfigurationError(str(exc), tracker=EMAIL_TRACKER_NAME) from exc

    def _imap_host(self, account: EmailAccount) -> str:
        """Resolve the IMAP host for one account.

        The host is global configuration (``settings.imap_host``) rather than a per-account
        column, deliberately: a desktop install talks to one IMAP server, and a per-account
        hostname would be an unvalidated string that every query is then interpolated into.

        Args:
            account: The mailbox row, used only for the error message.

        Returns:
            The configured host.

        Raises:
            TrackerConfigurationError: If ``IMAP_HOST`` is unset.
        """
        host = str(getattr(self.settings, "imap_host", "") or "").strip()
        if not host:
            raise TrackerConfigurationError(
                "generic IMAP is selected for this mailbox but IMAP_HOST is not set; set it "
                "or reconnect the account through Gmail or Outlook",
                tracker=EMAIL_TRACKER_NAME,
            )
        return host

    @staticmethod
    def _to_signal(account: EmailAccount, message: RawMessage, source: Any) -> StatusSignal:
        """Convert one mailbox message into a transport-shaped signal.

        The only interesting decision here is :attr:`StatusSignal.company_hint`. On mail
        relayed by an ATS the sender domain identifies the *system*, so the employer's name
        survives only in the display name or the reply-to — and the matcher weights that
        hint accordingly. Carrying it through here is what makes a Greenhouse rejection
        matchable at all.

        Args:
            account: The mailbox the message came from.
            message: The adapter's provider-neutral message.
            source: The :class:`~app.models.enums.SignalSource` this mailbox produces.

        Returns:
            The signal. Its ``body`` is memory-only; only the 500-character
            :attr:`~app.tracking.base.StatusSignal.snippet` is ever persisted (§17.8.3).
        """
        reply_to_domain = domain_of(message.reply_to) if message.reply_to else ""
        hint = message.sender_name or ""
        if not hint and is_relay_domain(message.sender_domain) and reply_to_domain:
            # Nothing in the display name, but the reply-to points at the employer: the
            # domain label is a usable name hint ("acme" from careers@acme.com).
            hint = reply_to_domain.split(".")[0]

        return StatusSignal(
            source=source,
            external_ref=message.message_id,
            received_at=message.received_at,
            sender=message.sender,
            sender_domain=message.sender_domain,
            subject=message.subject,
            body=message.body,
            company_hint=hint or None,
            raw={
                "account_id": str(account.id),
                "folder": message.folder,
                "sender_name": message.sender_name,
                "reply_to_domain": reply_to_domain,
            },
        )
