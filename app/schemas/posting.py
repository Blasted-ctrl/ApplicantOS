"""Job posting, company, and discovery schemas.

``job_postings`` is the entry point of the whole pipeline, and the discovery feed is the
busiest screen in the desktop app, so these models are shaped around that read path:

* :class:`PostingSummary` is what a list row needs — title, employer, location, arrangement,
  freshness, status, score. Small enough that a 200-row page stays under a few tens of
  kilobytes, which is what keeps virtualised scrolling at 60fps.
* :class:`PostingRead` adds the description and the deduplication bookkeeping for the detail
  view.
* :class:`PostingFilter` is the query behind ``GET /postings``, and
  :class:`DiscoverRequest` is the body of ``POST /postings/discover``.

``raw_json`` is deliberately **not** exposed. It exists so a parser bug can be fixed and
re-run without re-crawling; it is provider-shaped, unbounded, and of no use to a client.

``score`` is not an attribute of :class:`~app.models.posting.JobPosting` — a posting has a
*collection* of per-user scores — so the field defaults to ``None`` and the service layer
populates it with the requesting user's score. Naming the field ``scores`` would instead
make pydantic read the ORM relationship, which is the wrong one and the wrong shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.models.enums import (
    ATSProviderName,
    EmploymentType,
    PostingStatus,
    WorkArrangement,
)
from app.schemas.common import PaginationParams, Schema
from app.schemas.scoring import (
    MAX_NORMALIZED_SCORE,
    MIN_NORMALIZED_SCORE,
    ScoreRead,
)

__all__ = [
    "DEFAULT_DISCOVER_LIMIT",
    "CompanyRead",
    "DiscoverRequest",
    "PostingFilter",
    "PostingRead",
    "PostingSummary",
]

#: Postings requested per provider when a discovery run does not say otherwise. Sized so a
#: manual run finishes in seconds rather than minutes, since the user is watching it.
DEFAULT_DISCOVER_LIMIT: int = 100

#: Hard ceiling on a single discovery request, so one call cannot exhaust a provider's rate
#: limit budget for the rest of the day.
MAX_DISCOVER_LIMIT: int = 1000

#: Widest "posted within" window a discovery request may ask for. Beyond a quarter, postings
#: are overwhelmingly stale or filled.
MAX_POSTED_WITHIN_DAYS: int = 90


class CompanyRead(Schema):
    """An employer, deduplicated by ``normalized_name``.

    Reference data: the same row is referenced by every posting from that employer and every
    application sent there, which is what makes "applications per company" and "block this
    company" a join rather than a string match.
    """

    id: uuid.UUID
    name: str = Field(description="Display name, exactly as the provider spelled it.")
    normalized_name: str = Field(description="Deduplication key; one row per employer.")
    domain: str | None = None
    industry: str | None = Field(
        default=None,
        description="Free-text label matched against blocked_industries.",
    )
    size_bucket: str | None = Field(default=None, description='Headcount band, e.g. "11-50".')
    employee_count: int | None = None
    is_defense: bool = Field(
        default=False,
        description="Defence or weapons contractor; drives the exclude_defense policy.",
    )
    is_startup: bool = False
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    enriched_at: datetime | None = Field(
        default=None,
        description="When metadata was last refreshed; None means never enriched.",
    )
    created_at: datetime
    updated_at: datetime


class PostingSummary(Schema):
    """The list-row projection of a posting.

    Used by the discovery feed and nested inside :class:`~app.schemas.application.
    ApplicationRead`, so that an applications list does not drag every job description
    across the wire.
    """

    id: uuid.UUID
    title: str
    provider: ATSProviderName
    url: str
    apply_url: str | None = None
    location: str | None = None
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    posted_at: datetime | None = None
    closes_at: datetime | None = None
    status: PostingStatus = PostingStatus.DISCOVERED
    company_id: uuid.UUID | None = None
    company: CompanyRead | None = Field(
        default=None,
        description="Eagerly loaded employer; None until company resolution has run.",
    )
    is_open: bool = Field(
        default=True,
        description="False only when a closing date exists and has passed.",
    )
    created_at: datetime
    updated_at: datetime


class PostingRead(PostingSummary):
    """A posting in full, for the detail view.

    Adds the advertised description and the deduplication bookkeeping. ``raw_json`` remains
    server-side; ``score`` carries the requesting user's fit score when one has been
    computed.
    """

    external_id: str = Field(description="Provider-native identifier.")
    description: str | None = Field(
        default=None,
        description="Full advertised text, already stripped of markup.",
    )
    content_hash: str | None = Field(
        default=None,
        description="Hash of the advertised text; a change invalidates cached scores.",
    )
    dedupe_key: str | None = Field(
        default=None,
        description="Cross-provider identity key; None until dedupe has run.",
    )
    score: ScoreRead | None = Field(
        default=None,
        description="The requesting user's fit score, populated by the service layer.",
    )


class PostingFilter(PaginationParams):
    """Query parameters for ``GET /postings``.

    Inherits ``limit``/``offset`` so a route declares one dependency rather than two.
    Every field is optional: the unfiltered call is the discovery feed's default view.
    """

    q: str | None = Field(
        default=None,
        description="Free-text search across title, company and description.",
    )
    provider: ATSProviderName | None = None
    status: PostingStatus | None = None
    min_score: int | None = Field(
        default=None,
        ge=MIN_NORMALIZED_SCORE,
        le=MAX_NORMALIZED_SCORE,
        description="Only postings the requesting user scored at or above this.",
    )
    company: str | None = Field(
        default=None,
        description="Company name; matched against the normalised form, not verbatim.",
    )
    since: datetime | None = Field(
        default=None,
        description="Only postings published at or after this instant.",
    )
    remote_only: bool = Field(
        default=False,
        description="Restrict to WorkArrangement.REMOTE.",
    )

    @field_validator("q", "company")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """Treat an all-whitespace filter as absent.

        A UI that clears a search box commonly sends ``""``; without this, that would be
        interpreted as "match the empty string" and return nothing.
        """
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class DiscoverRequest(Schema):
    """Body of ``POST /postings/discover`` — one manual discovery run.

    Maps onto :class:`app.jobs.base.SearchQuery` plus the provider selection.
    ``providers`` defaults to empty, which the service reads as "every provider this user
    has enabled in their preferences" rather than "no providers" — the alternative would
    make the common call site restate configuration it already stored.
    """

    providers: list[ATSProviderName] = Field(
        default_factory=list,
        description="Providers to poll; empty means the user's enabled providers.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Search terms; empty means the user's preferred_keywords.",
    )
    locations: list[str] = Field(
        default_factory=list,
        description="Location filters; empty means the user's preferred_locations.",
    )
    remote_only: bool = False
    posted_within_days: int | None = Field(
        default=None,
        ge=1,
        le=MAX_POSTED_WITHIN_DAYS,
        description="Only consider postings published within this many days.",
    )
    limit: int = Field(
        default=DEFAULT_DISCOVER_LIMIT,
        ge=1,
        le=MAX_DISCOVER_LIMIT,
        description="Maximum postings to pull per provider.",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific search options passed through untouched.",
    )
