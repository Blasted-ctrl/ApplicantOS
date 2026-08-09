"""Gmail mailbox over the Gmail REST API — ``gmail.readonly`` and nothing else.

Gmail is where most job mail lands, and its API gives this feature two things IMAP cannot:
a real incremental feed (``historyId``) and a search language expressive enough to push the
whole privacy boundary — the window *and* the sender allowlist — onto Google's servers, so
the messages that come back are only ever the ones the query authorised.

Scope
-----

The only scope this adapter needs, requests, or works with is::

    https://www.googleapis.com/auth/gmail.readonly

That scope cannot send, delete, move, label or mark-as-read even if this code tried, which
is the point: privacy invariant §17.8.1 is enforced by Google's own authorisation layer and
not merely by our restraint. The class exposes no method that would attempt a mutation, and
:class:`~app.tracking.email.base.OAuthMailbox` gives it only ``GET``.

Incremental sync
----------------

The cursor is a ``historyId``: Gmail's monotonically increasing mailbox revision number,
captured from ``users.getProfile`` at :meth:`connect` — *before* any message is read, so a
message that arrives mid-sync is picked up by the next run rather than lost between them.

``users.history.list`` then returns only what changed since that point, which turns a
routine poll into one small request. History is retained by Google for about a week, so a
cursor older than that returns ``404``; that is expected rather than exceptional, and the
adapter falls back to the bounded ``q=`` search for the configured window. Re-reading is
free because ``UNIQUE(user_id, source, external_ref)`` de-duplicates on the way in.

Two-phase incremental fetch (§17.8.2)
-------------------------------------

``users.history.list`` returns *ids*, and it cannot be filtered by sender: what changed is
what changed. Turning those ids into messages therefore happens in two phases, and the
split is the privacy control:

1. **Metadata.** Each id is fetched with
   ``format=metadata&metadataHeaders=From,Reply-To,Subject,Date``. Google returns headers
   and ``internalDate`` and **no body at all** — not a payload part, not a snippet — so no
   message content is transferred for mail this adapter is about to discard.
2. **Bodies, for survivors only.** The metadata is checked against
   :meth:`~app.tracking.email.base.MailQuery.matches`, and only the ids inside the window
   *and* inside the sender allowlist are re-fetched with ``format=full``.

Fetching ``format=full`` first and filtering afterwards would pull the body of every
message that arrived in the user's inbox since the last poll — personal mail included —
into this process before rejecting it. The saving is logged as ``gmail.history_two_phase``
(``fetched_metadata`` / ``matched`` / ``fetched_full``). The non-incremental path needs no
such split: ``q=`` already applied both bounds on Google's servers.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

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

__all__ = ["GMAIL_API_BASE", "GMAIL_READONLY_SCOPE", "GmailMailbox"]

logger = structlog.get_logger(__name__)

#: Root of the Gmail REST API for the authenticated user.
GMAIL_API_BASE: Final[str] = "https://gmail.googleapis.com/gmail/v1/users/me"

#: Google's OAuth token endpoint.
GOOGLE_TOKEN_ENDPOINT: Final[str] = "https://oauth2.googleapis.com/token"

#: The sole scope this adapter uses. Read-only by construction: with this scope Google
#: rejects every modify, send, trash and delete call at the API boundary (§17.8.1).
GMAIL_READONLY_SCOPE: Final[str] = "https://www.googleapis.com/auth/gmail.readonly"

#: Message ids requested per ``users.messages.list`` page. Gmail's own maximum is 500.
LIST_PAGE_SIZE: Final[int] = 100

#: Changes requested per ``users.history.list`` page.
HISTORY_PAGE_SIZE: Final[int] = 500

#: Ceiling on list/history pages per run, so a mailbox that never reports exhaustion cannot
#: spin forever.
MAX_PAGES: Final[int] = 50

#: History type requested. Only *new mail* can change an application's status; label edits,
#: deletions and read-state changes are irrelevant and are not asked for.
HISTORY_TYPE_MESSAGE_ADDED: Final[str] = "messageAdded"

#: ``users.messages.get`` projection that returns the whole MIME tree, bodies included.
FORMAT_FULL: Final[str] = "full"

#: ``users.messages.get`` projection that returns headers and ``internalDate`` and no body.
#: Phase one of the incremental path (§17.8.2).
FORMAT_METADATA: Final[str] = "metadata"

#: Headers requested alongside ``format=metadata``. Exactly what
#: :meth:`~app.tracking.email.base.MailQuery.matches` reads (``From`` and ``Date``) plus the
#: two the tracker needs to identify a relayed message (``Reply-To``, ``Subject``). Asking
#: for a named set rather than all headers keeps recipients, routing and tracking headers
#: off the wire as well.
METADATA_HEADERS: Final[tuple[str, ...]] = ("From", "Reply-To", "Subject", "Date")

#: Gmail answers ``404`` when the requested ``startHistoryId`` has aged out of its roughly
#: one-week retention. That is a routine fallback condition, not a failure.
HTTP_NOT_FOUND: Final[int] = 404


class GmailMailbox(OAuthMailbox):
    """A read-only Gmail mailbox (``docs/CONTRACTS.md`` §17.3).

    Satisfies :class:`~app.tracking.email.base.MailBox` structurally. Constructed by the
    email tracker from an ``EmailAccount`` row whose ``provider`` is
    :attr:`~app.models.enums.MailProvider.GMAIL`; never imported elsewhere.

    Users who would rather not run an OAuth flow can use
    :class:`~app.tracking.email.imap.ImapMailbox` against ``imap.gmail.com`` with a Google
    app password instead — same signals, no client registration.
    """

    token_endpoint: ClassVar[str] = GOOGLE_TOKEN_ENDPOINT
    mail_scope: ClassVar[str] = GMAIL_READONLY_SCOPE
    provider_label: ClassVar[str] = "gmail"

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
            client_id: Google OAuth client id; falls back to ``settings.gmail_client_id``.
            client_secret: Google OAuth client secret; falls back to
                ``settings.gmail_client_secret``.
            max_messages: Ceiling on messages per run; defaults to
                ``settings.status_sync_max_messages_per_run``.
            settings: Application settings; resolved from the process singleton when
                ``None``.
        """
        resolved_id = (
            client_id if client_id is not None else getattr(settings, "gmail_client_id", None)
        )
        resolved_secret = (
            client_secret
            if client_secret is not None
            else getattr(settings, "gmail_client_secret", None)
        )
        super().__init__(
            credential_ref=credential_ref,
            client_id=resolved_id,
            client_secret=resolved_secret,
            max_messages=max_messages,
            settings=settings,
        )
        self._history_id: str = ""

    # -- lifecycle ---------------------------------------------------------------------

    async def connect(self) -> None:
        """Obtain an access token and capture the mailbox revision to resume from.

        The ``historyId`` is read *before* any message, so anything arriving during this run
        is picked up by the next one instead of falling between the two.

        Raises:
            MailboxAuthError: If the stored grant was rejected.
            MailboxConfigurationError: If the OAuth client is unconfigured or ``keyring`` or
                ``httpx`` is missing.
            MailboxUnavailableError: If Google could not be reached.
        """
        await super().connect()
        profile = await self._get_json(f"{GMAIL_API_BASE}/profile")
        self._history_id = str(profile.get("historyId") or "")
        self.logger.info("gmail.connected", has_history_id=bool(self._history_id))

    async def cursor(self) -> str:
        """Return the ``historyId`` captured at :meth:`connect`.

        Returns:
            The revision the next incremental run should start from, or ``""`` when the
            profile did not report one.
        """
        return self._history_id

    # -- search ------------------------------------------------------------------------

    async def search(
        self,
        *,
        since: datetime | None = None,
        cursor: str | None = None,
        senders: Sequence[str] | None = None,
    ) -> AsyncIterator[RawMessage]:
        """Yield messages inside the window and inside the sender allowlist, oldest first.

        Two paths, one guarantee. With a usable *cursor* the adapter asks
        ``users.history.list`` for what has changed since that revision — a small request
        that usually returns nothing — and turns those ids into messages in the two phases
        described in the module docstring, so a body is only ever downloaded for a message
        the allowlist already admitted. Otherwise it runs the bounded ``q=`` search built by
        :meth:`_build_q`, which pushes both the window and the allowlist onto Google's
        servers. Either way every candidate is checked against
        :meth:`~app.tracking.email.base.MailQuery.matches` before it is yielded.

        A run that hits ``status_sync_max_messages_per_run`` yields the **oldest** matches
        and logs ``gmail.run_truncated``; the remainder is picked up incrementally, because
        the cursor moves to the revision captured before this run began.

        Args:
            since: Earliest arrival time of interest; clamped to the lookback window.
            cursor: The ``historyId`` returned by :meth:`cursor` after the previous run.
            senders: Sender domains to restrict the search to.

        Yields:
            :class:`~app.tracking.email.base.RawMessage` values, oldest first.

        Raises:
            MailboxQueryError: If the sender allowlist is empty.
            MailboxAuthError: If the grant was rejected.
            MailboxUnavailableError: If Google could not be reached.
        """
        query = build_query(since, senders, settings=self._settings, limit=self._max_messages)

        history_ids: list[str] | None = None
        if cursor and cursor.strip().isdigit():
            history_ids = await self._ids_from_history(cursor.strip())

        collected: list[RawMessage]
        if history_ids is None:
            message_ids = await self._ids_from_search(query)
            collected = []
            for message_id in message_ids:
                message = await self._fetch_message(message_id)
                if message is not None and query.matches(message):
                    collected.append(message)
        else:
            message_ids = history_ids
            collected = await self._fetch_incremental(history_ids, query)

        collected.sort(key=lambda item: item.received_at)
        if len(collected) > query.limit:
            self.logger.warning(
                "gmail.run_truncated",
                matched=len(collected),
                limit=query.limit,
            )
            collected = collected[: query.limit]

        self.logger.info("gmail.searched", fetched=len(collected), candidates=len(message_ids))
        for message in collected:
            yield message

    def _build_q(self, query: MailQuery, domains: Sequence[str]) -> str:
        """Render one Gmail ``q=`` search string.

        Both privacy bounds are expressed server-side: ``after:`` is the lookback window and
        ``from:(… OR …)`` is the sender allowlist. Every domain has already been validated by
        :func:`~app.tracking.email.base.normalize_domain`, so the character set cannot carry
        an operator or a quote into the query.

        Args:
            query: The bounded query.
            domains: One chunk of the allowlist.

        Returns:
            A Gmail search expression.
        """
        senders = " OR ".join(domains)
        return f"after:{query.since.strftime('%Y/%m/%d')} from:({senders})"

    async def _ids_from_search(self, query: MailQuery) -> list[str]:
        """Collect message ids with ``users.messages.list``.

        The allowlist is chunked across several searches rather than truncated, so every
        domain the user applied to is still searched.

        Args:
            query: The bounded query.

        Returns:
            De-duplicated message ids.
        """
        found: dict[str, None] = {}
        for chunk in query.sender_chunks(DEFAULT_SENDERS_PER_QUERY):
            page_token = ""
            for _ in range(MAX_PAGES):
                params: dict[str, Any] = {
                    "q": self._build_q(query, chunk),
                    "maxResults": LIST_PAGE_SIZE,
                }
                if page_token:
                    params["pageToken"] = page_token
                payload = await self._get_json(f"{GMAIL_API_BASE}/messages", params=params)
                for entry in payload.get("messages") or ():
                    identifier = str(entry.get("id") or "")
                    if identifier:
                        found[identifier] = None
                page_token = str(payload.get("nextPageToken") or "")
                if not page_token or len(found) >= query.limit * 2:
                    break
        return list(found)

    async def _ids_from_history(self, start_history_id: str) -> list[str] | None:
        """Collect message ids added since *start_history_id*.

        Args:
            start_history_id: The cursor from the previous run.

        Returns:
            The ids of messages added since that revision, or ``None`` when Gmail no longer
            holds history that far back — which is routine after about a week and means the
            caller should fall back to the bounded search.
        """
        found: dict[str, None] = {}
        page_token = ""
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "startHistoryId": start_history_id,
                "historyTypes": HISTORY_TYPE_MESSAGE_ADDED,
                "maxResults": HISTORY_PAGE_SIZE,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = await self._get_json(f"{GMAIL_API_BASE}/history", params=params)
            except MailboxError as exc:
                if exc.status_code == HTTP_NOT_FOUND:
                    self.logger.info("gmail.history_expired")
                    return None
                raise
            for record in payload.get("history") or ():
                for added in record.get("messagesAdded") or ():
                    identifier = str((added.get("message") or {}).get("id") or "")
                    if identifier:
                        found[identifier] = None
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                break
        return list(found)

    async def _fetch_incremental(
        self, message_ids: Sequence[str], query: MailQuery
    ) -> list[RawMessage]:
        """Turn history ids into messages without downloading unauthorised bodies.

        Phase one fetches every id with ``format=metadata``, which returns no body; phase
        two re-fetches with ``format=full`` only the ids whose metadata satisfied *query*.
        This is privacy invariant §17.8.2 applied to the one path where Gmail cannot apply
        it server-side.

        Args:
            message_ids: Ids reported by ``users.history.list``.
            query: The bounded query — the window, the allowlist and the ceiling.

        Returns:
            Fully-populated messages for the ids that matched, oldest first.
        """
        matched: list[RawMessage] = []
        for message_id in message_ids:
            header_only = await self._fetch_message(message_id, metadata_only=True)
            if header_only is not None and query.matches(header_only):
                matched.append(header_only)

        # Oldest first *before* the ceiling is applied, so a truncated run fetches the
        # bodies of the oldest matches and the newest arrive on the next incremental run.
        matched.sort(key=lambda item: item.received_at)
        if len(matched) > query.limit:
            self.logger.warning("gmail.run_truncated", matched=len(matched), limit=query.limit)
            matched = matched[: query.limit]

        collected: list[RawMessage] = []
        for header_only in matched:
            message = await self._fetch_message(header_only.message_id)
            if message is not None:
                collected.append(message)

        self.logger.info(
            "gmail.history_two_phase",
            fetched_metadata=len(message_ids),
            matched=len(matched),
            fetched_full=len(collected),
        )
        return collected

    async def _fetch_message(
        self, message_id: str, *, metadata_only: bool = False
    ) -> RawMessage | None:
        """Fetch one message and reduce it to a :class:`RawMessage`.

        Args:
            message_id: Gmail's message id.
            metadata_only: When ``True``, request ``format=metadata`` with
                :data:`METADATA_HEADERS` — headers and ``internalDate`` only, no body. The
                resulting :class:`RawMessage` has an empty :attr:`~RawMessage.body`, which
                is all :meth:`~app.tracking.email.base.MailQuery.matches` needs.

        Returns:
            The message, or ``None`` when it has since been removed or could not be read —
            one bad message must not end a sync with hundreds left.
        """
        params: dict[str, Any] = {"format": FORMAT_FULL}
        if metadata_only:
            params = {"format": FORMAT_METADATA, "metadataHeaders": list(METADATA_HEADERS)}
        try:
            payload = await self._get_json(f"{GMAIL_API_BASE}/messages/{message_id}", params=params)
        except MailboxError as exc:
            self.logger.debug(
                "gmail.message_unavailable", message_id=message_id, error=type(exc).__name__
            )
            return None
        if not payload:
            return None
        return self._build_message(payload)

    def _build_message(self, payload: dict[str, Any]) -> RawMessage:
        """Convert a ``users.messages.get`` response into a :class:`RawMessage`.

        Args:
            payload: The decoded API response.

        Returns:
            The provider-neutral message.
        """
        body_payload = payload.get("payload") or {}
        headers = self._headers(body_payload)
        sender_name, sender = self._split_address(headers.get("from", ""))
        _, reply_to = self._split_address(headers.get("reply-to", ""))
        return RawMessage(
            message_id=str(payload.get("id") or ""),
            received_at=self._received_at(payload),
            sender=sender,
            sender_name=sender_name,
            sender_domain=domain_of(sender),
            subject=headers.get("subject", ""),
            body=extract_text(self._body_text(body_payload)),
            reply_to=reply_to,
            folder="gmail",
            raw={"thread_id": str(payload.get("threadId") or "")},
        )

    @staticmethod
    def _headers(body_payload: dict[str, Any]) -> dict[str, str]:
        """Index a Gmail payload's headers by lowercase name.

        Args:
            body_payload: The ``payload`` object of a message resource.

        Returns:
            A mapping of lowercase header name to value.
        """
        return {
            str(item.get("name", "")).lower(): str(item.get("value", ""))
            for item in body_payload.get("headers") or ()
        }

    @staticmethod
    def _split_address(header: str) -> tuple[str, str]:
        """Split a header value into display name and bare address.

        Gmail returns headers already MIME-decoded, so this needs none of the RFC 2047
        handling the IMAP adapter does.

        Args:
            header: A ``From:`` or ``Reply-To:`` value.

        Returns:
            ``(display_name, address)``; either component may be ``""``.
        """
        from email.utils import parseaddr

        name, address = parseaddr(header or "")
        return name.strip(), address.strip().lower()

    @staticmethod
    def _received_at(payload: dict[str, Any]) -> datetime:
        """Read a message's arrival time from ``internalDate``.

        Args:
            payload: The message resource.

        Returns:
            The instant in UTC; the current time when the field is missing or malformed,
            which keeps the message inside the window it was found in.
        """
        raw: Any = payload.get("internalDate")
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
        except (TypeError, ValueError):
            return datetime.now(UTC)

    def _body_text(self, body_payload: dict[str, Any]) -> str:
        """Flatten a Gmail MIME tree to a single string, preferring ``text/plain``.

        Args:
            body_payload: The ``payload`` object of a message resource.

        Returns:
            The plain-text parts joined, or the HTML parts when there is no plain text (the
            caller passes the result through
            :func:`~app.tracking.email.base.extract_text`, which strips markup and applies
            the retention cap). ``""`` when the message carries no text at all.
        """
        plain: list[str] = []
        html_parts: list[str] = []
        stack: list[dict[str, Any]] = [body_payload]
        while stack:
            node = stack.pop()
            for child in node.get("parts") or ():
                if isinstance(child, dict):
                    stack.append(child)
            mime = str(node.get("mimeType") or "")
            if not mime.startswith("text/"):
                continue
            decoded = self._decode_body(node.get("body") or {})
            if not decoded:
                continue
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        if plain:
            return "\n\n".join(plain)
        return "\n".join(html_parts)

    @staticmethod
    def _decode_body(body: dict[str, Any]) -> str:
        """Decode one base64url part body.

        Args:
            body: A message part's ``body`` object.

        Returns:
            The decoded text, or ``""`` for an attachment reference or undecodable data.
        """
        data = body.get("data")
        if not data:
            return ""
        text = str(data)
        padded = text + "=" * (-len(text) % 4)
        try:
            return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
                "utf-8", errors="replace"
            )
        except (ValueError, UnicodeDecodeError):
            return ""
