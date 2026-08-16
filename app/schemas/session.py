"""Run-session schemas — the live dashboard's data shape.

A :class:`~app.models.session.RunSession` is a rollup: individual applications carry the
detail, while the session carries the six counters that answer "how is this run going?" in a
single row. That is what lets the dashboard paint a progress bar without aggregating over a
growing table on every WebSocket tick.

:class:`SessionRead` therefore exposes **every** counter, not a curated subset — the desktop
session screen renders all of them, and a client that has to compute ``failures`` from
somewhere else will compute it differently from the server.

:class:`SessionDetail` adds the applications produced by the run.

Note:
    ``applications`` on :class:`SessionDetail` mirrors a **lazily loaded** relationship.
    Validate a :class:`~app.models.session.RunSession` into this model only after eagerly
    loading that collection (``selectinload(RunSession.applications)``); otherwise pydantic's
    attribute read triggers implicit IO and raises ``MissingGreenlet`` inside an async
    session. :class:`SessionRead` has no such requirement and is always safe.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from pydantic import Field

from app.models.enums import SessionStatus, StopReason
from app.schemas.application import ApplicationRead
from app.schemas.common import Schema
from app.schemas.posting import DiscoverRequest

__all__ = [
    "DEFAULT_SESSION_TRIGGER",
    "MAX_SCORE",
    "SessionDetail",
    "SessionRead",
    "SessionStartRequest",
]

#: Trigger recorded when the caller does not name one. Mirrors
#: :data:`app.models.session.DEFAULT_SESSION_TRIGGER`.
DEFAULT_SESSION_TRIGGER: Final[str] = "manual"

#: Top of the normalised score band. Mirrors
#: :data:`app.services.session_service.MAX_SCORE`.
MAX_SCORE: Final[int] = 100


class SessionRead(Schema):
    """One automation run: its status, its timing, and every rollup counter.

    ``duration_seconds`` is a hybrid on the ORM model — it measures against *now* while the
    run is still executing, and against ``ended_at`` once it has stopped — so a live
    dashboard shows a ticking figure without the server writing to the row.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    status: SessionStatus = SessionStatus.RUNNING
    started_at: datetime
    ended_at: datetime | None = Field(
        default=None,
        description="None while running; the watchdog reaps stale NULLs.",
    )

    # -- stopping -----------------------------------------------------------------------
    stop_reason: StopReason | None = Field(
        default=None,
        description="Why the run stopped. None while running.",
    )
    stop_requested_at: datetime | None = Field(
        default=None,
        description="When a stop was asked for; set before the run actually closes.",
    )
    stop_sentence: str | None = Field(
        default=None,
        description=(
            "The stop reason as one sentence with its number filled in, for display. "
            "None while running. Never the word 'Done.'"
        ),
    )

    # -- what the user asked for ---------------------------------------------------------
    max_applications: int | None = Field(
        default=None,
        description="Cap for this run alone; None means the configured session cap governs.",
    )
    match_threshold: int | None = Field(
        default=None,
        description="Minimum normalised score for this run; None means use the setting.",
    )

    # -- rollup counters ----------------------------------------------------------------
    jobs_found: int = Field(default=0, description="Postings discovered, before dedupe.")
    jobs_scored: int = Field(default=0, description="Postings this run put a score on.")
    jobs_qualified: int = Field(default=0, description="Postings that cleared the threshold.")
    resumes_generated: int = Field(default=0, description="Tailored versions produced.")
    applications_started: int = Field(
        default=0,
        description="Applications this run opened a browser for.",
    )
    applications_completed: int = Field(
        default=0,
        description="Applications that reached a submitted state.",
    )
    applications_skipped: int = Field(
        default=0,
        description="Postings passed over: already applied to, below the floor, or capped.",
    )
    manual_review: int = Field(
        default=0,
        description="Applications routed to a human rather than guessed at.",
    )
    failures: int = Field(default=0, description="Applications that failed outright.")
    avg_application_seconds: float | None = Field(
        default=None,
        description="Running mean of completed durations; None until the first sample.",
    )

    # -- provenance ---------------------------------------------------------------------
    token_usage: dict[str, Any] = Field(
        default_factory=dict,
        description="Cumulative LLM token counts, keyed by model or usage kind.",
    )
    config_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="Effective settings and preferences at start; keeps a run reproducible.",
    )
    trigger: str = Field(
        default=DEFAULT_SESSION_TRIGGER,
        description="What started this run: 'manual', 'schedule', 'api'.",
    )
    duration_seconds: float | None = Field(
        default=None,
        description="Elapsed seconds; measured against now while the run is executing.",
    )

    created_at: datetime
    updated_at: datetime

    @property
    def is_running(self) -> bool:
        """Whether this run is still executing."""
        return self.status is SessionStatus.RUNNING


class SessionDetail(SessionRead):
    """A run plus the applications it produced.

    See the module note: ``applications`` mirrors a lazily loaded relationship and must be
    eagerly loaded before an ORM instance is validated into this model.
    """

    applications: list[ApplicationRead] = Field(
        default_factory=list,
        description="Applications produced by this run; requires an eager load.",
    )
    checkpoints_pending: int = Field(
        default=0,
        description="Resumable checkpoints still outstanding for this run.",
    )
    stats: dict[str, int] = Field(
        default_factory=dict,
        description="Derived aggregates from SessionService.stats(); not stored on the row.",
    )


class SessionStartRequest(Schema):
    """Body of ``POST /sessions/start`` — begin one automation run.

    ``discover`` is optional: omitting it starts a run that processes postings already
    discovered and scored, which is the common case after an overnight poll.

    The three override fields exist so a user can start a *tighter* run than their standing
    configuration without editing settings first. They are one-directional by design —
    ``auto_apply`` and ``dry_run`` can only ever narrow what a run is permitted to do,
    because ``settings.is_submission_allowed`` (the server-side kill switch) is evaluated
    independently and always wins. A request cannot turn submission on.
    """

    trigger: str = Field(
        default=DEFAULT_SESSION_TRIGGER,
        description="What is starting this run: 'manual', 'schedule', 'api'.",
    )
    discover: DiscoverRequest | None = Field(
        default=None,
        description="Run discovery first; omit to process already-scored postings.",
    )
    auto_apply: bool | None = Field(
        default=None,
        description="Narrow this run to no submission. Cannot enable it.",
    )
    dry_run: bool | None = Field(
        default=None,
        description="Force dry-run for this session. Cannot disable the server's dry_run.",
    )
    max_applications: int | None = Field(
        default=None,
        ge=0,
        description="Cap for this run; the lower of this and the configured cap wins.",
    )
    match_threshold: int | None = Field(
        default=None,
        ge=0,
        le=MAX_SCORE,
        description=(
            "Minimum normalised score (0-100) for this run. The higher of this and "
            "`auto_apply_min_score` wins — a run can only ever be pickier than the setting."
        ),
    )
