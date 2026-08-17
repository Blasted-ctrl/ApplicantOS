"""Application schemas — the list row, the detail view, the audit trail, and the review queue.

An application is the row that guarantees ApplicantOS never applies to the same job twice,
and the row a user reaches for when they ask "what actually happened?". Both jobs are
reflected here:

* :class:`ApplicationRead` is the list projection: status, timing, and enough nested context
  (:class:`~app.schemas.posting.PostingSummary`, :class:`~app.schemas.posting.CompanyRead`,
  :class:`~app.schemas.scoring.ScoreRead`) to render a row without a second request.
* :class:`ApplicationDetail` adds everything the detail screen needs: the append-only event
  trail, the exact documents submitted, the artifact links, the answers given, and the
  model's account of how it answered them.
* :class:`ReviewItem` and :class:`ReviewResolve` are the human-in-the-loop path. Golden rule
  #2 — never guess — means an application stops and asks whenever a required field is
  unanswerable, confidence is low, or a captcha/MFA/login wall appears. ``unanswered_fields``
  is exactly what it stopped on.

**Nesting rules.** ``posting``, ``company``, ``resume_version``, ``cover_letter`` and
``events`` all correspond to eagerly loaded (``selectin``) relationships on
:class:`~app.models.application.Application`, so validating an ORM instance directly is safe
inside an async session. ``score`` and ``artifacts`` are *not* ORM attributes; they default
to empty and the service layer fills them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.models.enums import (
    ApplicationStatus,
    ATSProviderName,
    DocumentKind,
    FieldKind,
    ReviewReason,
)
from app.schemas.common import PaginationParams, Schema
from app.schemas.posting import CompanyRead, PostingSummary
from app.schemas.resume import ResumeVersionRead
from app.schemas.scoring import ScoreRead

__all__ = [
    "ApplicationDetail",
    "ApplicationEventRead",
    "ApplicationFilter",
    "ApplicationRead",
    "ApplicationUpdate",
    "ArtifactRead",
    "CoverLetterRead",
    "ReviewField",
    "ReviewItem",
    "ReviewResolve",
]


class ApplicationEventRead(Schema):
    """One append-only entry in an application's audit trail.

    Events are never updated and never deleted independently: they exist so the exact
    sequence of a submission survives long after the logs have rotated.
    """

    id: uuid.UUID
    application_id: uuid.UUID
    kind: str = Field(
        description="Machine-readable token: 'prepared', 'submitted', 'needs_review'.",
    )
    message: str | None = Field(default=None, description="One-liner for the timeline.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured context: field names, errors, decisions.",
    )
    at: datetime = Field(description="When the event happened, not when it was written.")
    created_at: datetime


class CoverLetterRead(Schema):
    """A cover letter drafted for one posting, optionally attached to one application.

    ``body`` is the canonical artifact and is retained; the rendered PDF is disposable, so
    ``file_id`` and ``download_url`` are frequently ``None``.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    posting_id: uuid.UUID
    application_id: uuid.UUID | None = None
    body: str = Field(description="The letter itself, in plain text.")
    tone: str = Field(default="professional", description="Register it was written in.")
    file_id: uuid.UUID | None = None
    download_url: str | None = Field(
        default=None,
        description="Link to the rendered artifact; set by the API layer.",
    )
    token_usage: dict[str, Any] = Field(default_factory=dict)
    word_count: int = Field(default=0, description="Whitespace-delimited word count.")
    created_at: datetime
    updated_at: datetime


class ArtifactRead(Schema):
    """A stored file produced or consumed by an application.

    Screenshots captured before and after submission, the rendered resume, the rendered
    cover letter. ``url`` points at the download endpoint rather than at the storage
    backend, so a local install and an S3 install expose the same shape.
    """

    id: uuid.UUID
    kind: DocumentKind = DocumentKind.OTHER
    filename: str
    content_type: str | None = None
    size_bytes: int = 0
    url: str | None = Field(default=None, description="Download link; set by the API layer.")
    created_at: datetime


class ApplicationRead(Schema):
    """One user's application to one posting — the list projection."""

    id: uuid.UUID
    user_id: uuid.UUID
    posting_id: uuid.UUID
    company_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = Field(
        default=None,
        description="Automation run that produced this, when there was one.",
    )
    status: ApplicationStatus = ApplicationStatus.DRAFT
    submitted_at: datetime | None = None
    duration_seconds: float | None = Field(
        default=None,
        description="Wall-clock seconds the browser flow took.",
    )
    confirmation_id: str | None = Field(
        default=None,
        description="Employer-issued reference, when the flow surfaces one.",
    )
    external_application_id: str | None = None
    review_reason: ReviewReason | None = Field(
        default=None,
        description="Why this stopped and asked for a human. None unless in review.",
    )
    attempt_count: int = 0
    last_error: str | None = None
    notes: str | None = Field(default=None, description="Free-text notes written by the user.")

    # -- nested context (eagerly loaded on the ORM side, or filled by the service) -----
    posting: PostingSummary | None = None
    company: CompanyRead | None = None
    score: ScoreRead | None = Field(
        default=None,
        description="Fit score for this posting and user; filled by the service layer.",
    )

    # -- derived predicates (plain Python properties on the ORM model) -----------------
    can_submit: bool = Field(
        default=True,
        description="Whether a submission may still be attempted. Golden rule #1.",
    )
    needs_review: bool = Field(
        default=False,
        description="Whether this is parked waiting on a human decision.",
    )

    created_at: datetime
    updated_at: datetime


class ApplicationDetail(ApplicationRead):
    """Everything the application detail screen renders.

    Adds the audit trail, the documents actually submitted, the artifact links, the answers
    given to the form, and the model's account of how it answered them. ``ai_reasoning`` is
    advisory: it explains the decisions, it never justifies them after the fact.
    """

    resume_version: ResumeVersionRead | None = Field(
        default=None,
        description=(
            "The generated resume version. Actually submitted **unless** "
            "`submitted_resume_filename` is set, in which case the applicant's own upload "
            "went instead and this is history rather than evidence."
        ),
    )
    submitted_resume_filename: str | None = Field(
        default=None,
        description=(
            "Name of the applicant's own uploaded resume, when `resume_source='master'` "
            "sent that rather than the generated view. None means `resume_version` is what "
            "the employer received — **or** that nothing was sent at all: this is populated "
            "only once the application reaches a post-submit status, so a rehearsal never "
            "reports a submission."
        ),
    )
    cover_letter: CoverLetterRead | None = Field(
        default=None,
        description="The cover letter actually submitted, when one was used.",
    )
    events: list[ApplicationEventRead] = Field(
        default_factory=list,
        description="Append-only audit trail, in chronological order.",
    )
    artifacts: list[ArtifactRead] = Field(
        default_factory=list,
        description="Screenshots and rendered documents; filled by the service layer.",
    )
    artifact_urls: list[str] = Field(
        default_factory=list,
        description="Flat list of artifact download links, for quick rendering.",
    )
    answers: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Answers a human settled, keyed by field selector or by the label they saw. "
            "An input to the next attempt, not a record of the last one."
        ),
    )
    submitted_answers: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "What was actually written into the form, keyed by the question as the page "
            "asked it. The record of what was submitted under the user's name."
        ),
    )
    ai_reasoning: str | None = Field(
        default=None,
        description="Model's account of how it answered the form. Advisory only.",
    )
    review_payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Context captured when the application was routed to review.",
    )
    browser_log: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Ordered browser action log captured during the flow.",
    )
    confirmation_text: str | None = Field(
        default=None,
        description="Verbatim success message scraped from the confirmation page.",
    )
    confirmation_screenshot_id: uuid.UUID | None = Field(
        default=None,
        description="Proof-of-submission screenshot captured by ApplicationVerifier.",
    )


class ApplicationUpdate(Schema):
    """Partial update to an application — the body of ``PATCH /applications/{id}``.

    Only the two fields a *human* legitimately owns are writable. Everything else
    (``submitted_at``, ``attempt_count``, ``answers``, the confirmation fields) is written by
    the pipeline and must not be settable over HTTP, or the never-apply-twice guard could be
    defeated by editing the status of a submitted application back to ``draft``.

    ``status`` is accepted because outcomes arrive out of band: only the user knows they were
    rejected, invited to interview, offered, or ghosted. The service validates the requested
    transition; this schema only checks that the value is a real status.

    Apply with ``model_dump(exclude_unset=True)``.
    """

    status: ApplicationStatus | None = Field(
        default=None,
        description="Outcome reported by the user; the service validates the transition.",
    )
    notes: str | None = None


class ApplicationFilter(PaginationParams):
    """Query parameters for ``GET /applications``."""

    q: str | None = Field(
        default=None,
        description="Free-text search across posting title and company name.",
    )
    status: ApplicationStatus | None = None
    provider: ATSProviderName | None = Field(
        default=None,
        description="Restrict to applications whose posting came from this provider.",
    )
    company: str | None = Field(
        default=None,
        description="Company name; matched against the normalised form.",
    )
    session_id: uuid.UUID | None = Field(
        default=None,
        description="Restrict to one automation run.",
    )
    review_reason: ReviewReason | None = None
    needs_review: bool | None = Field(
        default=None,
        description="True selects only applications parked awaiting a human.",
    )
    since: datetime | None = Field(default=None, description="Created at or after this.")
    until: datetime | None = Field(default=None, description="Created at or before this.")

    @field_validator("q", "company")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """Treat an all-whitespace filter as absent, so a cleared search box matches all."""
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


# ======================================================================================
# Review queue
# ======================================================================================


class ReviewField(Schema):
    """A form field the automation refused to answer.

    Mirrors :class:`app.jobs.base.FormField`. The refusal is the point: an unanswerable
    required field, an ambiguous select, or an answer below
    ``settings.min_answer_confidence`` routes here instead of being guessed at.
    """

    selector: str = Field(description="CSS/accessibility selector locating the input.")
    label: str = Field(default="", description="Label as it appears on the form.")
    kind: FieldKind = Field(
        default=FieldKind.UNKNOWN,
        description="Widget type; `unknown` always escalates rather than being filled.",
    )
    required: bool = Field(default=False, description="Whether the form demands a value.")
    options: list[str] = Field(
        default_factory=list,
        description="Allowed values for select-like fields.",
    )
    max_length: int | None = Field(default=None, description="Length cap the form enforces.")
    hint: str | None = Field(default=None, description="Helper text shown beside the field.")
    suggested_answer: str | None = Field(
        default=None,
        description="What the answerer would have said, and the confidence it rejected.",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence of `suggested_answer`; below the floor, hence the stop.",
    )


class ReviewItem(Schema):
    """One entry in the human review queue — ``GET /reviews``.

    Everything a reviewer needs to decide in one screen: what the application is, why it
    stopped, exactly which fields are unanswered, and the raw context the pipeline captured
    at the moment it stopped.
    """

    application: ApplicationRead
    reason: ReviewReason | None = Field(
        default=None,
        description="Why the application stopped and asked for a human.",
    )
    unanswered_fields: list[ReviewField] = Field(
        default_factory=list,
        description="The fields the automation refused to guess at.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw review context captured at the moment of the stop.",
    )


class ReviewResolve(Schema):
    """Body of ``POST /reviews/{id}/resolve`` — a human's answers to the open questions.

    Keys are the ``selector`` (or resolved field identifier) of each
    :class:`ReviewField`; values are whatever that field kind expects. Answers supplied here
    are also recorded as a
    :class:`~app.models.knowledge.MemoryEntry` correction, so the same question answers
    itself next time.
    """

    answers: dict[str, Any] = Field(
        default_factory=dict,
        description="Field identifier to the value the reviewer supplied.",
    )
