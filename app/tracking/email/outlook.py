"""Outlook / Microsoft 365 mailbox over Microsoft Graph — ``Mail.Read`` and nothing else.

Graph gives this feature the same two things the Gmail API does: a real incremental feed
(delta tokens) and a search language expressive enough to push both privacy bounds — the
lookback window and the sender allowlist — onto Microsoft's servers.

Scope
-----

The only mailbox permission this adapter requests or works with is::

    https://graph.microsoft.com/Mail.Read

``Mail.Read`` cannot send, delete, move, copy or flag a message; Graph rejects those calls at
the authorisation layer regardless of what any client asks for, which is privacy invariant
§17.8.1 enforced at the far end of the connection. The refresh grant additionally carries
``offline_access``, which is not a mailbox permission at all — it only allows an access token
to be renewed without prompting the user again. There is no ``Mail.ReadWrite`` anywhere in
this package.

Incremental sync
----------------

The cursor is a delta token. :meth:`OutlookMailbox.connect` asks for one with
``$deltatoken=latest``, which returns the *current* synchronisation state and no messages —
so it is captured before anything is read, and mail arriving mid-run belongs to the next run
rather than falling between the two.

Delta and ``$search`` cannot be combined in one Graph request, so the two paths differ in
where the allowlist is applied:

* **First run, or an expired token.** ``$search`` carries a KQL expression holding both the
  sender allowlist and the received-date floor. Nothing outside the query ever leaves
  Microsoft's servers.
* **Incremental run.** The delta feed returns what changed in the inbox since the token,
  which Graph cannot narrow by sender. Every message is therefore re-checked against
  :meth:`~app.tracking.email.base.MailQuery.matches` **inside this adapter**, and anything
  outside the window or the allowlist is discarded before it reaches the tracker. That keeps
  §17.8.2's guarantee — no unauthorised message is ever handed to anything that could
  persist it — while paying one delta request instead of a full re-scan.

A delta token that Graph no longer recognises answers ``410 Gone``; that is a routine
condition after a long pause, and the adapter falls back to the bounded ``$search``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import parse_qs, urlsplit

import structlog

from app.tracking.email.base import (
    DEFAULT_SENDERS_PER_QUERY,
    MailboxError,
    MailQuery,
    OAuthMailbox,
    RawMessage,
    build_query,
    domain_of,
    extract_text,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = ["GRAPH_API_BASE", "OUTLOOK_MAIL_READ_SCOPE", "OutlookMailbox"]

logger = structlog.get_logger(__name__)

#: Root of the Microsoft Graph API for the signed-in user.
GRAPH_API_BASE: Final[str] = "https://graph.microsoft.com/v1.0/me"

#: Microsoft's common-tenant OAuth token endpoint.
MICROSOFT_TOKEN_ENDPOINT: Final[str] = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

#: The sole mailbox permission this adapter uses. Read-only by construction (§17.8.1).
OUTLOOK_MAIL_READ_SCOPE: Final[str] = "https://graph.microsoft.com/Mail.Read"

#: Scope string sent with the refresh grant. ``offline_access`` is not a mailbox permission —
#: it grants nothing inside the mailbox and exists only so an access token can be renewed
#: without prompting the user again.
OUTLOOK_REFRESH_SCOPE: Final[str] = f"offline_access {OUTLOOK_MAIL_READ_SCOPE}"

#: Fields requested for every message. Asking for exactly what is used is the retention
#: guarantee applied at the wire: attachments, recipients and message headers are never
#: transferred because they are never selected.
GRAPH_SELECT_FIELDS: Final[str] = (
    "id,receivedDateTime,subject,from,replyTo,body,bodyPreview,internetMessageId"
)

#: Messages per page. Graph's practical maximum for ``$search`` is well above this; smaller
#: pages keep one response bounded.
PAGE_SIZE: Final[int] = 50

#: Ceiling on pages per run, so a feed that never reports exhaustion cannot spin forever.
MAX_PAGES: Final[int] = 50

#: Graph answers ``410 Gone`` when a delta token is too old to resume from. Routine.
HTTP_GONE: Final[int] = 410

#: Asks Graph to convert HTML bodies to text server-side. When honoured it removes the need
#: to strip markup at all; :func:`~app.tracking.email.base.extract_text` still runs, so a
#: server that ignores the preference changes nothing.
BODY_TEXT_PREFERENCE: Final[dict[str, str]] = {"Prefer": 'outlook.body-content-type="text"'}


class OutlookMailbox(OAuthMailbox):
    """A read-only Outlook / Microsoft 365 mailbox (``docs/CONTRACTS.md`` §17.3).

    Satisfies :class:`~app.tracking.email.base.MailBox` structurally. Constructed by the
    email tracker from an ``EmailAccount`` row whose ``provider`` is
    :attr:`~app.models.enums.MailProvider.OUTLOOK`; never imported elsewhere.

    A personal ``outlook.com`` account can also be read through
    :class:`~app.tracking.email.imap.ImapMailbox` with an app password, which needs no
    application registration.
    """

    token_endpoint: ClassVar[str] = MICROSOFT_TOKEN_ENDPOINT
    mail_scope: ClassVar[str] = OUTLOOK_MAIL_READ_SCOPE
    provider_label: ClassVar[str] = "outlook"

    def __init__(
        self,
        *,
        credential_ref: str,
        client_id: str | None = None,
        client_secret: str | None = None,
        max_messages: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Configure the adapter without touching the network or the keychain.

        Args:
            credential_ref: OS-keychain key holding the OAuth token blob (§17.8.4).
            client_id: Azure application id; falls back to ``settings.outlook_client_id``.
            client_secret: Azure client secret; falls back to
                ``settings.outlook_client_secret``.
            max_messages: Ceiling on messages per run; defaults to
                ``settings.status_sync_max_messages_per_run``.
            settings: Application settings; resolved from the process singleton when
                ``None``.
        """
        resolved_id = (
            client_id if client_id is not None else getattr(settings, "outlook_client_id", None)
        )
        resolved_secret = (
            client_secret
            if client_secret is not None
            else getattr(settings, "outlook_client_secret", None)
        )
        super().__init__(
            credential_ref=credential_ref,
            client_id=resolved_id,
            client_secret=resolved_secret,
            max_messages=max_messages,
            settings=settings,
        )
        self._delta_token: str = ""

    def _refresh_payload(self, refresh_token: str) -> dict[str, str]:
        """Return the refresh-grant form body, which Microsoft requires a scope on.

        Args:
            refresh_token: The keychain-held refresh token.

        Returns:
            The form fields, carrying ``Mail.Read`` and ``offline_access`` and no other
            permission.
        """
        payload = super()._refresh_payload(refresh_token)
        payload["scope"] = OUTLOOK_REFRESH_SCOPE
        return payload

    # -- lifecycle ---------------------------------------------------------------------

    async def connect(self) -> None:
        """Obtain an access token and capture the delta state to resume from.

        ``$deltatoken=latest`` returns the current synchronisation state and **no messages**,
        so the cursor is captured before anything is read and a message arriving mid-run
        belongs to the next run rather than to neither.

        Raises:
            MailboxAuthError: If the stored grant was rejected.
            MailboxConfigurationError: If the OAuth client is unconfigured or ``keyring`` or
                ``httpx`` is missing.
            MailboxUnavailableError: If Graph could not be reached.
        """
        await super().connect()
        try:
            payload = await self._get_json(
                self._delta_url(), params={"$deltatoken": "latest"}
            )
        except MailboxError as exc:
            self.logger.debug("outlook.delta_token_unavailable", error=type(exc).__name__)
            return
        self._delta_token = self._token_from_link(str(payload.get("@odata.deltaLink") or ""))
        self.logger.info("outlook.connected", has_delta_token=bool(self._delta_token))

    async def cursor(self) -> str:
        """Return the delta token for the next incremental run.

        Returns:
            The token, or ``""`` when Graph did not issue one.
        """
        return self._delta_token

    # -- search ------------------------------------------------------------------------

    async def search(
        self,
        *,
        since: datetime | None = None,
        cursor: str | None = None,
        senders: Sequence[str] | None = None,
    ) -> AsyncIterator[RawMessage]:
        """Yield messages inside the window and inside the sender allowlist, oldest first.

        With a usable *cursor* the inbox delta feed supplies only what changed; otherwise the
        bounded ``$search`` runs. Both paths end at the same filter — every message is
        checked against :meth:`~app.tracking.email.base.MailQuery.matches` before it is
        yielded, so nothing the query did not authorise leaves this adapter.

        A run that hits ``status_sync_max_messages_per_run`` yields the **oldest** matches
        and logs ``outlook.run_truncated``; the rest arrives incrementally, because the
        delta token moves to the state captured before this run began.

        Args:
            since: Earliest arrival time of interest; clamped to the lookback window.
            cursor: The delta token returned by :meth:`cursor` after the previous run.
            senders: Sender domains to restrict the search to.

        Yields:
            :class:`~app.tracking.email.base.RawMessage` values, oldest first.

        Raises:
            MailboxQueryError: If the sender allowlist is empty.
            MailboxAuthError: If the grant was rejected.
            MailboxUnavailableError: If Graph could not be reached.
        """
        query = build_query(since, senders, settings=self._settings, limit=self._max_messages)

        candidates: list[dict[str, Any]] | None = None
        if cursor and cursor.strip():
            candidates = await self._delta_page_through(cursor.strip(), query)
        if candidates is None:
            candidates = await self._search_page_through(query)

        collected: list[RawMessage] = []
        seen: set[str] = set()
        for payload in candidates:
            message = self._build_message(payload)
            if message.message_id in seen or not query.matches(message):
                continue
            seen.add(message.message_id)
            collected.append(message)

        collected.sort(key=lambda item: item.received_at)
        if len(collected) > query.limit:
            self.logger.warning(
                "outlook.run_truncated", matched=len(collected), limit=query.limit
            )
            collected = collected[: query.limit]

        self.logger.info(
            "outlook.searched", fetched=len(collected), candidates=len(candidates)
        )
        for message in collected:
            yield message

    # -- Graph queries -------------------------------------------------------------------

    @staticmethod
    def _delta_url() -> str:
        """Return the inbox delta endpoint.

        The inbox only, never ``/me/messages``: searching every folder in the mailbox would
        be the sweep §17.8.2 forbids, and job mail lands in the inbox.
        """
        return f"{GRAPH_API_BASE}/mailFolders/inbox/messages/delta"

    @staticmethod
    def _token_from_link(link: str) -> str:
        """Extract the ``$deltatoken`` value from a Graph delta link.

        Args:
            link: An ``@odata.deltaLink`` URL.

        Returns:
            The token, or ``""`` when the link carries none.
        """
        if not link:
            return ""
        values = parse_qs(urlsplit(link).query).get("$deltatoken")
        return values[0] if values else ""

    def _build_search(self, query: MailQuery, domains: Sequence[str]) -> str:
        """Render one KQL ``$search`` expression.

        Both privacy bounds are expressed server-side: ``from:`` alternatives are the sender
        allowlist and ``received>=`` is the lookback floor. Every domain has already been
        validated by :func:`~app.tracking.email.base.normalize_domain`, so no allowlist entry
        can carry a KQL operator or a quote into the expression.

        Args:
            query: The bounded query.
            domains: One chunk of the allowlist.

        Returns:
            A quoted KQL expression, ready to be sent as ``$search``.
        """
        senders = " OR ".join(f"from:{domain}" for domain in domains)
        return f'"({senders}) AND received>={query.since_date}"'

    async def _search_page_through(self, query: MailQuery) -> list[dict[str, Any]]:
        """Collect message payloads with a bounded ``$search``.

        The allowlist is chunked across several searches rather than truncated, so every
        domain the user applied to is still searched.

        Args:
            query: The bounded query.

        Returns:
            Raw message payloads, possibly containing duplicates across chunks (the caller
            de-duplicates by id).
        """
        collected: list[dict[str, Any]] = []
        for chunk in query.sender_chunks(DEFAULT_SENDERS_PER_QUERY):
            url = f"{GRAPH_API_BASE}/mailFolders/inbox/messages"
            params: dict[str, Any] | None = {
                "$search": self._build_search(query, chunk),
                "$select": GRAPH_SELECT_FIELDS,
                "$top": PAGE_SIZE,
            }
            for _ in range(MAX_PAGES):
                payload = await self._get_json(url, params=params, headers=BODY_TEXT_PREFERENCE)
                collected.extend(self._values(payload))
                next_link = str(payload.get("@odata.nextLink") or "")
                if not next_link or len(collected) >= query.limit * 2:
                    break
                url, params = next_link, None
        return collected

    async def _delta_page_through(
        self, token: str, query: MailQuery
    ) -> list[dict[str, Any]] | None:
        """Collect message payloads from the inbox delta feed.

        Args:
            token: The delta token from the previous run.
            query: The bounded query, used only to cap the volume collected — the allowlist
                itself is applied by the caller, because Graph cannot express it here.

        Returns:
            Raw message payloads, or ``None`` when Graph no longer recognises the token and
            the caller should fall back to the bounded ``$search``.
        """
        collected: list[dict[str, Any]] = []
        url = self._delta_url()
        params: dict[str, Any] | None = {"$deltatoken": token, "$select": GRAPH_SELECT_FIELDS}
        for _ in range(MAX_PAGES):
            try:
                payload = await self._get_json(url, params=params, headers=BODY_TEXT_PREFERENCE)
            except MailboxError as exc:
                if exc.status_code == HTTP_GONE:
                    self.logger.info("outlook.delta_token_expired")
                    return None
                raise
            collected.extend(
                item for item in self._values(payload) if "@removed" not in item
            )
            delta_link = str(payload.get("@odata.deltaLink") or "")
            if delta_link:
                self._delta_token = self._token_from_link(delta_link) or self._delta_token
            next_link = str(payload.get("@odata.nextLink") or "")
            if not next_link or len(collected) >= query.limit * 2:
                break
            url, params = next_link, None
        return collected

    @staticmethod
    def _values(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the ``value`` array of a Graph collection response.

        Args:
            payload: A decoded Graph response.

        Returns:
            The contained objects, ignoring anything that is not one.
        """
        return [item for item in payload.get("value") or () if isinstance(item, dict)]

    # -- message conversion ---------------------------------------------------------------

    def _build_message(self, payload: dict[str, Any]) -> RawMessage:
        """Convert one Graph message resource into a :class:`RawMessage`.

        Args:
            payload: A decoded message object.

        Returns:
            The provider-neutral message.
        """
        sender_name, sender = self._address(payload.get("from"))
        _, reply_to = self._address(self._first(payload.get("replyTo")))
        body = payload.get("body") or {}
        content = str(body.get("content") or "") or str(payload.get("bodyPreview") or "")
        return RawMessage(
            message_id=str(payload.get("id") or ""),
            received_at=self._received_at(payload),
            sender=sender,
            sender_name=sender_name,
            sender_domain=domain_of(sender),
            subject=str(payload.get("subject") or ""),
            body=extract_text(content),
            reply_to=reply_to,
            folder="inbox",
            raw={"internet_message_id": str(payload.get("internetMessageId") or "")[:200]},
        )

    @staticmethod
    def _first(value: Any) -> Any:
        """Return the first element of a Graph array-valued property, or ``None``."""
        if isinstance(value, list) and value:
            return value[0]
        return None

    @staticmethod
    def _address(recipient: Any) -> tuple[str, str]:
        """Unwrap Graph's nested recipient shape into ``(display_name, address)``.

        Args:
            recipient: A ``{"emailAddress": {"name": …, "address": …}}`` object, or ``None``.

        Returns:
            ``(display_name, address)``; either component may be ``""``.
        """
        if not isinstance(recipient, dict):
            return ("", "")
        inner = recipient.get("emailAddress")
        if not isinstance(inner, dict):
            return ("", "")
        return (
            str(inner.get("name") or "").strip(),
            str(inner.get("address") or "").strip().lower(),
        )

    @staticmethod
    def _received_at(payload: dict[str, Any]) -> datetime:
        """Read a message's arrival time from ``receivedDateTime``.

        Args:
            payload: A message resource.

        Returns:
            The instant in UTC; the current time when the field is missing or malformed,
            which keeps the message inside the window it was found in.
        """
        raw = str(payload.get("receivedDateTime") or "")
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        return datetime.now(UTC)
