"""The mailbox boundary — one read-only shape for Gmail, Outlook and generic IMAP.

Email is the channel (``docs/CONTRACTS.md`` §17). Every ATS, and LinkedIn itself, notifies
by email, so a single well-built mail pipeline covers LinkedIn, Glassdoor, Indeed and every
ATS at once — including the ones ApplicantOS never integrated. This module is that
pipeline's front door: the :class:`MailBox` protocol the three adapters implement, the
:class:`RawMessage` they yield, and the shared machinery that makes all three obey the same
rules.

Nothing here imports the ORM, the enums, or a third-party package at module scope. A
mailbox adapter is pure I/O over a network protocol, and it is testable — and *auditable* —
with nothing but a fake socket.

The six privacy invariants, and where each one is implemented
-------------------------------------------------------------

``docs/CONTRACTS.md`` §17.8 is non-negotiable, so it is worth being explicit about which
line of code enforces which clause:

1. **Read-only, always.** No send, delete, move, copy, flag or expunge call exists anywhere
   in ``app/tracking/`` — the methods are not even imported, so a reviewer can prove it with
   one ``grep``. :class:`~app.tracking.email.imap.ImapMailbox` opens every folder with
   ``readonly=True`` and fetches with ``BODY.PEEK[]`` so that reading does not so much as
   set the ``\\Seen`` flag; the OAuth adapters request ``gmail.readonly`` and ``Mail.Read``
   and nothing else.
2. **Narrow queries, never a full mailbox sweep.** :func:`build_query` is the only way to
   construct a search, it clamps the window to ``settings.status_sync_lookback_days``, and
   it **raises** rather than return a query with an empty sender allowlist. There is no
   code path that asks a mail server for "everything".
3. **Minimal retention.** These adapters return in-memory objects. :attr:`RawMessage.body`
   is capped at :data:`MAX_BODY_CHARS` on construction — nothing downstream needs a 200KB
   marketing email — and the service persists only a 500-character snippet.
4. **Credentials never touch the database.** :func:`load_credential` reads the OS keychain
   through ``keyring``, keyed on the ``credential_ref`` stored on the account row. When
   ``keyring`` is missing it raises and says how to install it; it never falls back to a
   file, an environment variable, or a column.
5. **Never log a body, an address, or a token.** :meth:`RawMessage.log_context` returns the
   message id and folder and nothing else, and every log call in this package uses it or
   logs bare counts.
6. **Nothing is read until the user connects a mailbox.** These classes have no discovery
   and no defaults that would find a mailbox on their own: an adapter is constructed from an
   ``EmailAccount`` row that only exists because the user created it.

Cursors
-------

Each adapter defines its own opaque cursor string, stored on ``email_accounts.cursor``:
``UIDVALIDITY:UID`` for IMAP, a ``historyId`` for Gmail, a delta token for Graph. The
contract they share is that a cursor is *advanced only for work that completed*, so an
interrupted sync resumes instead of restarting (golden rule #8), and that an
unrecognisable cursor degrades to a full re-scan of the lookback window rather than to
silently skipped mail.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    import httpx

    from app.config.settings import Settings

__all__ = [
    "DEFAULT_FOLDERS",
    "DEFAULT_SENDERS_PER_QUERY",
    "KEYRING_SERVICE",
    "MAX_BODY_CHARS",
    "MAX_PART_BYTES",
    "MAX_SENDER_DOMAINS",
    "MAX_SUBJECT_CHARS",
    "MailBox",
    "MailQuery",
    "MailboxAuthError",
    "MailboxConfigurationError",
    "MailboxError",
    "MailboxQueryError",
    "MailboxUnavailableError",
    "OAuthMailbox",
    "RawMessage",
    "build_query",
    "chunked",
    "decode_header_text",
    "domain_allowed",
    "domain_of",
    "extract_text",
    "load_credential",
    "load_credential_json",
    "normalize_domain",
    "parse_sender",
    "resolve_since",
    "strip_html",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Hard cap on the message text an adapter hands back. Classification reads a subject and a
#: few sentences; the LLM never sees more than 2,000 characters (``§17.4``). Everything past
#: this is quoted threads, tracking pixels and HTML boilerplate, and carrying it would cost
#: memory on every message of every sync for no gain.
MAX_BODY_CHARS: Final[int] = 20_000

#: Cap on one MIME part before decoding. A part larger than this is an attachment or an
#: image in disguise; skipping it keeps a single message from dominating a sync.
MAX_PART_BYTES: Final[int] = 512_000

#: Cap on a retained subject line. Subjects are short; anything longer is an attack on the
#: log line or the review screen.
MAX_SUBJECT_CHARS: Final[int] = 500

#: Ceiling on the sender allowlist for one query. A user with more applied-to companies than
#: this still gets every one of them searched — the adapters chunk the allowlist across
#: several queries — but a single query never grows without bound.
MAX_SENDER_DOMAINS: Final[int] = 500

#: Sender domains per individual server-side query. IMAP ``SEARCH`` nests ``OR`` terms in
#: prefix form and every mail server has a command-length limit; Gmail's ``q=`` and Graph's
#: ``$search`` have their own. Twenty-five is comfortably inside all three.
DEFAULT_SENDERS_PER_QUERY: Final[int] = 25

#: Folders searched when an account names none. Job mail lands in the inbox; searching
#: "all mail" would be the full sweep §17.8.2 forbids.
DEFAULT_FOLDERS: Final[tuple[str, ...]] = ("INBOX",)

#: Keychain service name under which every ApplicantOS mailbox secret is stored. The
#: ``credential_ref`` column holds the *username* half of the keychain entry; this is the
#: service half. One authority, so the writer and this reader cannot drift apart.
KEYRING_SERVICE: Final[str] = "applicantos"

#: Fallback lookback when settings cannot be read (a unit test constructing an adapter with
#: no settings). Deliberately the same 120 days as ``status_sync_lookback_days``.
DEFAULT_LOOKBACK_DAYS: Final[int] = 120

#: Fallback message ceiling, mirroring ``status_sync_max_messages_per_run``.
DEFAULT_MAX_MESSAGES: Final[int] = 500

#: Default HTTP timeout for the two OAuth adapters, in seconds.
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Sent on every outbound request the mail adapters make. Honest and identifiable, exactly
#: as ``app/jobs/base.py`` is: an operator reading their audit log should be able to tell
#: what this traffic is.
USER_AGENT: Final[str] = "ApplicantOS/0.1 (+application-status-sync; read-only)"

#: A syntactically plausible DNS name. Enforced on every allowlist entry so that a domain
#: can be interpolated into an IMAP ``SEARCH`` term or a Gmail ``q=`` string without any
#: possibility of quote injection — the character set simply cannot express one.
_DOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)

#: HTML comments, removed before tag stripping because a comment may legally contain ``>``.
_HTML_COMMENT: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)

#: Elements whose *content* is not prose. Dropped whole, not merely untagged.
_HTML_NOISE: Final[re.Pattern[str]] = re.compile(
    r"<(script|style|head|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)

#: Tags that end a visual line. Turned into newlines so a stripped table does not become one
#: 4,000-character sentence.
_HTML_BREAK: Final[re.Pattern[str]] = re.compile(
    r"<\s*/?\s*(br|p|div|tr|li|h[1-6]|table|blockquote)\b[^>]*>", re.IGNORECASE
)

#: Any remaining tag.
_HTML_TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]*>", re.DOTALL)

#: Three or more consecutive newlines, collapsed to a paragraph break.
_BLANK_LINES: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

#: Runs of horizontal whitespace, collapsed to one space.
_SPACES: Final[re.Pattern[str]] = re.compile(r"[^\S\n]+")

#: Cheap test for "this string is markup, not prose".
_LOOKS_LIKE_HTML: Final[re.Pattern[str]] = re.compile(
    r"<\s*(html|body|div|table|br|p|a|span|img)\b", re.IGNORECASE
)


# ======================================================================================
# Errors
# ======================================================================================


class MailboxError(Exception):
    """Base class for every mailbox failure.

    Kept independent of :class:`app.tracking.base.TrackerError` on purpose: this package
    knows about mail servers, not about plugins or applications, and the tracker that owns
    a mailbox is the right place to translate one vocabulary into the other. It also means
    these adapters import nothing from the tracking core, so they can be exercised on their
    own.

    Attributes:
        transient: Whether retrying later is likely to succeed.
        status_code: HTTP status that produced the failure, when one did. Lets a caller
            branch on a *specific* condition — Gmail's ``404`` for expired history is a
            routine fallback, not an error — without parsing an English message.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        status_code: int | None = None,
    ) -> None:
        """Record the failure and whether it is worth retrying.

        Args:
            message: Operator-facing explanation. Never include a message body, an address
                or a token — this string is logged and returned by the API.
            transient: Whether a later retry is worth attempting.
            status_code: HTTP status that produced the failure, when applicable.
        """
        super().__init__(message)
        self.transient = transient
        self.status_code = status_code


class MailboxAuthError(MailboxError):
    """The mail server rejected our credentials.

    A revoked OAuth grant, an expired refresh token, or a rotated IMAP app password. Never
    transient: the account is disabled and the user is asked to reconnect, because retrying
    a bad credential in a loop is how mailboxes get locked out.
    """


class MailboxUnavailableError(MailboxError):
    """The mail server could not be reached, or refused to answer right now.

    Network failures, timeouts, 5xx responses and rate limits. Transient by default: the
    cursor was not advanced, so the next poll resumes from the same place.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = True,
        status_code: int | None = None,
    ) -> None:
        """Construct an availability failure, transient unless told otherwise.

        Args:
            message: Operator-facing explanation.
            transient: Whether a later retry is worth attempting.
            status_code: HTTP status that produced the failure, when applicable.
        """
        super().__init__(message, transient=transient, status_code=status_code)


class MailboxConfigurationError(MailboxError):
    """The adapter cannot run because of how it is configured, not because of a fault.

    A missing OAuth client id, an unset IMAP host, or an absent ``keyring`` package. The fix
    is an operator action, so this never retries and the message says exactly what to do.
    """


class MailboxQueryError(MailboxError):
    """A search was requested that this package refuses to perform.

    Raised by :func:`build_query` when the sender allowlist is empty. Privacy invariant
    §17.8.2 forbids an unbounded mailbox sweep, and "the caller forgot to pass an
    allowlist" must fail loudly rather than quietly become one.
    """


# ======================================================================================
# Address and header helpers
# ======================================================================================


def normalize_domain(value: str) -> str:
    """Reduce *value* to a bare, validated, lowercase DNS name.

    Accepts the shapes an allowlist actually arrives in — ``"Acme.COM"``,
    ``"@acme.com"``, ``"careers@acme.com"``, ``"https://acme.com/jobs"``, ``"www.acme.com"``,
    ``"acme.com."`` — and returns ``"acme.com"`` for all of them. Anything that is not a
    syntactically valid domain returns ``""`` so the caller can drop it, which is what keeps
    a malformed company website out of a mail server query.

    Args:
        value: A domain, an address, or a URL.

    Returns:
        The normalised domain, or ``""`` when *value* does not contain one.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.split(":", 1)[0].strip().rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text if _DOMAIN_PATTERN.match(text) else ""


def domain_of(address: str) -> str:
    """Return the normalised domain of an email address.

    Args:
        address: A bare address such as ``"no-reply@greenhouse.io"``.

    Returns:
        The lowercase domain, or ``""`` when the address carries none.
    """
    return normalize_domain(address)


def parse_sender(header: str) -> tuple[str, str, str]:
    """Split a ``From:`` header into display name, address and domain.

    The display name matters: mail from an ATS relay carries the *employer* there
    (``"Acme Corp <no-reply@greenhouse.io>"``), which is the only place the company name
    appears when the sender domain identifies the ATS instead (``docs/CONTRACTS.md`` §17.5).

    Args:
        header: A raw ``From:`` or ``Reply-To:`` header value, possibly MIME-encoded.

    Returns:
        ``(display_name, address, domain)``. Any component that is absent is ``""``.
    """
    from email.utils import parseaddr

    decoded = decode_header_text(header)
    name, address = parseaddr(decoded)
    address = address.strip().lower()
    return name.strip(), address, domain_of(address)


def decode_header_text(value: str) -> str:
    """Decode a MIME-encoded header (RFC 2047) into plain text.

    IMAP hands back raw headers, so a subject line frequently arrives as
    ``"=?UTF-8?B?...?="``. Gmail and Graph decode for us; running everything through this
    keeps the three adapters producing identical :class:`RawMessage` values.

    Args:
        value: A header value, encoded or not.

    Returns:
        The decoded text, whitespace-normalised. Undecodable bytes become replacement
        characters rather than raising — a mangled subject is worth having, an exception in
        the middle of a sync is not.
    """
    if not value:
        return ""
    from email.header import decode_header, make_header

    try:
        return _SPACES.sub(" ", str(make_header(decode_header(value)))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return _SPACES.sub(" ", str(value)).strip()


# ======================================================================================
# Body extraction
# ======================================================================================


def strip_html(markup: str) -> str:
    """Reduce an HTML message body to readable text.

    Deliberately regex-based and dependency-free: this runs on every HTML message of every
    sync, the output is fed to phrase matching rather than to a renderer, and requiring
    ``beautifulsoup4`` to read a rejection email would be a poor trade. Comments and
    non-prose elements are dropped whole, line-ending tags become newlines, remaining tags
    vanish, and entities are unescaped.

    Args:
        markup: An HTML fragment or document.

    Returns:
        Plain text with collapsed whitespace. Never raises.
    """
    if not markup:
        return ""
    import html as html_module

    text = _HTML_COMMENT.sub(" ", markup)
    text = _HTML_NOISE.sub(" ", text)
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    text = html_module.unescape(text)
    return _tidy(text)


def _tidy(text: str) -> str:
    """Collapse whitespace without destroying paragraph structure.

    Args:
        text: Raw extracted text.

    Returns:
        The text with horizontal whitespace runs collapsed, trailing spaces removed, and
        runs of blank lines reduced to one.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def _decode_part(part: Any) -> str:
    """Decode one MIME part to text, tolerating every charset lie a mailer can tell.

    Args:
        part: An :class:`email.message.Message` leaf part.

    Returns:
        The decoded text, or ``""`` for an attachment, an oversized part, or one that
        carries no payload.
    """
    disposition = str(part.get("Content-Disposition") or "").lower()
    if "attachment" in disposition:
        return ""
    try:
        payload = part.get_payload(decode=True)
    except (AssertionError, TypeError, ValueError):
        return ""
    if not payload:
        return ""
    if len(payload) > MAX_PART_BYTES:
        payload = payload[:MAX_PART_BYTES]
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def extract_text(raw: Any, *, max_chars: int = MAX_BODY_CHARS) -> str:
    """Flatten a message into the bounded plain text everything downstream reads.

    ``text/plain`` wins whenever it exists, because it is what the sender wrote; ``text/html``
    is the fallback and goes through :func:`strip_html`. Attachments are never opened. The
    result is capped at *max_chars* — nothing downstream needs a 200KB marketing email, and
    the classifier's own view is capped an order of magnitude smaller again (§17.4).

    Args:
        raw: RFC 822 bytes, an :class:`email.message.Message`, or a string that is either
            plain text or HTML (which the two API adapters hand over directly).
        max_chars: Ceiling on the returned length.

    Returns:
        Plain text, whitespace-tidied and truncated. Never raises: an unparseable message
        yields ``""`` rather than aborting a sync that has 400 more messages to read.
    """
    limit = max(0, int(max_chars))
    if limit == 0:
        return ""

    if isinstance(raw, str):
        text = strip_html(raw) if _LOOKS_LIKE_HTML.search(raw) else _tidy(raw)
        return text[:limit]

    message = raw
    if isinstance(raw, bytes | bytearray):
        from email import message_from_bytes
        from email.policy import compat32

        try:
            message = message_from_bytes(bytes(raw), policy=compat32)
        except (TypeError, ValueError):
            return ""

    walk = getattr(message, "walk", None)
    if walk is None:
        return _tidy(str(message))[:limit]

    plain: list[str] = []
    html_parts: list[str] = []
    budget = limit * 2  # decode a little more than we keep, then stop
    for part in walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_maintype() != "text":
            continue
        subtype = part.get_content_subtype()
        decoded = _decode_part(part)
        if not decoded:
            continue
        if subtype == "plain":
            plain.append(decoded)
        elif subtype == "html":
            html_parts.append(decoded)
        if sum(len(chunk) for chunk in (*plain, *html_parts)) >= budget:
            break

    if plain:
        return _tidy("\n\n".join(plain))[:limit]
    if html_parts:
        return strip_html("\n".join(html_parts))[:limit]
    return ""


# ======================================================================================
# Queries — the narrow-search guarantee
# ======================================================================================


def chunked(items: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
    """Split *items* into consecutive tuples of at most *size* elements.

    Used to spread a large sender allowlist across several server-side queries rather than
    truncating it: every domain the user applied to is still searched, just not all in one
    command.

    Args:
        items: The sequence to split.
        size: Maximum chunk length; values below 1 are treated as 1.

    Returns:
        A tuple of chunks, empty when *items* is empty.
    """
    step = max(1, int(size))
    return tuple(tuple(items[index : index + step]) for index in range(0, len(items), step))


def domain_allowed(domain: str, allowlist: Sequence[str]) -> bool:
    """Return whether *domain* is covered by *allowlist*.

    Suffix-aware, because mail arrives from subdomains of the domain a user knows about:
    ``us-east.greenhouse.io`` is covered by ``greenhouse.io`` and ``careers.acme.com`` by
    ``acme.com``. Never the other way round — ``acme.com`` is not covered by
    ``careers.acme.com``, and ``notacme.com`` is not covered by ``acme.com``.

    Args:
        domain: The sender domain of a message.
        allowlist: Normalised domains, as held by :attr:`MailQuery.senders`.

    Returns:
        ``True`` when the message is from an allowed sender.
    """
    candidate = normalize_domain(domain)
    if not candidate:
        return False
    return any(candidate == allowed or candidate.endswith(f".{allowed}") for allowed in allowlist)


def resolve_since(
    since: datetime | None,
    *,
    lookback_days: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> datetime:
    """Return the earliest instant a search is permitted to reach.

    The lookback window is a ceiling, not a default: a caller asking for six months of mail
    when ``status_sync_lookback_days`` is 120 gets 120 days. This is the single place that
    clamp happens, so no adapter can widen its own window (§17.8.2).

    Args:
        since: The caller's requested floor. ``None`` means "the whole permitted window".
        lookback_days: Override for ``settings.status_sync_lookback_days``.
        settings: Application settings; resolved from the process singleton when ``None``.
        now: Reference instant, for tests. Defaults to the current UTC time.

    Returns:
        A timezone-aware UTC datetime, never earlier than the lookback floor and never in
        the future.
    """
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    days = lookback_days
    if days is None:
        days = getattr(_resolve_settings(settings), "status_sync_lookback_days", None)
    if days is None:
        days = DEFAULT_LOOKBACK_DAYS
    floor = reference - timedelta(days=max(1, int(days)))
    if since is None:
        return floor
    requested = since if since.tzinfo else since.replace(tzinfo=UTC)
    requested = requested.astimezone(UTC)
    return min(max(requested, floor), reference)


def _resolve_settings(settings: Settings | None) -> Any:
    """Return *settings*, or the process singleton when it is ``None``.

    Imported lazily so that constructing a mailbox in a unit test does not drag in
    ``pydantic-settings`` or read the environment.

    Args:
        settings: An explicit settings object, or ``None``.

    Returns:
        A settings object; an object with no relevant attributes if the import fails, which
        makes every ``getattr`` fall through to this module's own defaults.
    """
    if settings is not None:
        return settings
    try:
        from app.config.settings import get_settings
    except ImportError:  # pragma: no cover - settings is a first-party module
        return object()
    return get_settings()


@dataclass(frozen=True, slots=True)
class MailQuery:
    """A bounded mailbox search: a time floor, a sender allowlist, and a message ceiling.

    Frozen because it is a *permission*, not a working value — an adapter renders it into
    IMAP criteria, a Gmail ``q=`` string or a Graph ``$search`` expression, and must not be
    able to relax it on the way.

    Attributes:
        since: Earliest arrival time to consider, aware UTC. Already clamped to the
            configured lookback window.
        senders: Normalised sender domains. Guaranteed non-empty — :func:`build_query`
            refuses to build a query without one.
        limit: Maximum messages to yield across every folder and every chunk.
    """

    since: datetime
    senders: tuple[str, ...]
    limit: int

    @property
    def since_date(self) -> str:
        """The floor as ``YYYY-MM-DD``, for query languages that take a date."""
        return self.since.date().isoformat()

    def allows(self, domain: str) -> bool:
        """Return whether *domain* is in the allowlist (subdomains included)."""
        return domain_allowed(domain, self.senders)

    def matches(self, message: RawMessage) -> bool:
        """Return whether *message* satisfies this query.

        Applied client-side by the adapters whose incremental API cannot express the
        allowlist server-side — Microsoft Graph's delta feed in particular. The message is
        still discarded before it leaves the adapter, so nothing outside this package ever
        sees mail the query did not authorise.

        Args:
            message: A message the server returned.

        Returns:
            ``True`` when the message is inside the window and from an allowed sender.
        """
        return message.received_at >= self.since and self.allows(message.sender_domain)

    def sender_chunks(self, size: int = DEFAULT_SENDERS_PER_QUERY) -> tuple[tuple[str, ...], ...]:
        """Split :attr:`senders` into per-request groups.

        Args:
            size: Maximum domains per server-side query.

        Returns:
            One tuple of domains per query the adapter should issue.
        """
        return chunked(self.senders, size)


def build_query(
    since: datetime | None = None,
    senders: Iterable[str] | None = None,
    *,
    lookback_days: int | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> MailQuery:
    """Build the only kind of mailbox search this package will perform.

    Two bounds, both mandatory (``docs/CONTRACTS.md`` §17.8.2):

    * **A window**, clamped to ``settings.status_sync_lookback_days`` by
      :func:`resolve_since`.
    * **A sender allowlist**, supplied by the caller — the domains of companies the user
      actually applied to, plus :data:`app.tracking.base.ATS_RELAY_DOMAINS`. Entries are
      normalised and validated, and anything that is not a syntactically valid domain is
      dropped, so a domain can never carry punctuation into a server-side query.

    An empty allowlist raises. That is the whole point: "search my mailbox for everything"
    is not a request this code knows how to make, and a caller that forgot the allowlist
    must fail loudly rather than quietly become a full sweep.

    Args:
        since: Earliest arrival time of interest. Clamped to the lookback window.
        senders: Domains, addresses or URLs identifying senders of interest.
        lookback_days: Override for ``settings.status_sync_lookback_days``.
        limit: Maximum messages for this run; defaults to
            ``settings.status_sync_max_messages_per_run``.
        settings: Application settings; resolved from the process singleton when ``None``.
        now: Reference instant, for tests.

    Returns:
        A :class:`MailQuery` with a clamped window and a non-empty, de-duplicated allowlist.

    Raises:
        MailboxQueryError: If no usable sender domain survives normalisation.
    """
    resolved = _resolve_settings(settings)
    floor = resolve_since(since, lookback_days=lookback_days, settings=resolved, now=now)

    seen: dict[str, None] = {}
    for entry in senders or ():
        normalized = normalize_domain(entry)
        if normalized and normalized not in seen:
            seen[normalized] = None
        if len(seen) >= MAX_SENDER_DOMAINS:
            break
    allowlist = tuple(seen)
    if not allowlist:
        raise MailboxQueryError(
            "refusing to search a mailbox without a sender allowlist: every query must be "
            "bounded by the domains the user actually applied to plus the known ATS relay "
            "domains (docs/CONTRACTS.md §17.8.2)"
        )

    ceiling = limit
    if ceiling is None:
        ceiling = getattr(resolved, "status_sync_max_messages_per_run", DEFAULT_MAX_MESSAGES)
    return MailQuery(since=floor, senders=allowlist, limit=max(1, int(ceiling)))


# ======================================================================================
# Credentials — keychain only, never the database (§17.8.4)
# ======================================================================================


def load_credential(credential_ref: str) -> str:
    """Read one mailbox secret from the OS keychain.

    ``EmailAccount.credential_ref`` is a *key*, never a secret: the IMAP app password and
    the OAuth refresh token live in Windows Credential Manager, the macOS Keychain or the
    freedesktop Secret Service, and this is the only function that reads them.

    There is deliberately no fallback. If ``keyring`` is not installed the operator is told
    to install it — the alternative would be putting a mailbox password in a database column
    or an environment file, which §17.8.4 forbids outright.

    Blocking: keychain backends talk to a system service. Async callers must wrap this in
    :func:`asyncio.to_thread`.

    Args:
        credential_ref: The keychain username half, stored on the account row.

    Returns:
        The stored secret.

    Raises:
        MailboxConfigurationError: If ``keyring`` is not installed, or the reference is
            empty.
        MailboxAuthError: If the keychain holds no secret under this reference — the user
            disconnected the mailbox, or the entry was removed outside the app.
    """
    reference = (credential_ref or "").strip()
    if not reference:
        raise MailboxConfigurationError(
            "this mailbox has no credential_ref; reconnect the account so its secret is "
            "written to the OS keychain"
        )
    try:
        import keyring
    except ImportError as exc:
        raise MailboxConfigurationError(
            "the 'keyring' package is required to read mailbox credentials from the OS "
            "keychain but is not installed — run `pip install keyring`. ApplicantOS will "
            "not fall back to storing mailbox secrets in the database or in .env "
            "(docs/CONTRACTS.md §17.8.4)."
        ) from exc

    try:
        secret = keyring.get_password(KEYRING_SERVICE, reference)
    except Exception as exc:  # keyring raises backend-specific errors
        raise MailboxConfigurationError(
            f"the OS keychain could not be read ({type(exc).__name__}); unlock it or "
            "reconnect the mailbox"
        ) from exc

    if not secret:
        raise MailboxAuthError(
            "no credential is stored in the OS keychain for this mailbox; reconnect the account"
        )
    return secret


def load_credential_json(credential_ref: str) -> dict[str, Any]:
    """Read a JSON credential blob (an OAuth token set) from the keychain.

    Gmail and Outlook store ``{"refresh_token": …, "access_token": …}`` rather than a single
    string. A blob that is not JSON is treated as a bare refresh token, which is what a
    hand-provisioned entry usually contains.

    Args:
        credential_ref: The keychain username half, stored on the account row.

    Returns:
        The decoded mapping. A non-JSON secret becomes ``{"refresh_token": <secret>}``.

    Raises:
        MailboxConfigurationError: If ``keyring`` is unavailable or the reference is empty.
        MailboxAuthError: If nothing is stored, or the blob is JSON but not an object.
    """
    secret = load_credential(credential_ref)
    try:
        decoded = json.loads(secret)
    except (TypeError, ValueError):
        return {"refresh_token": secret}
    if not isinstance(decoded, dict):
        raise MailboxAuthError(
            "the stored mailbox credential is not an OAuth token object; reconnect the account"
        )
    return decoded


# ======================================================================================
# Messages
# ======================================================================================


@dataclass(slots=True)
class RawMessage:
    """One message, as far as this package ever looks at it.

    Provider-neutral by design: an IMAP UID fetch, a Gmail ``messages.get`` and a Graph
    ``/messages`` item all reduce to exactly these fields, so the tracker above has one
    shape to convert into a :class:`~app.tracking.base.StatusSignal` and the classifier has
    one shape to read.

    Attributes:
        message_id: The source's own identifier — an IMAP UID, a Gmail message id, a Graph
            message id. Becomes ``StatusSignal.external_ref`` and therefore half of the
            idempotency key that makes re-reading an overlapping window free.
        received_at: Arrival time, aware UTC.
        sender: The bare sender address, lowercased.
        sender_name: The display name, which is where an ATS relay puts the employer.
        sender_domain: Normalised sender domain.
        subject: Decoded subject, capped at :data:`MAX_SUBJECT_CHARS`.
        body: Flattened text, capped at :data:`MAX_BODY_CHARS`. Memory-only — the database
            keeps a 500-character snippet and nothing more (§17.8.3).
        reply_to: Bare reply-to address when the message carries one; frequently the
            employer's real address on mail relayed by an ATS.
        folder: Source folder or label, for logs.
        raw: Small provider-specific extras (thread id, labels). Not a place for the body.
    """

    message_id: str
    received_at: datetime
    sender: str = ""
    sender_name: str = ""
    sender_domain: str = ""
    subject: str = ""
    body: str = ""
    reply_to: str = ""
    folder: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise, bound and back-fill the fields the adapters populate unevenly.

        Forces the timestamp to aware UTC, lowercases the addresses, derives
        :attr:`sender_domain` from :attr:`sender` when an adapter did not set it, and
        applies the retention caps here rather than in three separate adapters — so no
        adapter can forget them.
        """
        self.message_id = str(self.message_id).strip()
        self.received_at = (
            self.received_at.replace(tzinfo=UTC)
            if self.received_at.tzinfo is None
            else self.received_at.astimezone(UTC)
        )
        self.sender = self.sender.strip().lower()
        self.sender_name = _SPACES.sub(" ", self.sender_name).strip()
        self.sender_domain = normalize_domain(self.sender_domain) or domain_of(self.sender)
        self.reply_to = self.reply_to.strip().lower()
        self.subject = _SPACES.sub(" ", self.subject).strip()[:MAX_SUBJECT_CHARS]
        self.body = self.body[:MAX_BODY_CHARS]

    def log_context(self) -> dict[str, Any]:
        """Return the only fields that may be logged about a message (§17.8.5).

        Never a body, never an address, never a subject. A message id and a folder are
        enough to correlate a log line with the mailbox and carry nothing private.

        Returns:
            A structlog-ready mapping of ``message_id`` and ``folder``.
        """
        return {"message_id": self.message_id, "folder": self.folder}


# ======================================================================================
# The protocol
# ======================================================================================


@runtime_checkable
class MailBox(Protocol):
    """The read-only mailbox interface (``docs/CONTRACTS.md`` §17.3).

    Three implementations — :class:`~app.tracking.email.gmail.GmailMailbox`,
    :class:`~app.tracking.email.outlook.OutlookMailbox` and
    :class:`~app.tracking.email.imap.ImapMailbox` — and a caller never imports one directly:
    the email tracker picks by ``EmailAccount.provider``.

    The interface has no mutating member, and that is a deliberate design constraint rather
    than an omission. There is no ``mark_read``, no ``archive``, no ``delete``: an operation
    that does not exist in the protocol cannot be called by mistake, and the absence is
    verifiable by reading forty lines rather than auditing three adapters.

    Usage::

        await mailbox.connect()
        try:
            async for message in mailbox.search(since=floor, cursor=saved, senders=allow):
                ...
            new_cursor = await mailbox.cursor()
        finally:
            await mailbox.close()
    """

    async def connect(self) -> None:
        """Authenticate against the mail server.

        Reads the account's secret from the OS keychain and establishes whatever session
        the protocol needs — an IMAP connection, an OAuth access token. Idempotent:
        connecting twice is a no-op, not a second session.

        Raises:
            MailboxAuthError: If the server rejected the credential.
            MailboxConfigurationError: If the adapter is not configured to connect.
            MailboxUnavailableError: If the server could not be reached.
        """
        ...

    def search(
        self,
        *,
        since: datetime | None = None,
        cursor: str | None = None,
        senders: Sequence[str] | None = None,
    ) -> AsyncIterator[RawMessage]:
        """Yield messages inside the window and inside the sender allowlist.

        Declared without ``async`` because every implementation is an ``async def``
        containing ``yield`` — an async generator function — and this signature is what such
        a function actually has. The same convention as
        :meth:`app.jobs.base.ATSProvider.search`.

        Implementations must pass *since* and *senders* through :func:`build_query` and use
        nothing else to bound the search: that function is where the privacy guarantee is
        implemented, and an adapter that built its own criteria would be bypassing it.

        Args:
            since: Earliest arrival time of interest; clamped to the lookback window.
            cursor: The opaque cursor returned by :meth:`cursor` after the previous run.
                An unrecognisable or invalidated cursor must degrade to a full re-scan of
                the window, never to silently skipped mail.
            senders: Sender domains to restrict the search to. Required in practice — an
                empty allowlist raises rather than sweeping the mailbox.

        Yields:
            :class:`RawMessage` values, oldest first.

        Raises:
            MailboxQueryError: If the allowlist is empty.
            MailboxAuthError: If the session was rejected.
            MailboxUnavailableError: If the server could not be reached.
        """
        ...

    async def cursor(self) -> str:
        """Return the cursor to persist for the next incremental run.

        Only meaningful after :meth:`search` has been fully consumed: it describes the point
        the completed run reached, so persisting it early would skip mail (golden rule #8).

        Returns:
            An opaque, provider-specific string — ``UIDVALIDITY:UID``, a Gmail
            ``historyId``, or a Graph delta token. ``""`` when there is nothing to resume
            from.
        """
        ...

    async def close(self) -> None:
        """Release the session.

        Idempotent, and safe on a mailbox that never connected. Implementations log out or
        close their HTTP client; none of them issues a mailbox-modifying command on the way
        out (an IMAP ``CLOSE`` would expunge in a read-write session, so the adapter logs
        out instead).
        """
        ...


# ======================================================================================
# Shared plumbing for the two OAuth mailboxes
# ======================================================================================


class OAuthMailbox:
    """Common machinery for the Gmail and Microsoft Graph adapters.

    Both are the same shape — refresh an access token with a keychain-held refresh token,
    then make read-only JSON GETs against a REST API — and writing that twice would be two
    places for the retry rules, the error mapping and the token handling to drift apart.
    Subclasses supply the endpoints and implement :meth:`MailBox.search` and
    :meth:`MailBox.cursor`.

    The access token lives on the instance for the life of one sync and is never logged,
    never persisted, and never written back to the keychain: a refresh costs one request and
    is cheaper than a stale-token failure mid-run.

    Attributes:
        token_endpoint: OAuth token endpoint of the provider.
        mail_scope: The single mailbox permission requested — read-only, by construction.
        provider_label: Short name used in error messages and log events.
    """

    token_endpoint: ClassVar[str] = ""
    mail_scope: ClassVar[str] = ""
    provider_label: ClassVar[str] = "oauth"

    def __init__(
        self,
        *,
        credential_ref: str,
        client_id: str | None,
        client_secret: str | None,
        max_messages: int | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        settings: Settings | None = None,
    ) -> None:
        """Configure the adapter without touching the network or the keychain.

        Args:
            credential_ref: Keychain key holding the OAuth token blob.
            client_id: OAuth client id for this provider, from settings.
            client_secret: OAuth client secret for this provider, from settings.
            max_messages: Ceiling on messages per run; defaults to
                ``settings.status_sync_max_messages_per_run``.
            timeout_seconds: Per-request HTTP timeout.
            settings: Application settings; resolved lazily when ``None``.
        """
        self._credential_ref = credential_ref
        self._client_id = client_id
        self._client_secret = client_secret
        self._settings = settings
        self._timeout = max(1.0, float(timeout_seconds))
        resolved_max = max_messages
        if resolved_max is None:
            resolved_max = getattr(
                _resolve_settings(settings),
                "status_sync_max_messages_per_run",
                DEFAULT_MAX_MESSAGES,
            )
        self._max_messages = max(1, int(resolved_max))
        self._client: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._lock = asyncio.Lock()
        self.logger = logger.bind(mailbox=self.provider_label)

    # -- lifecycle ---------------------------------------------------------------------

    async def connect(self) -> None:
        """Obtain an access token for this run.

        Idempotent: a second call while a token is held returns immediately.

        Raises:
            MailboxConfigurationError: If the OAuth client is not configured or ``keyring``
                is missing.
            MailboxAuthError: If the refresh token was rejected.
            MailboxUnavailableError: If the token endpoint could not be reached.
        """
        async with self._lock:
            if self._access_token is not None:
                return
            await self._refresh_access_token()

    async def close(self) -> None:
        """Drop the access token and close the HTTP client. Idempotent."""
        async with self._lock:
            client = self._client
            self._client = None
            self._access_token = None
        if client is not None:
            await client.aclose()

    async def healthcheck(self) -> bool:
        """Return whether this adapter could authenticate right now.

        Returns:
            ``True`` when a token was obtained. A configuration or auth failure returns
            ``False`` rather than raising, because health checks aggregate.
        """
        try:
            await self.connect()
        except MailboxError:
            return False
        return True

    # -- HTTP --------------------------------------------------------------------------

    @property
    def _http(self) -> httpx.AsyncClient:
        """The adapter's shared ``httpx.AsyncClient``, built on first use.

        Returns:
            A client carrying the ApplicantOS user agent and this adapter's timeout.

        Raises:
            MailboxConfigurationError: If ``httpx`` is not installed.
        """
        client = self._client
        if client is not None:
            return client
        try:
            import httpx
        except ImportError as exc:
            raise MailboxConfigurationError(
                "the 'httpx' package is required to read a Gmail or Outlook mailbox but is "
                "not installed — run `pip install httpx`"
            ) from exc
        created = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._client = created
        return created

    def _refresh_payload(self, refresh_token: str) -> dict[str, str]:
        """Return the form body for the OAuth refresh grant.

        Overridden by Microsoft, which requires the scope to be repeated on refresh.

        Args:
            refresh_token: The keychain-held refresh token.

        Returns:
            The form fields to post to :attr:`token_endpoint`.
        """
        return {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id or "",
            "client_secret": self._client_secret or "",
        }

    async def _refresh_access_token(self) -> None:
        """Exchange the stored refresh token for an access token.

        A credential blob holding only an access token (a hand-provisioned entry, or a
        short-lived token supplied for a one-off run) is used as-is rather than refreshed.

        Raises:
            MailboxConfigurationError: If the OAuth client id/secret are unset.
            MailboxAuthError: If the grant was rejected or no usable token is stored.
            MailboxUnavailableError: If the token endpoint could not be reached.
        """
        blob = await asyncio.to_thread(load_credential_json, self._credential_ref)
        refresh_token = str(blob.get("refresh_token") or "").strip()
        if not refresh_token:
            existing = str(blob.get("access_token") or "").strip()
            if existing:
                self._access_token = existing
                return
            raise MailboxAuthError(
                f"the stored {self.provider_label} credential contains no refresh token; "
                "reconnect the mailbox"
            )

        if not self._client_id or not self._client_secret:
            raise MailboxConfigurationError(
                f"{self.provider_label} OAuth client is not configured — set the "
                f"{self.provider_label.upper()}_CLIENT_ID and "
                f"{self.provider_label.upper()}_CLIENT_SECRET settings"
            )

        import httpx

        try:
            response = await self._http.post(
                self.token_endpoint, data=self._refresh_payload(refresh_token)
            )
        except httpx.HTTPError as exc:
            raise MailboxUnavailableError(
                f"could not reach the {self.provider_label} token endpoint: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            raise MailboxAuthError(
                f"{self.provider_label} refused to refresh the access token "
                f"(HTTP {response.status_code}); the user must reconnect the mailbox"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MailboxUnavailableError(
                f"{self.provider_label} token endpoint returned a non-JSON response"
            ) from exc

        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise MailboxAuthError(
                f"{self.provider_label} returned no access token; reconnect the mailbox"
            )
        self._access_token = token
        self.logger.info("mailbox.token_refreshed")

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform one authenticated read and return the decoded JSON object.

        Only ``GET`` exists here. That is the read-only guarantee expressed as an API: this
        class offers no way to make a mutating request against a mailbox, so no subclass can
        make one by accident.

        A ``401`` triggers exactly one token refresh and one retry — an access token expiring
        mid-sync is routine, an endless refresh loop is not.

        Args:
            url: Absolute URL.
            params: Query parameters.
            headers: Extra headers merged over the authorization header.

        Returns:
            The decoded JSON object. A JSON array or scalar yields ``{}``.

        Raises:
            MailboxAuthError: On a ``401``/``403`` that survived one refresh.
            MailboxUnavailableError: On a transport failure, a rate limit, or a 5xx.
            MailboxError: On any other 4xx.
        """
        import httpx

        for attempt in (1, 2):
            if self._access_token is None:
                await self._refresh_access_token()
            request_headers = {"Authorization": f"Bearer {self._access_token}"}
            if headers:
                request_headers.update(headers)
            try:
                response = await self._http.get(url, params=params, headers=request_headers)
            except httpx.HTTPError as exc:
                raise MailboxUnavailableError(
                    f"{self.provider_label} request failed: {type(exc).__name__}"
                ) from exc

            if response.status_code == 401 and attempt == 1:
                self._access_token = None
                continue
            if response.status_code in (401, 403):
                raise MailboxAuthError(
                    f"{self.provider_label} denied access (HTTP {response.status_code}); "
                    "the mailbox must be reconnected",
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise MailboxUnavailableError(
                    f"{self.provider_label} is unavailable (HTTP {response.status_code})",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise MailboxError(
                    f"{self.provider_label} rejected the request (HTTP {response.status_code})",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise MailboxUnavailableError(
                    f"{self.provider_label} returned a non-JSON response"
                ) from exc
            return payload if isinstance(payload, dict) else {}

        raise MailboxAuthError(  # pragma: no cover - the loop always returns or raises
            f"{self.provider_label} authentication could not be established"
        )
