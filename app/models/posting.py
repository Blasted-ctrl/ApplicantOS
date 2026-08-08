"""Discovered job postings — the entry point of the whole pipeline.

Every posting ApplicantOS ever sees becomes exactly one row in ``job_postings``, no matter
how many times it is re-crawled or how many providers surface it. Two independent unique
constraints make that a database guarantee rather than an application convention:

``UNIQUE(provider, external_id)``
    Re-polling a provider is idempotent. The same Greenhouse job id can only ever produce
    one row, so :meth:`app.services.pipeline.Pipeline.ingest` can upsert instead of
    checking-then-inserting (which races under concurrent pollers).

``UNIQUE(dedupe_key)``
    The *cross-provider* guarantee. The same role syndicated to Greenhouse, LinkedIn and a
    careers page shares one :func:`app.jobs.dedupe.dedupe_key`, so the user is never
    offered — or worse, never applies to — the same job twice through different doors.
    The column is nullable so a posting can exist between ingestion and dedupe; SQL treats
    NULLs as distinct, so any number of not-yet-deduped rows coexist.

``content_hash`` is separate from ``dedupe_key`` and answers a different question: not
"is this the same job?" but "has this job's *text* changed since I last scored it?". A
changed hash invalidates cached scores and cached tailored resumes.

Index design follows the two hot read paths: the discovery feed
(``ORDER BY posted_at DESC WHERE status = ...``, served by the composite
``(status, posted_at)`` index) and single-key lookups during ingestion.
"""

from __future__ import annotations

import operator
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import GUID
from app.models.enums import (
    ATSProviderName,
    EmploymentType,
    PostingStatus,
    WorkArrangement,
)
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.company import Company
    from app.models.cover_letter import CoverLetter
    from app.models.score import JobScore

__all__ = [
    "EMPLOYMENT_TYPE_COLUMN",
    "POSTING_STATUS_COLUMN",
    "PROVIDER_COLUMN",
    "WORK_ARRANGEMENT_COLUMN",
    "JobPosting",
]


# --------------------------------------------------------------------------------------
# Column types and sizes
# --------------------------------------------------------------------------------------

# Enum columns are stored as their lowercase string *value* (``"greenhouse"``), never as
# the Python member name. ``values_callable`` is what forces that: SQLAlchemy's default
# behaviour persists ``Enum.name``, which would write ``"FULL_TIME"`` and break the frozen
# wire vocabulary in ``docs/CONTRACTS.md`` §3. ``native_enum=False`` keeps the column a
# plain VARCHAR so adding a member never needs a PostgreSQL type migration.
_ENUM_VALUES: Final = operator.methodcaller("values")

#: Storage type for :class:`~app.models.enums.ATSProviderName`.
PROVIDER_COLUMN: Final[SAEnum] = SAEnum(
    ATSProviderName,
    name="ats_provider_name",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Storage type for :class:`~app.models.enums.PostingStatus`.
POSTING_STATUS_COLUMN: Final[SAEnum] = SAEnum(
    PostingStatus,
    name="posting_status",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Storage type for :class:`~app.models.enums.WorkArrangement`.
WORK_ARRANGEMENT_COLUMN: Final[SAEnum] = SAEnum(
    WorkArrangement,
    name="work_arrangement",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Storage type for :class:`~app.models.enums.EmploymentType`.
EMPLOYMENT_TYPE_COLUMN: Final[SAEnum] = SAEnum(
    EmploymentType,
    name="employment_type",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Width of a hex-encoded SHA-256 digest, used by ``content_hash``.
SHA256_HEX_LENGTH: Final[int] = 64

#: Width of a cross-provider dedupe key. Generous: the key is a composite of normalised
#: company, normalised title and location, and is not necessarily a fixed-width digest.
DEDUPE_KEY_MAX_LENGTH: Final[int] = 255

#: Width of a provider-native posting identifier.
EXTERNAL_ID_MAX_LENGTH: Final[int] = 255

#: Width of the posting title.
POSTING_TITLE_MAX_LENGTH: Final[int] = 512

#: Width of the free-text location string as advertised.
POSTING_LOCATION_MAX_LENGTH: Final[int] = 255

#: Width of an ISO 4217 currency code.
CURRENCY_CODE_LENGTH: Final[int] = 3


class JobPosting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single job advertisement, deduplicated within and across providers.

    Rows move through :class:`~app.models.enums.PostingStatus` from ``discovered`` to a
    terminal state. Scores (:class:`~app.models.score.JobScore`) hang off a posting per
    user; applications and cover letters hang off it per user as well.

    Note:
        :attr:`raw_json` preserves the provider's untouched payload so a parser bug can be
        fixed and re-run without re-crawling. Like every JSON column here it is not change
        tracked — reassign rather than mutate in place.
    """

    __tablename__ = "job_postings"
    __table_args__ = (
        # Idempotent re-polling: one row per (provider, provider-native id).
        UniqueConstraint(
            "provider",
            "external_id",
            name="uq_job_postings_provider_external_id",
        ),
        # The discovery feed's only query shape: filter by status, order by recency.
        Index("ix_job_postings_status_posted_at", "status", "posted_at"),
    )

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Resolved employer. Nullable so ingestion can persist before company resolution.",
    )

    # -- provider identity ------------------------------------------------------------
    provider: Mapped[ATSProviderName] = mapped_column(
        PROVIDER_COLUMN,
        nullable=False,
        doc="Which ATS this posting was discovered through.",
    )
    external_id: Mapped[str] = mapped_column(
        String(EXTERNAL_ID_MAX_LENGTH),
        nullable=False,
        doc="Provider-native posting identifier; unique together with `provider`.",
    )
    url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Canonical human-facing URL of the posting.",
    )
    apply_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Direct URL of the application form when it differs from `url`.",
    )

    # -- advertised content -----------------------------------------------------------
    title: Mapped[str] = mapped_column(
        String(POSTING_TITLE_MAX_LENGTH),
        nullable=False,
        doc="Role title as advertised.",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Full job description text, already stripped of markup.",
    )
    location: Mapped[str | None] = mapped_column(
        String(POSTING_LOCATION_MAX_LENGTH),
        nullable=True,
        doc="Location string exactly as advertised.",
    )
    work_arrangement: Mapped[WorkArrangement] = mapped_column(
        WORK_ARRANGEMENT_COLUMN,
        default=WorkArrangement.UNKNOWN,
        nullable=False,
        doc="Remote/hybrid/onsite. `unknown` is honest, never a guess.",
    )
    employment_type: Mapped[EmploymentType] = mapped_column(
        EMPLOYMENT_TYPE_COLUMN,
        default=EmploymentType.UNKNOWN,
        nullable=False,
        doc="Full-time/internship/contract/... as advertised.",
    )
    salary_min: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="Bottom of the advertised range, in `salary_currency` units per year.",
    )
    salary_max: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="Top of the advertised range, in `salary_currency` units per year.",
    )
    salary_currency: Mapped[str | None] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=True,
        doc="ISO 4217 code for the advertised range.",
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        index=True,
        doc="When the employer published the posting; drives feed ordering and freshness.",
    )
    closes_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        doc="Advertised closing date, when the provider supplies one.",
    )
    raw_json: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Untouched provider payload, retained so parsers can be re-run offline.",
    )

    # -- deduplication and lifecycle ---------------------------------------------------
    content_hash: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH),
        nullable=True,
        index=True,
        doc="Hash of the normalised advertised text; a change invalidates cached scores.",
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        String(DEDUPE_KEY_MAX_LENGTH),
        nullable=True,
        unique=True,
        index=True,
        doc="Cross-provider identity key. NULL until app.jobs.dedupe has run.",
    )
    status: Mapped[PostingStatus] = mapped_column(
        POSTING_STATUS_COLUMN,
        default=PostingStatus.DISCOVERED,
        nullable=False,
        index=True,
        doc="Pipeline lifecycle state.",
    )

    # -- relationships ---------------------------------------------------------------
    # `company` is eager: no posting is ever rendered without its employer, and selectin
    # on a many-to-one consults the identity map first, so it costs nothing when the
    # company is already loaded.
    company: Mapped[Company | None] = relationship(
        "Company",
        back_populates="postings",
        lazy="selectin",
    )
    # Scores are small (at most one per user) and are shown with the posting everywhere.
    scores: Mapped[list[JobScore]] = relationship(
        "JobScore",
        back_populates="posting",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    # Deliberately NOT a delete cascade. `applications.posting_id` is ON DELETE RESTRICT
    # so that hard-deleting a posting that was already applied to raises instead of
    # erasing the evidence behind golden rule #1. `passive_deletes` keeps SQLAlchemy from
    # trying to NULL the (NOT NULL) foreign key and lets the database refuse cleanly.
    applications: Mapped[list[Application]] = relationship(
        "Application",
        back_populates="posting",
        passive_deletes=True,
    )
    cover_letters: Mapped[list[CoverLetter]] = relationship(
        "CoverLetter",
        back_populates="posting",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def is_open(self) -> bool:
        """Whether the posting is still accepting applications, as far as we know.

        A posting with no ``closes_at`` is treated as open: absence of a closing date is
        the overwhelmingly common case and must not be read as "expired".

        Returns:
            ``False`` only when a closing date exists and is in the past.
        """
        if self.closes_at is None:
            return True
        return self.closes_at > datetime.now(UTC)

    @property
    def salary_range(self) -> tuple[int | None, int | None]:
        """The advertised compensation range as a ``(min, max)`` pair.

        Returns:
            Both ends of the range; either or both may be ``None`` when the posting did
            not advertise them. Never inferred from the other end.
        """
        return (self.salary_min, self.salary_max)

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"JobPosting(id={self.id!r}, provider={self.provider!r}, "
            f"external_id={self.external_id!r}, title={self.title!r}, "
            f"status={self.status!r})"
        )
