"""Status-sync schemas — connected mailboxes, observed signals, and what a sync did.

The HTTP surface of ``docs/CONTRACTS.md`` §17: the six routes under
``/api/v1/tracking`` accept and return exactly the models declared here, and
``desktop/src/lib/api/types.ts`` mirrors them.

One rule dominates this module and is not negotiable. **``credential_ref`` is never a
response field.** It names an entry in the OS keychain, and while the reference is not
itself a secret, publishing it hands any reader the exact lookup key for one. Neither
:class:`EmailAccountCreate` nor :class:`EmailAccountRead` carries it: the write side accepts
an optional :attr:`EmailAccountCreate.secret`, which the service hands straight to the
keychain and never persists, and the read side reports the boolean
:attr:`EmailAccountRead.connected` instead — derived from
:attr:`~app.models.tracking.EmailAccount.connected` on the ORM model, so the projection
cannot drift from the column.

:class:`StatusSignalRead` is likewise a *bounded* projection of a message: sender, subject,
a snippet already capped at 500 characters by the model, and the classification. There is no
field here through which a full message body could reach a client, because there is no
column holding one (§17.8.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, SecretStr, field_validator

from app.models.enums import (
    ApplicationStatus,
    MailProvider,
    SignalKind,
    SignalSource,
)
from app.schemas.common import PaginationParams, Schema

__all__ = [
    "MAX_CONFIDENCE",
    "MIN_CONFIDENCE",
    "EmailAccountCreate",
    "EmailAccountRead",
    "SignalFilter",
    "SignalResolve",
    "StatusSignalRead",
    "SyncReportRead",
    "SyncRequest",
    "TrackingStats",
]

#: Confidence bounds shared by every field in this module. Stated once so a schema and the
#: ``settings.status_sync_min_confidence`` gate cannot disagree about the scale.
MIN_CONFIDENCE: float = 0.0
MAX_CONFIDENCE: float = 1.0


# ======================================================================================
# Mailboxes
# ======================================================================================


class EmailAccountCreate(Schema):
    """Body of ``POST /tracking/accounts`` — connect one mailbox, read-only.

    Connecting is the explicit act that switches status sync on for this user; nothing is
    read from any mailbox before a row exists (§17.8.6).

    ``secret`` exists for generic IMAP, where the only credential is a password or an
    app-specific password and there is no browser flow to obtain one. It is a
    :class:`~pydantic.SecretStr`, so it cannot be printed by an accidental ``repr`` or dump,
    and the service writes it to the OS keychain and never to the database. Gmail and
    Outlook omit it: their tokens arrive from the OAuth callback, which asks for read-only
    scopes (``gmail.readonly``, ``Mail.Read``).
    """

    provider: MailProvider = Field(
        description="Mailbox implementation to use: gmail, outlook, or generic IMAP.",
    )
    address: str = Field(
        min_length=3,
        description="The mailbox address; unique per user.",
    )
    secret: SecretStr | None = Field(
        default=None,
        description=(
            "IMAP password or app-specific password. Stored in the OS keychain, never in "
            "the database. Omitted for OAuth providers."
        ),
    )
    folders: list[str] = Field(
        default_factory=list,
        description="Folders or labels to search. Empty means the provider's inbox scope.",
    )
    enabled: bool = Field(
        default=True,
        description="Whether the poller may read this mailbox once it is connected.",
    )

    @field_validator("address")
    @classmethod
    def _normalize_address(cls, value: str) -> str:
        """Fold the address to lowercase and reject a blank one.

        Mirrors the same normalisation on
        :class:`~app.models.tracking.EmailAccount`, so ``UNIQUE(user_id, address)`` sees one
        spelling rather than two and a re-connect updates the existing row.

        Args:
            value: The submitted address.

        Returns:
            The folded address.

        Raises:
            ValueError: If the address is blank or has no ``@``.
        """
        trimmed = value.strip().lower()
        if "@" not in trimmed or trimmed.startswith("@") or trimmed.endswith("@"):
            raise ValueError("address must be an email address")
        return trimmed


class EmailAccountRead(Schema):
    """A connected mailbox as the desktop app sees it.

    Carries no credential and no keychain reference — :attr:`connected` is the entire
    answer the client needs, and it is the ORM model's own derived property.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    provider: MailProvider
    address: str
    enabled: bool = Field(default=True, description="Whether the poller may read this.")
    connected: bool = Field(
        default=False,
        description="Whether a credential exists in the OS keychain for this account.",
    )
    last_sync_at: datetime | None = Field(
        default=None,
        description="When this mailbox was last polled successfully; None means never.",
    )
    folders: list[str] = Field(
        default_factory=list,
        description="Folders or labels searched.",
    )
    last_error: str | None = Field(
        default=None,
        description="Message from the most recent failed poll.",
    )
    created_at: datetime
    updated_at: datetime


# ======================================================================================
# Signals
# ======================================================================================


class StatusSignalRead(Schema):
    """One classified message that says something about an application's outcome.

    ``snippet`` is at most 500 characters and is the only body-derived text that exists
    anywhere in the system after classification finishes.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    application_id: uuid.UUID | None = Field(
        default=None,
        description="Application this was matched to; None while unmatched.",
    )
    source: SignalSource
    kind: SignalKind = SignalKind.UNKNOWN
    external_ref: str = Field(description="Provider message id; the idempotency key.")
    sender: str = Field(default="", description="From header as received.")
    sender_domain: str = Field(default="", description="Domain of the sender address.")
    subject: str = Field(default="", description="Decoded subject line.")
    snippet: str = Field(
        default="",
        max_length=500,
        description="At most 500 characters of body. Full bodies are never stored.",
    )
    received_at: datetime = Field(description="When the message arrived, per the provider.")
    detected_status: ApplicationStatus | None = Field(
        default=None,
        description="Status the kind implies; None for 'viewed' and 'unknown'.",
    )
    confidence: float = Field(
        default=MIN_CONFIDENCE,
        ge=MIN_CONFIDENCE,
        le=MAX_CONFIDENCE,
        description="Combined classification and match confidence.",
    )
    applied: bool = Field(
        default=False,
        description="Whether this already moved the matched application's status.",
    )
    needs_review: bool = Field(
        default=False,
        description="Whether a human must confirm before this may be applied.",
    )
    match_evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Why the matcher chose this application: domains, scores, ids, window.",
    )
    created_at: datetime
    updated_at: datetime


class SignalFilter(PaginationParams):
    """Query parameters for ``GET /tracking/signals``."""

    source: SignalSource | None = None
    kind: SignalKind | None = None
    application_id: uuid.UUID | None = Field(
        default=None,
        description="Restrict to signals matched to one application.",
    )
    applied: bool | None = Field(
        default=None,
        description="True selects only signals that already moved a status.",
    )
    needs_review: bool | None = Field(
        default=None,
        description="True selects only signals awaiting human confirmation.",
    )
    since: datetime | None = Field(default=None, description="Received at or after this.")
    until: datetime | None = Field(default=None, description="Received at or before this.")


class SignalResolve(Schema):
    """Body of ``POST /tracking/signals/{id}/resolve`` — a human's decision about a signal.

    Reached when the matcher's confidence fell below
    ``settings.status_sync_min_confidence`` or when no application matched well enough.
    Both fields are required on purpose: the whole reason this signal is in the queue is
    that the system would have had to guess at one of them, and asking the human for half an
    answer would just move the guess one level up. Dismissing a signal instead is a separate
    endpoint.
    """

    application_id: uuid.UUID = Field(
        description="The application this signal actually refers to.",
    )
    status: ApplicationStatus = Field(
        description="The status that application should now be in.",
    )


# ======================================================================================
# Sync runs and aggregate state
# ======================================================================================


class SyncRequest(Schema):
    """Body of ``POST /tracking/sync`` — run a sync now.

    An empty body syncs every enabled mailbox from each one's own cursor, which is what the
    desktop app sends on launch.
    """

    account_id: uuid.UUID | None = Field(
        default=None,
        description="Sync only this mailbox. None syncs every enabled account.",
    )
    since: datetime | None = Field(
        default=None,
        description=(
            "Override the incremental window and search from this instant instead. "
            "Bounded by settings.status_sync_lookback_days."
        ),
    )


class SyncReportRead(Schema):
    """What one sync of one mailbox did — the serialised form of ``SyncReport``.

    The counters narrow monotonically: every fetched message is either classified or
    skipped, every classified one is either matched or skipped, and every matched one is
    either applied or routed to review. A run that fetched messages and applied nothing is a
    normal, healthy outcome — most mail in a lookback window is not about an application.
    """

    account_id: uuid.UUID | None = Field(
        default=None,
        description="Mailbox this report covers; None for a whole-user roll-up.",
    )
    fetched: int = Field(default=0, ge=0, description="Messages read from the provider.")
    classified: int = Field(default=0, ge=0, description="Messages the classifier scored.")
    matched: int = Field(default=0, ge=0, description="Signals bound to an application.")
    applied: int = Field(default=0, ge=0, description="Signals that moved a status.")
    needs_review: int = Field(
        default=0,
        ge=0,
        description="Signals parked for human confirmation rather than guessed at.",
    )
    skipped: int = Field(
        default=0,
        ge=0,
        description="Messages already seen, or carrying no application information.",
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock seconds the run took.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-account failures. A non-empty list does not invalidate the counts.",
    )


class TrackingStats(Schema):
    """Aggregate status-sync state — what the tracking panel renders in one request."""

    accounts: int = Field(default=0, ge=0, description="Mailboxes connected.")
    accounts_enabled: int = Field(
        default=0,
        ge=0,
        description="Connected mailboxes the poller is allowed to read.",
    )
    signals_total: int = Field(default=0, ge=0, description="Signals recorded, all time.")
    signals_applied: int = Field(
        default=0,
        ge=0,
        description="Signals that moved an application's status.",
    )
    signals_pending_review: int = Field(
        default=0,
        ge=0,
        description="Signals waiting for one-click human confirmation.",
    )
    signals_by_kind: dict[str, int] = Field(
        default_factory=dict,
        description="Signal counts keyed by SignalKind value.",
    )
    applications_synced: int = Field(
        default=0,
        ge=0,
        description="Applications whose current status came from a signal.",
    )
    last_sync_at: datetime | None = Field(
        default=None,
        description="Most recent successful poll across every mailbox.",
    )
