"""Applications — the row that guarantees ApplicantOS never applies to the same job twice.

``UNIQUE(user_id, posting_id)``, named explicitly ``uq_applications_user_posting``, *is*
golden rule #1 of ``docs/CONTRACTS.md`` §18. Everything else — the status guard in
``Pipeline.submit``, the dedupe key on the posting, the checkpoint state machine — is
defence in depth layered on top of it. Those layers can be bypassed by a bug, a race
between two workers, or a retry storm; a unique index cannot. The constraint is named
rather than left to the naming convention precisely because it is load-bearing and must be
addressable by name in migrations, in tests, and in the ``IntegrityError`` handler that
turns a violation into a benign "already applied".

The second guarantee is *auditability*. An application accumulates an append-only trail of
:class:`ApplicationEvent` rows, so "what actually happened during that submission?" is
answerable months later without reading logs. :meth:`Application.add_event` is the only
sanctioned way to write one.

The third guarantee is *referential*. ``applications.posting_id`` is ``ON DELETE RESTRICT``
rather than ``CASCADE``, which is a deliberate departure from the ownership rule the rest of
the schema follows. Cascading would mean that hard-deleting a posting silently erases the
evidence that it was already applied to — and since discovery would rediscover the same job
under a fresh primary key, the unique constraint would no longer recognise it and the system
would apply a second time. RESTRICT converts that scenario from silent data loss into a
loud ``IntegrityError``. Expiring a posting (``cleanup.expire_postings``) sets its status
and is unaffected; anything that genuinely needs to remove an applied-to posting must delete
the applications first, deliberately.

Two column pairs deserve a note, because they create a mutual foreign key between
``applications`` and both ``resume_versions`` and ``cover_letters``:

* ``applications.resume_version_id`` / ``applications.cover_letter_id`` — *which* document
  was actually submitted. Declared ``use_alter=True`` so the schema can be created despite
  the cycle, and the relationships use ``post_update=True`` so a flush can insert both
  rows and then wire them together.
* ``resume_versions.application_id`` / ``cover_letters.application_id`` — every document
  generated *for* this application, including drafts that were not submitted. These are
  ordinary ``ON DELETE SET NULL`` foreign keys, enforced on every backend.
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    ColumnElement,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import GUID, utcnow
from app.models.enums import (
    APPLICATION_POST_SUBMIT_STATES,
    APPLICATION_TERMINAL_STATES,
    ApplicationStatus,
    ReviewReason,
)
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.cover_letter import CoverLetter
    from app.models.file import UploadedFile
    from app.models.posting import JobPosting
    from app.models.resume import ResumeVersion
    from app.models.session import RunSession
    from app.models.user import User

__all__ = [
    "APPLICATION_STATUS_COLUMN",
    "NON_SUBMITTABLE_APPLICATION_STATES",
    "REVIEW_REASON_COLUMN",
    "Application",
    "ApplicationEvent",
]


# --------------------------------------------------------------------------------------
# Column types and sizes
# --------------------------------------------------------------------------------------

# Enum columns persist the lowercase string *value*, never the Python member name.
# SQLAlchemy's default behaviour writes ``Enum.name``, which would break the frozen wire
# vocabulary in ``docs/CONTRACTS.md`` §3; ``values_callable`` forces the value form.
_ENUM_VALUES: Final = operator.methodcaller("values")

#: Storage type for :class:`~app.models.enums.ApplicationStatus`.
APPLICATION_STATUS_COLUMN: Final[SAEnum] = SAEnum(
    ApplicationStatus,
    name="application_status",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Storage type for :class:`~app.models.enums.ReviewReason`.
REVIEW_REASON_COLUMN: Final[SAEnum] = SAEnum(
    ReviewReason,
    name="review_reason",
    native_enum=False,
    values_callable=_ENUM_VALUES,
)

#: Statuses from which a submission must never be attempted: the employer already has the
#: application, or the application is finished. This union is what :attr:`Application.
#: can_submit` tests, and it is derived from the enum module so the two can never drift.
NON_SUBMITTABLE_APPLICATION_STATES: Final[frozenset[ApplicationStatus]] = (
    APPLICATION_POST_SUBMIT_STATES | APPLICATION_TERMINAL_STATES
)

#: Deterministic ordering of :data:`NON_SUBMITTABLE_APPLICATION_STATES` for use in SQL
#: ``IN`` clauses. Frozenset iteration order varies between processes, which would defeat
#: SQLAlchemy's compiled-statement cache.
_NON_SUBMITTABLE_ORDERED: Final[tuple[ApplicationStatus, ...]] = tuple(
    sorted(NON_SUBMITTABLE_APPLICATION_STATES, key=lambda status: status.value)
)

#: Status assumed for an application that has not been flushed yet, where the column
#: default has not been applied by the database.
DEFAULT_APPLICATION_STATUS: Final[ApplicationStatus] = ApplicationStatus.DRAFT

#: Width of an employer-side confirmation reference.
CONFIRMATION_ID_MAX_LENGTH: Final[int] = 255

#: Width of the provider's own application identifier.
EXTERNAL_APPLICATION_ID_MAX_LENGTH: Final[int] = 255

#: Width of an :class:`ApplicationEvent` kind token (``prepared``, ``submitted``, ...).
EVENT_KIND_MAX_LENGTH: Final[int] = 64

#: Explicit constraint names for the two mutually-dependent foreign keys. They must be
#: named for ``use_alter`` to be able to emit ``ALTER TABLE ... ADD CONSTRAINT``. The names
#: match what ``NAMING_CONVENTION`` would otherwise have produced.
_FK_RESUME_VERSION: Final[str] = "fk_applications_resume_version_id_resume_versions"
_FK_COVER_LETTER: Final[str] = "fk_applications_cover_letter_id_cover_letters"


class Application(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """One user's application to one posting.

    The row is created in ``draft``, walks through ``preparing`` → ``ready`` →
    ``submitting`` → ``submitted``/``confirmed``, and may divert to ``needs_review`` at any
    point (golden rule #2: never guess). :attr:`can_submit` is the single predicate every
    submission path consults before touching a browser.

    Note:
        :attr:`answers`, :attr:`review_payload` and :attr:`browser_log` are plain JSON
        columns and are not change tracked. Reassign them rather than mutating in place.
    """

    __tablename__ = "applications"
    __table_args__ = (
        # Golden rule #1. Named explicitly: migrations, tests and the IntegrityError
        # handler all address this constraint by name.
        UniqueConstraint(
            "user_id",
            "posting_id",
            name="uq_applications_user_posting",
        ),
        # The applications list is always scoped to a user and almost always filtered by
        # status ("show me what needs review", "show me what was submitted").
        Index("ix_applications_user_id_status", "user_id", "status"),
    )

    # -- subject ----------------------------------------------------------------------
    posting_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("job_postings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        doc=(
            "The posting applied to. Half of the never-apply-twice unique key, and NOT "
            "NULL because a NULL would make that key vacuous. RESTRICT, not CASCADE: "
            "see the module docstring."
        ),
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "Denormalised employer, so analytics can group by company without joining "
            "through the posting. SET NULL: purging a company must not erase history."
        ),
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("run_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "Automation run that produced this application, when there was one. Optional "
            "reference: pruning old sessions must never delete applications."
        ),
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        APPLICATION_STATUS_COLUMN,
        default=DEFAULT_APPLICATION_STATUS,
        nullable=False,
        index=True,
        doc="Lifecycle state; the submission guard reads this together with can_submit.",
    )

    # -- submitted documents -----------------------------------------------------------
    resume_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey(
            "resume_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name=_FK_RESUME_VERSION,
        ),
        nullable=True,
        index=True,
        doc="The resume version actually submitted (one of several possibly generated).",
    )
    cover_letter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey(
            "cover_letters.id",
            ondelete="SET NULL",
            use_alter=True,
            name=_FK_COVER_LETTER,
        ),
        nullable=True,
        index=True,
        doc="The cover letter actually submitted, when one was required or chosen.",
    )

    # -- submission outcome -------------------------------------------------------------
    submitted_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        index=True,
        doc="When submission completed. Drives the daily rate limit and the funnel chart.",
    )
    duration_seconds: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        doc="Wall-clock seconds the browser flow took; feeds apply_duration_seconds.",
    )
    confirmation_screenshot_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Proof-of-submission screenshot captured by ApplicationVerifier.",
    )
    confirmation_id: Mapped[str | None] = mapped_column(
        String(CONFIRMATION_ID_MAX_LENGTH),
        nullable=True,
        doc="Employer-issued confirmation reference, when the flow surfaces one.",
    )
    confirmation_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Verbatim success message scraped from the confirmation page.",
    )
    external_application_id: Mapped[str | None] = mapped_column(
        String(EXTERNAL_APPLICATION_ID_MAX_LENGTH),
        nullable=True,
        index=True,
        doc="The ATS's own identifier for this application, for later status polling.",
    )

    # -- review, retries and evidence ---------------------------------------------------
    review_reason: Mapped[ReviewReason | None] = mapped_column(
        REVIEW_REASON_COLUMN,
        nullable=True,
        index=True,
        doc="Why this application stopped and asked for a human. NULL unless in review.",
    )
    review_payload: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Everything the reviewer needs: unanswered fields, candidate answers, context.",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        doc="How many submission attempts have been made; bounds the retry policy.",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Message from the most recent failure, retained for the review screen.",
    )
    answers: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Field-by-field answers submitted, keyed by resolved field identifier.",
    )
    ai_reasoning: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Model's account of how it answered the form; advisory, never authoritative.",
    )
    browser_log: Mapped[list[dict[str, Any]]] = mapped_column(
        default=list,
        nullable=False,
        doc="Ordered browser action log captured by ArtifactRecorder during the flow.",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Free-text notes written by the user in the desktop app.",
    )

    # -- relationships -----------------------------------------------------------------
    posting: Mapped[JobPosting] = relationship(
        "JobPosting",
        back_populates="applications",
        lazy="selectin",
    )
    company: Mapped[Company | None] = relationship(
        "Company",
        back_populates="applications",
        lazy="selectin",
    )
    session: Mapped[RunSession | None] = relationship(
        "RunSession",
        back_populates="applications",
    )
    # `post_update=True` breaks the row-level cycle: SQLAlchemy inserts the application,
    # inserts the document, then issues a follow-up UPDATE to wire the pointer.
    # `foreign_keys` disambiguates against the reverse `resume_versions.application_id`.
    resume_version: Mapped[ResumeVersion | None] = relationship(
        "ResumeVersion",
        foreign_keys=[resume_version_id],
        post_update=True,
        lazy="selectin",
    )
    cover_letter: Mapped[CoverLetter | None] = relationship(
        "CoverLetter",
        foreign_keys=[cover_letter_id],
        post_update=True,
        lazy="selectin",
    )
    confirmation_screenshot: Mapped[UploadedFile | None] = relationship(
        "UploadedFile",
        back_populates="confirmation_for_applications",
        foreign_keys=[confirmation_screenshot_id],
        lazy="selectin",
    )
    # Every document generated for this application, submitted or not. No delete-orphan:
    # golden rule #6 keeps generated content beyond the life of the application row.
    resume_versions: Mapped[list[ResumeVersion]] = relationship(
        "ResumeVersion",
        back_populates="application",
        foreign_keys="ResumeVersion.application_id",
        passive_deletes=True,
    )
    cover_letters: Mapped[list[CoverLetter]] = relationship(
        "CoverLetter",
        back_populates="application",
        foreign_keys="CoverLetter.application_id",
        passive_deletes=True,
    )
    # The audit trail: small, append-only, and shown on every detail view.
    events: Mapped[list[ApplicationEvent]] = relationship(
        "ApplicationEvent",
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="ApplicationEvent.at",
    )
    user: Mapped[User] = relationship("User", back_populates="applications")

    # -- behaviour ----------------------------------------------------------------------

    @hybrid_property
    def can_submit(self) -> bool:
        """Whether a submission may still be attempted for this application.

        ``False`` once the employer already holds the application (any post-submit status)
        and ``False`` once the application has reached a terminal status. This is the
        in-process half of golden rule #1; ``uq_applications_user_posting`` is the other.

        An unflushed row whose ``status`` default has not been applied yet is treated as
        ``draft``, which is what the column default will make it.

        Returns:
            ``True`` when the current status permits a submission attempt.
        """
        status = self.status if self.status is not None else DEFAULT_APPLICATION_STATUS
        return status not in NON_SUBMITTABLE_APPLICATION_STATES

    @can_submit.inplace.expression
    @classmethod
    def _can_submit_expression(cls) -> ColumnElement[bool]:
        """SQL form of :attr:`can_submit`, usable directly in a ``where()`` clause."""
        return cls.status.notin_(_NON_SUBMITTABLE_ORDERED)

    @property
    def needs_review(self) -> bool:
        """Whether this application is parked waiting on a human decision."""
        return self.status is ApplicationStatus.NEEDS_REVIEW

    def add_event(
        self,
        kind: str,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ApplicationEvent:
        """Append an entry to this application's audit trail.

        The event is attached to :attr:`events`, so the enclosing unit of work persists it
        on the next flush through the collection's ``save-update`` cascade — the caller
        does not need to ``session.add()`` it. Timestamps are stamped here rather than at
        flush so ordering reflects when the event *happened*, not when it was written.

        Args:
            kind: Short machine-readable event token, for example ``"prepared"``,
                ``"submitted"``, ``"needs_review"`` or ``"retry"``.
            message: Optional human-readable one-liner for the desktop timeline.
            payload: Optional structured context. Copied defensively, so a later mutation
                of the caller's dictionary cannot rewrite history.

        Returns:
            The newly created :class:`ApplicationEvent`, already attached to this
            application.

        Note:
            :attr:`events` is ``lazy="selectin"``, so it is already populated on any
            application loaded from the database and empty on a freshly constructed one.
            Neither case emits SQL here, which keeps this safe to call inside async code.
        """
        event = ApplicationEvent(
            kind=str(kind),
            message=message,
            payload=dict(payload) if payload is not None else {},
            at=utcnow(),
        )
        self.events.append(event)
        return event

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"Application(id={self.id!r}, user_id={self.user_id!r}, "
            f"posting_id={self.posting_id!r}, status={self.status!r})"
        )


class ApplicationEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One append-only entry in an application's audit trail.

    Events are never updated and never deleted independently: they exist so that the exact
    sequence of a submission — what was prepared, what was clicked, what failed, what a
    human decided — survives long after the logs have rotated.

    Note:
        :attr:`payload` is a plain JSON column and is not change tracked. An event is
        immutable by intent, so this is the right trade.
    """

    __tablename__ = "application_events"
    __table_args__ = (
        # The only query shape: one application's trail, in chronological order.
        Index("ix_application_events_application_id_at", "application_id", "at"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        doc="Owning application. Events die with it — they document nothing else.",
    )
    kind: Mapped[str] = mapped_column(
        String(EVENT_KIND_MAX_LENGTH),
        nullable=False,
        doc="Machine-readable event token, e.g. 'prepared', 'submitted', 'needs_review'.",
    )
    message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Human-readable one-liner rendered in the desktop timeline.",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        default=dict,
        nullable=False,
        doc="Structured context for the event: field names, errors, decisions.",
    )
    at: Mapped[datetime] = mapped_column(
        default=utcnow,
        nullable=False,
        doc="When the event occurred, which may precede when the row was written.",
    )

    application: Mapped[Application] = relationship(
        "Application",
        back_populates="events",
    )

    def __repr__(self) -> str:
        """Return a debugger-friendly summary that never triggers a lazy load."""
        return (
            f"ApplicationEvent(id={self.id!r}, "
            f"application_id={self.application_id!r}, kind={self.kind!r}, "
            f"at={self.at!r})"
        )
