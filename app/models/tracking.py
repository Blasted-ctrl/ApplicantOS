"""Status sync storage — the connected mailbox and the signals read out of it.

These two tables close the application loop described in ``docs/CONTRACTS.md`` §17: an
outcome finds its own way into the database instead of waiting for the user to type it in.
:class:`EmailAccount` is a mailbox the user explicitly connected; :class:`StatusSignal` is
one message from it, classified, matched to an application, and either applied or parked for
a human.

Three properties of this schema are load-bearing, and none of them is a convenience.

**Re-syncing is idempotent.** ``UNIQUE(user_id, source, external_ref)`` — named
``uq_status_signals_user_id_source_external_ref`` — means the same message can never produce
a second signal, however many times a mailbox is re-polled, a cursor is reset, or a worker
retries. Without it a crash mid-sync would replay a rejection email and re-apply a status
transition that had already been recorded. The constraint is the guarantee; the cursor on
:class:`EmailAccount` is only an optimisation on top of it.

**Full message bodies are never persisted.** ``docs/CONTRACTS.md`` §17.8.3 is explicit:
message id, sender, subject, a snippet capped at :data:`SNIPPET_MAX_LENGTH` characters, the
timestamp and the classification — nothing else. The body lives in memory for the duration
of classification and is then dropped. The cap is enforced twice, in the column width *and*
in :meth:`StatusSignal.truncate`, because SQLite silently accepts an over-long value where
PostgreSQL raises: relying on the column alone would mean the invariant held on the
deployment target and quietly failed on the lightweight one.

**Credentials never touch the database.** :attr:`EmailAccount.credential_ref` stores a key
into the OS keychain (``keyring``), never a token, password, or refresh token. Disconnecting
an account deletes the row, and the service deletes the keychain entry the reference names.

Deletion posture: both tables are owned by the user and cascade with them.
``status_signals.application_id`` is ``ON DELETE SET NULL`` — a signal is *evidence*, and it
outlives the application it was matched to, keeping the record of what the mailbox actually
said.
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database.base import Base
from app.database.types import GUID
from app.models.application import APPLICATION_STATUS_COLUMN
from app.models.enums import (
    ApplicationStatus,
    MailProvider,
    SignalKind,
    SignalSource,
)
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.user import User

__all__ = [
    "CREDENTIAL_REF_MAX_LENGTH",
    "CURSOR_MAX_LENGTH",
    "DEFAULT_SIGNAL_CONFIDENCE",
    "EMAIL_ADDRESS_MAX_LENGTH",
    "EXTERNAL_REF_MAX_LENGTH",
    "MAIL_PROVIDER_COLUMN",
    "SENDER_DOMAIN_MAX_LENGTH",
    "SENDER_MAX_LENGTH",
    "SIGNAL_KIND_COLUMN",
    "SIGNAL_SOURCE_COLUMN",
    "SNIPPET_MAX_LENGTH",
    "SUBJECT_MAX_LENGTH",
    "EmailAccount",
    "StatusSignal",
]


# --------------------------------------------------------------------------------------
# Column types and sizes
# --------------------------------------------------------------------------------------

# Enum columns persist the lowercase string *value*, never the Python member name, exactly
# as ``app/models/application.py`` does. SQLAlchemy's default writes ``Enum.name``, which
# would break the frozen wire vocabulary of ``docs/CONTRACTS.md`` §3.
_ENUM_VALUES: Final = operator.methodcaller("values")

#: Storage type for :class:`~app.models.enums.MailProvider`.
MAIL_PROVIDER_COLUMN: Final[SAEnum] = SAEnum(
    MailProvider,
    name="mail_provider",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Storage type for :class:`~app.models.enums.SignalSource`.
SIGNAL_SOURCE_COLUMN: Final[SAEnum] = SAEnum(
    SignalSource,
    name="signal_source",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Storage type for :class:`~app.models.enums.SignalKind`.
SIGNAL_KIND_COLUMN: Final[SAEnum] = SAEnum(
    SignalKind,
    name="signal_kind",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: RFC 5321 maximum email address length: a 64-octet local part, ``@``, a 255-octet domain.
EMAIL_ADDRESS_MAX_LENGTH: Final[int] = 320

#: Width of the OS-keychain lookup key. A structured string such as
#: ``"applicantos:gmail:user@example.com"`` — never a credential.
CREDENTIAL_REF_MAX_LENGTH: Final[int] = 255

#: Width of a mailbox resume cursor: a Gmail ``historyId``, an IMAP ``UIDVALIDITY:UID``
#: pair, or a Microsoft Graph delta token. Generous, because delta tokens are opaque.
CURSOR_MAX_LENGTH: Final[int] = 1024

#: Width of the provider's own message identifier — an RFC 5322 ``Message-ID``, a Gmail
#: message id, or a Graph message id. Half of the idempotency key, so it is stored whole.
EXTERNAL_REF_MAX_LENGTH: Final[int] = 512

#: Width of the ``From`` header as received, which is a display name *and* an address
#: (``"Acme Recruiting <no-reply@greenhouse.io>"``), not a bare address.
SENDER_MAX_LENGTH: Final[int] = 512

#: Registrable domain length per RFC 1035. Matches ``companies.domain``, because the signal
#: matcher compares the two directly.
SENDER_DOMAIN_MAX_LENGTH: Final[int] = 253

#: Width of a stored subject line. RFC 5322 caps a header line at 998 octets; MIME-decoded
#: subjects are far shorter, and anything longer is truncated rather than rejected — a
#: subject is display metadata, never evidence.
SUBJECT_MAX_LENGTH: Final[int] = 998

#: **Binding.** Maximum characters of message body retained, per ``docs/CONTRACTS.md``
#: §17.8.3. Enforced in the column width and in :meth:`StatusSignal.truncate`.
SNIPPET_MAX_LENGTH: Final[int] = 500

#: Confidence recorded when a signal is written without one. Zero, not a midpoint: an
#: unstated confidence must never clear ``settings.status_sync_min_confidence``.
DEFAULT_SIGNAL_CONFIDENCE: Final[float] = 0.0


class EmailAccount(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """One mailbox the user connected so ApplicantOS can read application outcomes from it.

    Nothing is read until a row exists here: connecting is an explicit act, and
    disconnecting deletes the row together with the keychain entry :attr:`credential_ref`
    names and the :attr:`cursor` that would let a sync resume (``docs/CONTRACTS.md``
    §17.8.6).

    Access is read-only on every provider — OAuth read scopes for Gmail and Outlook,
    ``readonly=True`` for IMAP — and search is always narrowed to the lookback window and to
    sender domains derived from companies the user actually applied to, plus the known ATS
    relay domains. There is no full-mailbox sweep anywhere in this system.

    Note:
        :attr:`folders` is a plain JSON column and is not change tracked. Reassign it rather
        than mutating it in place.
    """

    __tablename__ = "email_accounts"
    __table_args__ = (
        # One row per address per user. Re-connecting the same mailbox must update the
        # existing row (and its cursor) rather than start a second, competing sync.
        UniqueConstraint(
            "user_id",
            "address",
            name="uq_email_accounts_user_id_address",
        ),
        # The only scheduling query: "which of this user's mailboxes are still switched on?"
        Index("ix_email_accounts_user_id_enabled", "user_id", "enabled"),
    )

    provider: Mapped[MailProvider] = mapped_column(
        MAIL_PROVIDER_COLUMN,
        nullable=False,
        index=True,
        doc="Mailbox implementation: gmail, outlook or generic IMAP. All read-only.",
    )
    address: Mapped[str] = mapped_column(
        String(EMAIL_ADDRESS_MAX_LENGTH),
        nullable=False,
        doc="The mailbox address. Half of the per-user uniqueness key.",
    )
    credential_ref: Mapped[str | None] = mapped_column(
        String(CREDENTIAL_REF_MAX_LENGTH),
        nullable=True,
        doc=(
            "Lookup key into the OS keychain (keyring) — NEVER a token, password or "
            "refresh token. Credentials must not touch the database; this column holds "
            "only the name under which the real secret is stored outside it."
        ),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
        doc="Whether the poller may read this mailbox. Cleared to pause without deleting.",
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        doc="When this mailbox was last polled successfully. NULL means never.",
    )
    cursor: Mapped[str | None] = mapped_column(
        String(CURSOR_MAX_LENGTH),
        nullable=True,
        doc=(
            "Provider resume token: a Gmail historyId, an IMAP 'UIDVALIDITY:UID' pair, or "
            "a Graph delta token. An optimisation only — correctness rests on the unique "
            "constraint over (user_id, source, external_ref), not on this."
        ),
    )
    folders: Mapped[list[str]] = mapped_column(
        default=list,
        nullable=False,
        doc="Folders/labels to search. Empty means the provider's default inbox scope.",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Message from the most recent failed poll, shown beside the account in the UI.",
    )

    user: Mapped[User] = relationship("User")

    @property
    def connected(self) -> bool:
        """Whether a credential is stored in the keychain for this account.

        Derived from the *presence* of :attr:`credential_ref`, so the API can report
        connection state without the response schema ever carrying the reference itself.

        Returns:
            ``True`` when a keychain reference has been recorded.
        """
        return bool(self.credential_ref)

    @property
    def is_pollable(self) -> bool:
        """Whether the poller may read this mailbox right now.

        Both halves are required: the user has to have left it switched on, and a credential
        has to exist. A row with no credential is a half-finished connection, not a mailbox.

        Returns:
            ``True`` when the account is enabled and has a keychain reference.
        """
        return bool(self.enabled) and self.connected

    @validates("address")
    def _normalize_address(self, _key: str, value: str | None) -> str | None:
        """Fold an assigned address to lowercase and strip surrounding whitespace.

        Mailbox addresses arrive from OAuth profile responses, IMAP configuration and typed
        input, and those three disagree about case. ``UNIQUE(user_id, address)`` compares
        bytes, so ``User@Example.com`` and ``user@example.com`` would otherwise be two
        mailboxes and two independent syncs of the same inbox.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The address being assigned.

        Returns:
            The folded address, or ``None``.
        """
        if value is None:
            return None
        return value.strip().lower()

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"EmailAccount(id={self.id!r}, user_id={self.user_id!r}, "
            f"provider={self.provider!r}, enabled={self.enabled!r})"
        )


class StatusSignal(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """One classified message that says something about one application's outcome.

    This is the persisted form of the transport-shaped ``StatusSignal`` dataclass in
    :mod:`app.tracking.base`, minus the body: what survives is the identity of the message,
    who sent it, what it was about, what the classifier made of it, and how sure that was.

    Two flags decide what happens next, and they are mutually exclusive in practice:

    * :attr:`applied` — the signal moved the application's status, and
      :attr:`~app.models.application.Application.status_source` records that it did.
    * :attr:`needs_review` — confidence fell below ``settings.status_sync_min_confidence``,
      or no application matched well enough. The signal is surfaced in the desktop app for
      one-click confirmation instead of being guessed at, which is golden rule #2 applied to
      inbound outcomes rather than to outbound answers.

    Note:
        :attr:`match_evidence` is a plain JSON column and is not change tracked. Reassign it
        rather than mutating it in place.
    """

    __tablename__ = "status_signals"
    __table_args__ = (
        # What makes re-syncing idempotent: the same message, from the same source, for the
        # same user, can only ever be one row. See the module docstring.
        UniqueConstraint(
            "user_id",
            "source",
            "external_ref",
            name="uq_status_signals_user_id_source_external_ref",
        ),
        # "What has this sync already acted on?" and "what is waiting for a human?" — the
        # two scans the worker and the review queue perform on every pass.
        Index("ix_status_signals_user_id_applied", "user_id", "applied"),
        Index("ix_status_signals_user_id_needs_review", "user_id", "needs_review"),
    )

    # -- subject -----------------------------------------------------------------------
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "Application this signal was matched to, once it was. NULL while unmatched, "
            "and SET NULL afterwards: a signal is evidence of what the mailbox said and "
            "outlives the row it referred to."
        ),
    )
    source: Mapped[SignalSource] = mapped_column(
        SIGNAL_SOURCE_COLUMN,
        nullable=False,
        index=True,
        doc="Channel the signal came from. Part of the (user, source, ref) idempotency key.",
    )
    kind: Mapped[SignalKind] = mapped_column(
        SIGNAL_KIND_COLUMN,
        default=SignalKind.UNKNOWN,
        nullable=False,
        index=True,
        doc="What the classifier made of the message; 'unknown' never moves anything.",
    )
    external_ref: Mapped[str] = mapped_column(
        String(EXTERNAL_REF_MAX_LENGTH),
        nullable=False,
        doc=(
            "Provider message identifier. The other half of the idempotency key, and the "
            "reason a re-poll of the same mailbox cannot replay a rejection twice."
        ),
    )

    # -- message metadata (never the body) -----------------------------------------------
    sender: Mapped[str] = mapped_column(
        String(SENDER_MAX_LENGTH),
        default="",
        nullable=False,
        doc="The From header as received, display name included.",
    )
    sender_domain: Mapped[str] = mapped_column(
        String(SENDER_DOMAIN_MAX_LENGTH),
        default="",
        nullable=False,
        index=True,
        doc="Domain of the sender address; matched against companies.domain and ATS relays.",
    )
    subject: Mapped[str] = mapped_column(
        String(SUBJECT_MAX_LENGTH),
        default="",
        nullable=False,
        doc="Decoded subject line, truncated to the column width.",
    )
    snippet: Mapped[str] = mapped_column(
        String(SNIPPET_MAX_LENGTH),
        default="",
        nullable=False,
        doc=(
            "At most 500 characters of body, retained so a reviewer can see why the "
            "classifier decided what it did. Full message bodies are NEVER stored "
            "(docs/CONTRACTS.md §17.8.3); the cap is enforced here and by a validator."
        ),
    )
    received_at: Mapped[datetime] = mapped_column(
        nullable=False,
        index=True,
        doc=(
            "When the message arrived, from the provider — not when it was read. Drives "
            "the 'submitted before this' half of matching and the incremental lookback."
        ),
    )

    # -- classification and disposition ----------------------------------------------------
    detected_status: Mapped[ApplicationStatus | None] = mapped_column(
        APPLICATION_STATUS_COLUMN,
        nullable=True,
        doc=(
            "Status the kind implies, resolved through SIGNAL_KIND_TO_STATUS. NULL for a "
            "kind that carries information but no status change ('viewed', 'unknown')."
        ),
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        default=DEFAULT_SIGNAL_CONFIDENCE,
        server_default="0",
        nullable=False,
        doc="Combined classification and match confidence, in [0, 1]; the review gate.",
    )
    applied: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
        doc="Whether this signal has already moved the matched application's status.",
    )
    needs_review: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
        doc="Whether a human must confirm this signal before it may be applied.",
    )
    match_evidence: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Why the matcher chose this application: domain hit, name score, ids, window.",
    )

    application: Mapped[Application | None] = relationship("Application")
    user: Mapped[User] = relationship("User")

    # -- behaviour -------------------------------------------------------------------------

    @staticmethod
    def truncate(value: str | None, limit: int) -> str:
        """Return *value* clipped to *limit* characters, never ``None``.

        Args:
            value: Text from a mail header or body. ``None`` is accepted and becomes ``""``,
                because these columns are ``NOT NULL`` and an absent header is not an error.
            limit: Maximum number of characters to keep.

        Returns:
            The clipped text.
        """
        if not value:
            return ""
        return value[:limit]

    @validates("snippet")
    def _cap_snippet(self, _key: str, value: str | None) -> str:
        """Enforce the 500-character retention cap on every assignment.

        The column is already ``VARCHAR(500)``, but that is only half an enforcement:
        PostgreSQL raises on an over-long value while SQLite stores it in full, so a caller
        that handed a whole message body to this attribute would violate
        ``docs/CONTRACTS.md`` §17.8.3 silently on the lightweight install and loudly on the
        real one. Truncating here makes the invariant hold identically on both, and makes it
        hold on the in-memory object rather than only after a flush.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The snippet being assigned, possibly a full body.

        Returns:
            At most :data:`SNIPPET_MAX_LENGTH` characters.
        """
        return self.truncate(value, SNIPPET_MAX_LENGTH)

    @validates("subject")
    def _cap_subject(self, _key: str, value: str | None) -> str:
        """Clip an assigned subject to the column width, for the same reason as the snippet.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The subject being assigned.

        Returns:
            At most :data:`SUBJECT_MAX_LENGTH` characters.
        """
        return self.truncate(value, SUBJECT_MAX_LENGTH)

    @validates("sender")
    def _cap_sender(self, _key: str, value: str | None) -> str:
        """Clip an assigned ``From`` header to the column width.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The sender header being assigned.

        Returns:
            At most :data:`SENDER_MAX_LENGTH` characters.
        """
        return self.truncate(value, SENDER_MAX_LENGTH)

    @validates("sender_domain")
    def _normalize_sender_domain(self, _key: str, value: str | None) -> str:
        """Fold an assigned sender domain to lowercase and clip it to the column width.

        Domains are case-insensitive and arrive in whatever case the sender used, but the
        matcher compares them byte-for-byte against ``companies.domain`` and the ATS relay
        list — both of which are lowercase.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The domain being assigned.

        Returns:
            The folded domain, at most :data:`SENDER_DOMAIN_MAX_LENGTH` characters.
        """
        if not value:
            return ""
        return self.truncate(value.strip().lower(), SENDER_DOMAIN_MAX_LENGTH)

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"StatusSignal(id={self.id!r}, user_id={self.user_id!r}, "
            f"source={self.source!r}, kind={self.kind!r}, applied={self.applied!r})"
        )
