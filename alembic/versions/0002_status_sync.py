"""status sync

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08

Adds the two tables behind automatic application status sync (``docs/CONTRACTS.md`` §17) and
the three columns that let an :class:`~app.models.application.Application` record *how* it
learned its current status.

Hand-written, in the style of ``0001_initial_schema.py``, and for the same reason: every
constraint name, referential action, index and column width below is a deliberate decision
rather than whatever a diff happened to emit. The conventions inherited from that file hold
here unchanged:

**Portable column types, not literal ones.** ``GUID`` / ``JSONType`` / ``UTCDateTime`` from
:mod:`app.database.types` are used exactly as the models declare them, so the migrated schema
is byte-identical to ``Base.metadata.create_all`` on both PostgreSQL and SQLite.

**Enums are VARCHAR, not native types.** Every enum column in the models is
``sa.Enum(..., native_enum=False)`` with no explicit length, which compiles to a
``VARCHAR(n)`` whose width is the longest member *value*. Those widths are named as constants
below rather than inlined. Adding an enum member that is longer than its constant is
therefore a schema change and needs its own migration — which is the point: it makes the
consequence visible instead of silently truncating on PostgreSQL.

**Two invariants this migration exists to create.**

* ``uq_status_signals_user_id_source_external_ref`` is what makes re-syncing idempotent. The
  same message can never produce two signals, so a crash mid-sync, a reset cursor or a
  retried worker cannot replay a rejection email and re-apply a status transition.
* ``uq_email_accounts_user_id_address`` is what stops one mailbox becoming two competing
  syncs when the same address is connected twice in different case.

**``status_signals.application_id`` is SET NULL, not CASCADE.** A signal is evidence of what
the mailbox actually said and outlives the application row it was matched to.

**``applications.status_source`` carries a server default.** It is ``NOT NULL`` and this is
an ``ALTER TABLE ... ADD COLUMN`` against a table that already has rows, so a default is
required rather than stylistic — and it must be ``'manual'``, matching the model, because
every pre-existing status was either typed in by the user or written by the pipeline before
status sync existed, and ``manual`` is the value that protects both from being overwritten by
a lower-confidence inference.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa

from alembic import op
from app.database.types import GUID, JSONType, UTCDateTime

#: Identifier of this revision.
revision: str = "0002"

#: Revision this one builds on.
down_revision: str | None = "0001"

#: Branch labels, unused: this project keeps one linear history.
branch_labels: str | Sequence[str] | None = None

#: Dependencies on other version directories, unused.
depends_on: str | Sequence[str] | None = None


# ======================================================================================
# Column widths
#
# Enum widths are the longest member *value*, which is what sa.Enum(native_enum=False)
# computes when given no explicit length. Everything else mirrors a module constant in
# app/models/tracking.py.
# ======================================================================================

#: ``MailProvider`` — longest value ``"outlook"``.
ENUM_MAIL_PROVIDER: Final[int] = 7

#: ``SignalSource`` — longest value ``"email_outlook"``.
ENUM_SIGNAL_SOURCE: Final[int] = 13

#: ``SignalKind`` — longest values ``"application_received"`` / ``"assessment_requested"``.
ENUM_SIGNAL_KIND: Final[int] = 20

#: ``ApplicationStatus`` — longest value ``"needs_review"``. Shared with ``applications.
#: status``, because ``status_signals.detected_status`` reuses the same column type.
ENUM_APPLICATION_STATUS: Final[int] = 12

#: ``StatusSource`` — longest values ``"inferred"`` / ``"pipeline"``.
ENUM_STATUS_SOURCE: Final[int] = 8

#: RFC 5321 maximum email address length.
EMAIL_ADDRESS_MAX: Final[int] = 320

#: OS-keychain lookup key. Never a credential — see the column comment.
CREDENTIAL_REF_MAX: Final[int] = 255

#: Mailbox resume cursor: Gmail ``historyId``, IMAP ``UIDVALIDITY:UID``, Graph delta token.
CURSOR_MAX: Final[int] = 1024

#: Provider message identifier; half of the idempotency key, so it is stored whole.
EXTERNAL_REF_MAX: Final[int] = 512

#: ``From`` header as received — a display name *and* an address, not a bare address.
SENDER_MAX: Final[int] = 512

#: Registrable domain length per RFC 1035, matching ``companies.domain``.
SENDER_DOMAIN_MAX: Final[int] = 253

#: RFC 5322 header line limit; subjects longer than this are truncated by the model.
SUBJECT_MAX: Final[int] = 998

#: **Binding.** Retention cap on body text, per ``docs/CONTRACTS.md`` §17.8.3.
SNIPPET_MAX: Final[int] = 500


# ======================================================================================
# Server defaults
#
# Only columns whose models declare ``server_default=`` appear here, so that autogenerate
# never sees a difference between this migration and Base.metadata.
# ======================================================================================

#: Zero, for ``status_signals.confidence`` and both of its boolean flags.
ZERO: Final[str] = "0"

#: True, for ``email_accounts.enabled`` — connecting a mailbox switches it on.
TRUE: Final[str] = "1"

#: Provenance of a status nobody attributed, mirroring ``DEFAULT_STATUS_SOURCE``.
DEFAULT_STATUS_SOURCE: Final[str] = "manual"


# ======================================================================================
# Shared column groups (identical to 0001, restated so the two files stay independent)
# ======================================================================================


def _id_column() -> sa.Column:
    """Return the UUID primary key column shared by every table."""
    return sa.Column("id", GUID(), nullable=False)


def _user_id_column() -> sa.Column:
    """Return the owning-user foreign key column."""
    return sa.Column("user_id", GUID(), nullable=False)


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    """Return the ``created_at`` / ``updated_at`` audit columns."""
    return (
        sa.Column(
            "created_at",
            UTCDateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            UTCDateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    """Return the ``user_id -> users.id`` foreign key for *table*.

    ``CASCADE``: disconnecting is one thing, but deleting a user must not leave their
    mailboxes or the signals read from them behind.

    Args:
        table: The referencing table's name, used to build the constraint name.
    """
    return sa.ForeignKeyConstraint(
        ["user_id"],
        ["users.id"],
        name=f"fk_{table}_user_id_users",
        ondelete="CASCADE",
    )


def upgrade() -> None:
    """Create the status-sync tables and add the provenance columns to ``applications``."""
    _create_email_accounts()
    _create_status_signals()
    _add_application_status_provenance()


def downgrade() -> None:
    """Remove everything this revision added, in exact reverse order.

    ``DROP TABLE`` removes a table's own indexes and constraints on both backends, so those
    are not dropped individually. The three ``applications`` columns are dropped explicitly;
    none of them is indexed, so no index has to be unpicked first.
    """
    _drop_application_status_provenance()
    op.drop_table("status_signals")
    op.drop_table("email_accounts")


# ======================================================================================
# Mailboxes
# ======================================================================================


def _create_email_accounts() -> None:
    """Create ``email_accounts`` — the mailboxes the user explicitly connected.

    Nothing is read from any mailbox until a row exists here, and every provider is opened
    read-only (``gmail.readonly``, ``Mail.Read``, IMAP ``readonly=True``).
    """
    op.create_table(
        "email_accounts",
        _id_column(),
        _user_id_column(),
        sa.Column("provider", sa.String(ENUM_MAIL_PROVIDER), nullable=False),
        sa.Column("address", sa.String(EMAIL_ADDRESS_MAX), nullable=False),
        # A key into the OS keychain, never a token, password or refresh token. Golden-rule
        # adjacent: credentials must not touch the database (docs/CONTRACTS.md §17.8.4).
        sa.Column("credential_ref", sa.String(CREDENTIAL_REF_MAX), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=TRUE, nullable=False),
        sa.Column("last_sync_at", UTCDateTime(), nullable=True),
        sa.Column("cursor", sa.String(CURSOR_MAX), nullable=True),
        sa.Column("folders", JSONType(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_email_accounts"),
        _user_fk("email_accounts"),
        # One row per address per user: re-connecting the same mailbox must update the
        # existing row and its cursor, not start a second, competing sync.
        sa.UniqueConstraint(
            "user_id", "address", name="uq_email_accounts_user_id_address"
        ),
    )
    op.create_index("ix_email_accounts_user_id", "email_accounts", ["user_id"], unique=False)
    op.create_index(
        "ix_email_accounts_provider", "email_accounts", ["provider"], unique=False
    )
    op.create_index(
        "ix_email_accounts_created_at", "email_accounts", ["created_at"], unique=False
    )
    # The only scheduling query: "which of this user's mailboxes are still switched on?"
    op.create_index(
        "ix_email_accounts_user_id_enabled",
        "email_accounts",
        ["user_id", "enabled"],
        unique=False,
    )


# ======================================================================================
# Signals
# ======================================================================================


def _create_status_signals() -> None:
    """Create ``status_signals`` — one classified message per row, never a message body.

    The retained columns are exactly those ``docs/CONTRACTS.md`` §17.8.3 permits: message
    id, sender, subject, a snippet capped at 500 characters, the timestamp, and the
    classification. There is no column here in which a full body could be stored.
    """
    op.create_table(
        "status_signals",
        _id_column(),
        _user_id_column(),
        # -- subject ---------------------------------------------------------------------
        sa.Column("application_id", GUID(), nullable=True),
        sa.Column("source", sa.String(ENUM_SIGNAL_SOURCE), nullable=False),
        sa.Column("kind", sa.String(ENUM_SIGNAL_KIND), nullable=False),
        sa.Column("external_ref", sa.String(EXTERNAL_REF_MAX), nullable=False),
        # -- message metadata (never the body) --------------------------------------------
        sa.Column("sender", sa.String(SENDER_MAX), nullable=False),
        sa.Column("sender_domain", sa.String(SENDER_DOMAIN_MAX), nullable=False),
        sa.Column("subject", sa.String(SUBJECT_MAX), nullable=False),
        sa.Column("snippet", sa.String(SNIPPET_MAX), nullable=False),
        sa.Column("received_at", UTCDateTime(), nullable=False),
        # -- classification and disposition -------------------------------------------------
        sa.Column("detected_status", sa.String(ENUM_APPLICATION_STATUS), nullable=True),
        sa.Column("confidence", sa.Float(), server_default=ZERO, nullable=False),
        sa.Column("applied", sa.Boolean(), server_default=ZERO, nullable=False),
        sa.Column("needs_review", sa.Boolean(), server_default=ZERO, nullable=False),
        sa.Column("match_evidence", JSONType(), nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_status_signals"),
        _user_fk("status_signals"),
        # SET NULL, not CASCADE: a signal is evidence of what the mailbox said, and it
        # outlives the application row it was matched to.
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_status_signals_application_id_applications",
            ondelete="SET NULL",
        ),
        # The idempotency guarantee: the same message, from the same source, for the same
        # user, can only ever be one row. Without this a re-poll replays outcomes.
        sa.UniqueConstraint(
            "user_id",
            "source",
            "external_ref",
            name="uq_status_signals_user_id_source_external_ref",
        ),
    )
    op.create_index("ix_status_signals_user_id", "status_signals", ["user_id"], unique=False)
    op.create_index(
        "ix_status_signals_application_id",
        "status_signals",
        ["application_id"],
        unique=False,
    )
    op.create_index("ix_status_signals_source", "status_signals", ["source"], unique=False)
    op.create_index("ix_status_signals_kind", "status_signals", ["kind"], unique=False)
    op.create_index(
        "ix_status_signals_sender_domain",
        "status_signals",
        ["sender_domain"],
        unique=False,
    )
    op.create_index(
        "ix_status_signals_received_at", "status_signals", ["received_at"], unique=False
    )
    op.create_index(
        "ix_status_signals_created_at", "status_signals", ["created_at"], unique=False
    )
    # The two scans every sync pass performs: what has already been acted on, and what is
    # waiting for a human.
    op.create_index(
        "ix_status_signals_user_id_applied",
        "status_signals",
        ["user_id", "applied"],
        unique=False,
    )
    op.create_index(
        "ix_status_signals_user_id_needs_review",
        "status_signals",
        ["user_id", "needs_review"],
        unique=False,
    )


# ======================================================================================
# Application status provenance
# ======================================================================================


def _add_application_status_provenance() -> None:
    """Add ``last_synced_at`` / ``status_source`` / ``status_confidence`` to ``applications``.

    Together these answer "how do we know?" about ``applications.status``. Without them an
    outcome read out of a mailbox is indistinguishable from one the user typed in, and the
    sync service has no basis on which to refuse to overwrite the latter with the former.

    ``status_source`` is ``NOT NULL`` with a server default because this is an
    ``ALTER TABLE ... ADD COLUMN`` on a populated table; ``'manual'`` is the correct
    backfill, not merely a convenient one, since every status that predates status sync was
    written either by a human or by the pipeline on that human's behalf.
    """
    op.add_column("applications", sa.Column("last_synced_at", UTCDateTime(), nullable=True))
    op.add_column(
        "applications",
        sa.Column(
            "status_source",
            sa.String(ENUM_STATUS_SOURCE),
            server_default=DEFAULT_STATUS_SOURCE,
            nullable=False,
        ),
    )
    op.add_column("applications", sa.Column("status_confidence", sa.Float(), nullable=True))


def _drop_application_status_provenance() -> None:
    """Remove the three provenance columns from ``applications``.

    Dropped in reverse order of addition. SQLite has supported
    ``ALTER TABLE ... DROP COLUMN`` since 3.35 (2021), and none of these columns participates
    in an index or a constraint, which are the two cases SQLite still refuses to drop.
    """
    op.drop_column("applications", "status_confidence")
    op.drop_column("applications", "status_source")
    op.drop_column("applications", "last_synced_at")
