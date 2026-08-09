"""Generic IMAP mailbox — read-only, incremental, and dependent on nothing but the stdlib.

This is the adapter that makes status sync universal. Gmail and Outlook have REST APIs, but
Fastmail, Proton Bridge, iCloud, a university mail server and a self-hosted Dovecot do not —
and all of them speak IMAP. It also covers Gmail itself for users who would rather generate
an app password than run an OAuth consent flow: ``imap.gmail.com:993`` with an app password
works here with no extra code.

Read-only, three times over
---------------------------

Privacy invariant §17.8.1 is not a policy this module follows, it is the way it is built:

* Every folder is opened with ``select(folder, readonly=True)``, which makes ``imaplib``
  issue **EXAMINE** rather than **SELECT**. In an EXAMINE session the server itself refuses
  any mutating command, so read-only is enforced at the far end of the socket and not only
  by our restraint.
* Messages are fetched with ``BODY.PEEK[]``. A plain ``BODY[]`` fetch sets the ``\\Seen``
  flag — reading the mail would *change* the mail. ``PEEK`` is why a sync leaves no trace in
  the user's unread count.
* Nothing in this module calls, or even imports, a mutating operation. There is no
  ``store``, no ``copy``, no ``expunge``, no ``append``. Closing logs out rather than
  issuing ``CLOSE``, because ``CLOSE`` expunges deleted messages in a read-write session and
  the safest command is the one that is never sent.

Incremental sync and UIDVALIDITY
--------------------------------

The cursor is ``"UIDVALIDITY:UID"`` (``docs/CONTRACTS.md`` §17.2). ``UID`` is the highest
message processed; ``UIDVALIDITY`` is the server's generation counter for that folder.

UIDs are only comparable within one UIDVALIDITY generation. When a folder is recreated, a
mailbox is migrated, or a server rebuilds its index, UIDVALIDITY changes and every stored
UID becomes meaningless — RFC 3501 requires the client to discard them. Trusting a stale UID
after that would silently skip mail, which for this feature means silently losing a
rejection. So a changed UIDVALIDITY drops the UID floor and re-scans the whole lookback
window instead; re-reading is free because ``UNIQUE(user_id, source, external_ref)``
de-duplicates on the way into the database.

Blocking I/O
------------

``imaplib`` is synchronous. Every network call runs in :func:`asyncio.to_thread`, one hop
per folder rather than one per message, so a mailbox sync never blocks the event loop and a
slow server slows only itself.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import structlog

from app.tracking.email.base import (
    DEFAULT_FOLDERS,
    DEFAULT_SENDERS_PER_QUERY,
    MailboxAuthError,
    MailboxConfigurationError,
    MailboxUnavailableError,
    MailQuery,
    RawMessage,
    build_query,
    decode_header_text,
    extract_text,
    load_credential,
    parse_sender,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = [
    "DEFAULT_IMAP_PORT",
    "FETCH_BATCH_SIZE",
    "IMAP_TIMEOUT_SECONDS",
    "ImapMailbox",
]

logger = structlog.get_logger(__name__)

#: Default IMAPS port. The plaintext port (143) is reachable by setting ``use_ssl=False``,
#: which is only ever appropriate against a local bridge such as Proton's.
DEFAULT_IMAP_PORT: Final[int] = 993

#: Socket timeout for every IMAP operation, in seconds.
IMAP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Messages fetched per ``UID FETCH`` command. Large enough that a 200-message sync is eight
#: round trips rather than 200; small enough that one command's response is bounded.
FETCH_BATCH_SIZE: Final[int] = 25

#: IMAP dates are ``DD-Mon-YYYY`` with English month abbreviations. ``strftime("%b")`` is
#: locale-dependent and would emit ``"janv."`` on a French Windows install, producing a
#: search the server rejects — hence an explicit table.
_IMAP_MONTHS: Final[tuple[str, ...]] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

#: Pulls the UID out of a ``FETCH`` response prefix such as ``b'3 (UID 4021 BODY[] {8342}'``.
_UID_IN_RESPONSE: Final[re.Pattern[bytes]] = re.compile(rb"UID\s+(\d+)")

#: Separates folder entries in a multi-folder cursor.
_CURSOR_SEPARATOR: Final[str] = ";"

#: Separates a folder name from its ``UIDVALIDITY:UID`` pair in a multi-folder cursor.
_CURSOR_ASSIGN: Final[str] = "="


def _imap_date(value: datetime) -> str:
    """Format *value* as an IMAP ``SEARCH SINCE`` date.

    Args:
        value: An aware datetime.

    Returns:
        The date as ``DD-Mon-YYYY`` with an English month abbreviation.
    """
    moment = value.astimezone(UTC)
    return f"{moment.day:02d}-{_IMAP_MONTHS[moment.month - 1]}-{moment.year}"


def _or_chain(terms: Sequence[Sequence[str]]) -> list[str]:
    """Combine IMAP search keys with ``OR``, in the prefix form the protocol requires.

    IMAP's ``OR`` takes exactly two operands, so *n* alternatives nest:
    ``OR k1 OR k2 k3``. Building the chain here keeps the sender allowlist expressible as
    one server-side search instead of one search per domain.

    Args:
        terms: Search keys, each already split into its tokens (``["FROM", '"acme.com"']``).

    Returns:
        A flat token list, empty when *terms* is empty.
    """
    if not terms:
        return []
    if len(terms) == 1:
        return list(terms[0])
    return ["OR", *terms[0], *_or_chain(terms[1:])]


class ImapMailbox:
    """A read-only IMAP mailbox (``docs/CONTRACTS.md`` §17.3).

    Satisfies :class:`~app.tracking.email.base.MailBox` structurally; it is constructed by
    the email tracker from an ``EmailAccount`` row and never imported by anything else.

    Example::

        mailbox = ImapMailbox(
            host="imap.gmail.com",
            username="someone@gmail.com",
            credential_ref=account.credential_ref,
        )
        await mailbox.connect()
        try:
            async for message in mailbox.search(since=floor, cursor=account.cursor,
                                                senders=allowlist):
                ...
            account.cursor = await mailbox.cursor()
        finally:
            await mailbox.close()

    Attributes:
        logger: structlog logger bound with the host and folder count — never the address.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        credential_ref: str,
        port: int | None = None,
        use_ssl: bool = True,
        folders: Sequence[str] | None = None,
        max_messages: int | None = None,
        settings: Settings | None = None,
        timeout_seconds: float = IMAP_TIMEOUT_SECONDS,
    ) -> None:
        """Configure the mailbox without connecting or reading the keychain.

        Args:
            host: IMAP server hostname.
            username: Login name, normally the user's email address.
            credential_ref: OS-keychain key holding the password or app password. The
                secret itself is never passed in, never stored on the instance, and never
                written to the database (§17.8.4).
            port: Server port; defaults to :data:`DEFAULT_IMAP_PORT`.
            use_ssl: Whether to connect with implicit TLS. Leave ``True`` unless talking to
                a local bridge.
            folders: Folders to search; defaults to :data:`DEFAULT_FOLDERS` — the inbox
                only, because searching "all mail" is the sweep §17.8.2 forbids.
            max_messages: Ceiling on messages per run; defaults to
                ``settings.status_sync_max_messages_per_run``.
            settings: Application settings, used for the lookback window and the message
                ceiling. Resolved from the process singleton when ``None``.
            timeout_seconds: Socket timeout.

        Raises:
            MailboxConfigurationError: If *host* or *username* is empty.
        """
        self._host = (host or "").strip()
        self._username = (username or "").strip()
        if not self._host or not self._username:
            raise MailboxConfigurationError(
                "an IMAP mailbox needs both a host and a username; set IMAP_HOST or "
                "reconnect the account"
            )
        self._credential_ref = credential_ref
        self._port = int(port) if port else DEFAULT_IMAP_PORT
        self._use_ssl = bool(use_ssl)
        self._folders = tuple(folders) if folders else DEFAULT_FOLDERS
        self._settings = settings
        self._timeout = max(1.0, float(timeout_seconds))
        # Left as ``None`` when unset so that :func:`build_query` resolves the ceiling from
        # ``status_sync_max_messages_per_run`` at search time — one authority for the limit,
        # and no stale copy taken before settings were available.
        self._max_messages = max(1, int(max_messages)) if max_messages else None
        self._connection: Any | None = None
        self._cursors: dict[str, tuple[int, int]] = {}
        self.logger = logger.bind(imap_host=self._host, folders=len(self._folders))

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        username: str,
        credential_ref: str,
        folders: Sequence[str] | None = None,
    ) -> ImapMailbox:
        """Build a mailbox from the generic IMAP settings (``IMAP_HOST``/``PORT``/``USE_SSL``).

        Args:
            settings: Application settings carrying the IMAP server configuration.
            username: Login name for the account.
            credential_ref: OS-keychain key holding the password.
            folders: Folders to search; defaults to the inbox.

        Returns:
            A configured, unconnected mailbox.

        Raises:
            MailboxConfigurationError: If ``imap_host`` is unset.
        """
        host = getattr(settings, "imap_host", None)
        if not host:
            raise MailboxConfigurationError(
                "IMAP_HOST is not set; configure it before connecting a generic IMAP mailbox"
            )
        return cls(
            host=host,
            username=username,
            credential_ref=credential_ref,
            port=getattr(settings, "imap_port", DEFAULT_IMAP_PORT),
            use_ssl=bool(getattr(settings, "imap_use_ssl", True)),
            folders=folders,
            settings=settings,
        )

    # -- lifecycle ---------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the IMAP session and authenticate. Idempotent.

        Raises:
            MailboxAuthError: If the server rejected the credentials.
            MailboxConfigurationError: If ``keyring`` is unavailable or holds no secret.
            MailboxUnavailableError: If the server could not be reached.
        """
        if self._connection is not None:
            return
        await asyncio.to_thread(self._connect_sync)
        self.logger.info("imap.connected", port=self._port, ssl=self._use_ssl)

    def _connect_sync(self) -> None:
        """Blocking half of :meth:`connect`.

        The password is read from the keychain into a local, handed straight to ``login``,
        and never stored on the instance — an adapter that keeps a password in an attribute
        eventually ends up with it in a repr, a traceback or a pickle.

        Raises:
            MailboxAuthError: If ``login`` failed.
            MailboxUnavailableError: If the connection could not be established.
        """
        import imaplib

        password = load_credential(self._credential_ref)
        factory = imaplib.IMAP4_SSL if self._use_ssl else imaplib.IMAP4
        try:
            connection = factory(self._host, self._port, timeout=self._timeout)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxUnavailableError(
                f"could not connect to the IMAP server at {self._host}:{self._port} "
                f"({type(exc).__name__})"
            ) from exc

        try:
            connection.login(self._username, password)
        except imaplib.IMAP4.error as exc:
            raise MailboxAuthError(
                f"the IMAP server at {self._host} rejected the stored credentials; "
                "reconnect the mailbox"
            ) from exc
        except OSError as exc:
            raise MailboxUnavailableError(
                f"the IMAP connection to {self._host} dropped during login ({type(exc).__name__})"
            ) from exc
        self._connection = connection

    async def close(self) -> None:
        """Log out and drop the session. Idempotent, and safe if never connected.

        Deliberately ``logout()`` and never ``CLOSE``: the IMAP ``CLOSE`` command expunges
        messages flagged for deletion in a read-write session. Our sessions are EXAMINE-only
        so it would be harmless, but the command that is never sent cannot be sent by
        mistake after a future edit.
        """
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        await asyncio.to_thread(self._logout_sync, connection)

    def _logout_sync(self, connection: Any) -> None:
        """Blocking half of :meth:`close`; a failing logout is logged, never raised.

        Args:
            connection: The ``imaplib`` connection to release.
        """
        import imaplib

        try:
            connection.logout()
        except (OSError, imaplib.IMAP4.error) as exc:
            self.logger.debug("imap.logout_failed", error=type(exc).__name__)

    # -- cursor ------------------------------------------------------------------------

    async def cursor(self) -> str:
        """Return the ``UIDVALIDITY:UID`` cursor for the run that just completed.

        With a single configured folder the cursor is the bare ``"UIDVALIDITY:UID"`` pair
        fixed by ``docs/CONTRACTS.md`` §17.2. With several, entries are joined as
        ``"INBOX=1:100;Archive=7:42"`` — the same grammar, one entry per folder.

        Returns:
            The cursor, or ``""`` when nothing has been searched yet.
        """
        return self._encode_cursor()

    def _encode_cursor(self) -> str:
        """Render :attr:`_cursors` into the persisted string form."""
        if not self._cursors:
            return ""
        if len(self._folders) == 1:
            folder = self._folders[0]
            pair = self._cursors.get(folder)
            return f"{pair[0]}:{pair[1]}" if pair else ""
        return _CURSOR_SEPARATOR.join(
            f"{folder}{_CURSOR_ASSIGN}{validity}:{uid}"
            for folder, (validity, uid) in sorted(self._cursors.items())
        )

    def _parse_cursor(self, cursor: str | None) -> dict[str, tuple[int, int]]:
        """Parse a persisted cursor back into per-folder ``(uidvalidity, uid)`` pairs.

        Tolerant by design: an unparseable cursor yields ``{}``, which re-scans the lookback
        window rather than skipping mail. A cursor is an optimisation, never a correctness
        dependency.

        Args:
            cursor: The stored cursor string, or ``None``.

        Returns:
            A mapping of folder name to ``(uidvalidity, uid)``.
        """
        text = (cursor or "").strip()
        if not text:
            return {}
        parsed: dict[str, tuple[int, int]] = {}
        for entry in text.split(_CURSOR_SEPARATOR):
            piece = entry.strip()
            if not piece:
                continue
            folder = self._folders[0]
            if _CURSOR_ASSIGN in piece:
                folder, _, piece = piece.partition(_CURSOR_ASSIGN)
                folder = folder.strip()
            validity, _, uid = piece.partition(":")
            try:
                parsed[folder] = (int(validity), int(uid))
            except ValueError:
                self.logger.debug("imap.cursor_unparseable")
                continue
        return parsed

    # -- search ------------------------------------------------------------------------

    async def search(
        self,
        *,
        since: datetime | None = None,
        cursor: str | None = None,
        senders: Sequence[str] | None = None,
    ) -> AsyncIterator[RawMessage]:
        """Yield messages inside the window and inside the sender allowlist, oldest first.

        The search is bounded twice before a single command is sent: :func:`build_query`
        clamps the window to ``status_sync_lookback_days`` and refuses an empty allowlist,
        and the per-folder UID floor from *cursor* narrows it further to what has not been
        read yet.

        When more messages match than :attr:`_max_messages` allows, the **oldest** are taken
        and the cursor advances only to the last one processed — so the next run continues
        from there instead of skipping the remainder (golden rule #8).

        Args:
            since: Earliest arrival time of interest; clamped to the lookback window.
            cursor: The cursor returned by :meth:`cursor` after the previous run.
            senders: Sender domains to restrict the search to.

        Yields:
            :class:`~app.tracking.email.base.RawMessage` values, oldest first within each
            folder.

        Raises:
            MailboxQueryError: If the sender allowlist is empty.
            MailboxUnavailableError: If a folder could not be examined or fetched, or the
                mailbox was never connected.
        """
        query = build_query(
            since,
            senders,
            settings=self._settings,
            limit=self._max_messages,
        )
        saved = self._parse_cursor(cursor)
        remaining = query.limit

        for folder in self._folders:
            if remaining <= 0:
                break
            messages, validity, highest = await asyncio.to_thread(
                self._search_folder_sync, folder, query, saved.get(folder), remaining
            )
            self.logger.info(
                "imap.folder_searched",
                folder=folder,
                fetched=len(messages),
                uidvalidity=validity,
            )
            for message in messages:
                yield message
                remaining -= 1
            if highest:
                self._cursors[folder] = (validity, highest)

    def _require_connection(self) -> Any:
        """Return the live connection.

        Returns:
            The ``imaplib`` connection.

        Raises:
            MailboxUnavailableError: If :meth:`connect` was never called or the session was
                already closed.
        """
        connection = self._connection
        if connection is None:
            raise MailboxUnavailableError(
                "the IMAP mailbox is not connected; call connect() before searching",
                transient=False,
            )
        return connection

    def _search_folder_sync(
        self,
        folder: str,
        query: MailQuery,
        saved: tuple[int, int] | None,
        remaining: int,
    ) -> tuple[list[RawMessage], int, int]:
        """Examine one folder and fetch the matching messages. Blocking.

        Args:
            folder: Folder to examine.
            query: The bounded query.
            saved: The stored ``(uidvalidity, uid)`` pair for this folder, if any.
            remaining: How many more messages this run may yield.

        Returns:
            ``(messages, uidvalidity, highest_uid_processed)``. ``highest_uid_processed`` is
            ``0`` when nothing matched, which leaves the stored cursor untouched.

        Raises:
            MailboxUnavailableError: If the folder could not be examined or searched.
        """
        import imaplib

        connection = self._require_connection()

        try:
            # readonly=True issues EXAMINE, not SELECT: the server itself then refuses
            # every mutating command for the rest of this session (§17.8.1).
            status, _ = connection.select(folder, readonly=True)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxUnavailableError(
                f"could not examine IMAP folder {folder!r} ({type(exc).__name__})"
            ) from exc
        if status != "OK":
            raise MailboxUnavailableError(
                f"the IMAP server refused to examine folder {folder!r} (status {status})"
            )

        validity = self._read_uidvalidity(connection)
        floor_uid = 0
        if saved is not None:
            if saved[0] == validity:
                floor_uid = saved[1]
            else:
                self.logger.info(
                    "imap.uidvalidity_changed",
                    folder=folder,
                    stored=saved[0],
                    current=validity,
                )

        uids = self._search_uids(connection, folder, query, floor_uid)
        if not uids:
            return ([], validity, 0)

        selected = uids[: max(0, remaining)]
        messages = self._fetch_messages(connection, folder, selected)
        highest = max((int(message.message_id) for message in messages), default=0)
        return (messages, validity, highest)

    def _read_uidvalidity(self, connection: Any) -> int:
        """Read the UIDVALIDITY of the folder currently examined.

        Args:
            connection: The live ``imaplib`` connection, with a folder already examined.

        Returns:
            The generation counter, or ``0`` when the server did not report one — which is
            treated as "no comparable cursor" and therefore re-scans the window.
        """
        raw = connection.untagged_responses.get("UIDVALIDITY")
        if not raw:
            return 0
        value = raw[0]
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="ignore")
        try:
            return int(str(value).strip())
        except ValueError:
            return 0

    def _search_uids(
        self,
        connection: Any,
        folder: str,
        query: MailQuery,
        floor_uid: int,
    ) -> list[int]:
        """Run the bounded ``UID SEARCH`` commands and return the matching UIDs.

        The sender allowlist is chunked across several searches rather than truncated, so a
        user with 300 applied-to companies still has all 300 domains searched without any
        single command exceeding a server's line-length limit.

        Args:
            connection: The live connection, with *folder* examined.
            folder: Folder name, for error messages.
            query: The bounded query.
            floor_uid: Highest UID already processed in this UIDVALIDITY generation; ``0``
                for a full re-scan of the window.

        Returns:
            Matching UIDs, ascending and de-duplicated.

        Raises:
            MailboxUnavailableError: If a search command failed.
        """
        import imaplib

        since_token = _imap_date(query.since)
        found: set[int] = set()

        for chunk in query.sender_chunks(DEFAULT_SENDERS_PER_QUERY):
            criteria: list[str] = []
            if floor_uid > 0:
                criteria += ["UID", f"{floor_uid + 1}:*"]
            criteria += ["SINCE", since_token]
            criteria += _or_chain([["FROM", f'"{domain}"'] for domain in chunk])
            try:
                status, data = connection.uid("SEARCH", None, *criteria)
            except (OSError, imaplib.IMAP4.error) as exc:
                raise MailboxUnavailableError(
                    f"IMAP search failed in folder {folder!r} ({type(exc).__name__})"
                ) from exc
            if status != "OK":
                raise MailboxUnavailableError(
                    f"the IMAP server refused a search in folder {folder!r} (status {status})"
                )
            for token in self._flatten_search(data):
                if token > floor_uid:
                    found.add(token)

        return sorted(found)

    @staticmethod
    def _flatten_search(data: Any) -> list[int]:
        """Turn a ``UID SEARCH`` response into a list of integers.

        Args:
            data: The raw response payload — a list of space-separated byte strings.

        Returns:
            Every UID the server reported, ignoring anything unparseable.
        """
        uids: list[int] = []
        for item in data or ():
            if item is None:
                continue
            text = item.decode("ascii", errors="ignore") if isinstance(item, bytes) else str(item)
            for token in text.split():
                try:
                    uids.append(int(token))
                except ValueError:
                    continue
        return uids

    def _fetch_messages(
        self,
        connection: Any,
        folder: str,
        uids: Sequence[int],
    ) -> list[RawMessage]:
        """Fetch and parse the given UIDs with ``BODY.PEEK[]``.

        ``PEEK`` is the whole point: a plain ``BODY[]`` fetch sets ``\\Seen`` and would make
        reading the user's mail modify it. Batching keeps a 200-message sync to eight round
        trips.

        Args:
            connection: The live connection, with *folder* examined.
            folder: Folder name, recorded on each message.
            uids: UIDs to fetch, ascending.

        Returns:
            Parsed messages, in the order the server returned them.

        Raises:
            MailboxUnavailableError: If a fetch command failed.
        """
        import imaplib

        messages: list[RawMessage] = []
        for start in range(0, len(uids), FETCH_BATCH_SIZE):
            batch = uids[start : start + FETCH_BATCH_SIZE]
            request = ",".join(str(uid) for uid in batch)
            try:
                status, data = connection.uid("FETCH", request, "(BODY.PEEK[])")
            except (OSError, imaplib.IMAP4.error) as exc:
                raise MailboxUnavailableError(
                    f"IMAP fetch failed in folder {folder!r} ({type(exc).__name__})"
                ) from exc
            if status != "OK":
                raise MailboxUnavailableError(
                    f"the IMAP server refused a fetch in folder {folder!r} (status {status})"
                )
            for uid, payload in self._iter_fetch(data, batch):
                message = self._build_message(uid, payload, folder)
                if message is not None:
                    messages.append(message)
        return messages

    @staticmethod
    def _iter_fetch(data: Any, requested: Sequence[int]) -> list[tuple[int, bytes]]:
        """Pair each RFC 822 payload in a ``FETCH`` response with its UID.

        ``imaplib`` returns a flat list mixing tuples (prefix, payload) with bare byte
        strings (the trailing ``b')'``). The UID is in the prefix; when a server omits it,
        the requested order is used as a fallback so a message is never dropped for a
        formatting quirk.

        Args:
            data: The raw response payload.
            requested: The UIDs this batch asked for, in order.

        Returns:
            ``(uid, raw_bytes)`` pairs.
        """
        results: list[tuple[int, bytes]] = []
        fallback = iter(requested)
        for item in data or ():
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            prefix, payload = item[0], item[1]
            if not isinstance(payload, bytes | bytearray):
                continue
            uid = 0
            if isinstance(prefix, bytes | bytearray):
                found = _UID_IN_RESPONSE.search(bytes(prefix))
                if found:
                    uid = int(found.group(1))
            if uid == 0:
                uid = next(fallback, 0)
            if uid:
                results.append((uid, bytes(payload)))
        return results

    def _build_message(self, uid: int, payload: bytes, folder: str) -> RawMessage | None:
        """Parse one RFC 822 payload into a :class:`RawMessage`.

        Args:
            uid: The message UID, which becomes its stable external reference.
            payload: Raw message bytes.
            folder: Folder the message came from.

        Returns:
            The parsed message, or ``None`` when the payload could not be parsed at all —
            one malformed message must not end a sync that has hundreds left to read.
        """
        from email import message_from_bytes
        from email.policy import compat32

        try:
            parsed = message_from_bytes(payload, policy=compat32)
        except (TypeError, ValueError):
            self.logger.debug("imap.message_unparseable", message_id=str(uid), folder=folder)
            return None

        sender_name, sender, sender_domain = parse_sender(parsed.get("From", ""))
        _, reply_to, _ = parse_sender(parsed.get("Reply-To", ""))
        return RawMessage(
            message_id=str(uid),
            received_at=self._received_at(parsed),
            sender=sender,
            sender_name=sender_name,
            sender_domain=sender_domain,
            subject=decode_header_text(parsed.get("Subject", "")),
            body=extract_text(parsed),
            reply_to=reply_to,
            folder=folder,
            raw={"message_id_header": str(parsed.get("Message-ID", ""))[:200]},
        )

    @staticmethod
    def _received_at(parsed: Any) -> datetime:
        """Read a message's arrival time from its ``Date`` header.

        Args:
            parsed: The parsed :class:`email.message.Message`.

        Returns:
            The header's instant in UTC. A missing or malformed ``Date`` falls back to the
            current time, which keeps the message inside the window it was found in rather
            than discarding it.
        """
        from email.utils import parsedate_to_datetime

        raw = parsed.get("Date")
        if raw:
            try:
                moment = parsedate_to_datetime(str(raw))
            except (TypeError, ValueError):
                moment = None
            if moment is not None:
                return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
        return datetime.now(UTC)
