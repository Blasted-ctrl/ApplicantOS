"""Read-only mailbox adapters — the transport half of application status sync.

``docs/CONTRACTS.md`` §17 in one namespace. Three adapters implement one protocol:

============================================  ==================================================
:class:`~app.tracking.email.gmail.GmailMailbox`     Gmail REST API, ``historyId`` incremental sync,
                                              scope ``gmail.readonly``.
:class:`~app.tracking.email.outlook.OutlookMailbox` Microsoft Graph, delta tokens, scope
                                              ``Mail.Read``.
:class:`~app.tracking.email.imap.ImapMailbox`       Anything else that speaks IMAP — Fastmail,
                                              iCloud, Proton Bridge, a university server, or
                                              Gmail with an app password. ``EXAMINE`` +
                                              ``BODY.PEEK[]``, ``UIDVALIDITY:UID`` cursor.
============================================  ==================================================

Callers never import one of these directly: the email tracker resolves the adapter from
``EmailAccount.provider``, exactly as the pipeline resolves an ATS provider through the
plugin registry (golden rule #5).

Every adapter is read-only and provably so. Not one send, delete, move, copy, flag or
expunge call exists in this package — the operations are not merely unused, they are not
imported, so ``grep -rniE "\\.(send|delete|move|copy|store|expunge|trash)\\(" app/tracking/``
is a complete audit rather than a spot check. The full set of privacy invariants, and which
line of code enforces each, is documented in :mod:`app.tracking.email.base`.
"""

from __future__ import annotations

from app.tracking.email.base import (
    DEFAULT_FOLDERS,
    DEFAULT_SENDERS_PER_QUERY,
    KEYRING_SERVICE,
    MAX_BODY_CHARS,
    MAX_SENDER_DOMAINS,
    MailBox,
    MailboxAuthError,
    MailboxConfigurationError,
    MailboxError,
    MailboxQueryError,
    MailboxUnavailableError,
    MailQuery,
    OAuthMailbox,
    RawMessage,
    build_query,
    decode_header_text,
    domain_allowed,
    domain_of,
    extract_text,
    load_credential,
    load_credential_json,
    normalize_domain,
    parse_sender,
    resolve_since,
    strip_html,
)
from app.tracking.email.gmail import GMAIL_READONLY_SCOPE, GmailMailbox
from app.tracking.email.imap import ImapMailbox
from app.tracking.email.outlook import OUTLOOK_MAIL_READ_SCOPE, OutlookMailbox

__all__ = [
    # Protocol and shapes
    "MailBox",
    "MailQuery",
    "RawMessage",
    "OAuthMailbox",
    # Adapters
    "GmailMailbox",
    "ImapMailbox",
    "OutlookMailbox",
    # Scopes — read-only, and named so a reviewer can find them in one grep
    "GMAIL_READONLY_SCOPE",
    "OUTLOOK_MAIL_READ_SCOPE",
    # Query construction and text handling
    "build_query",
    "resolve_since",
    "domain_allowed",
    "domain_of",
    "normalize_domain",
    "parse_sender",
    "decode_header_text",
    "extract_text",
    "strip_html",
    # Credentials
    "KEYRING_SERVICE",
    "load_credential",
    "load_credential_json",
    # Errors
    "MailboxError",
    "MailboxAuthError",
    "MailboxConfigurationError",
    "MailboxQueryError",
    "MailboxUnavailableError",
    # Limits
    "DEFAULT_FOLDERS",
    "DEFAULT_SENDERS_PER_QUERY",
    "MAX_BODY_CHARS",
    "MAX_SENDER_DOMAINS",
]
