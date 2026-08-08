"""Canonical enumerations shared by every layer of ApplicantOS.

This module is the vocabulary of the system. The ORM stores these values, the pydantic
schemas serialise them, the REST API returns them, and ``desktop/src/lib/api/types.ts``
mirrors them character-for-character. The string values are therefore **frozen** by
``docs/CONTRACTS.md`` §3: renaming a value is a breaking change to the database, the HTTP
contract, and the desktop client simultaneously.

Design notes:

* Every enum derives from :class:`StrEnum`, a ``(str, Enum)`` mixin. Members compare equal
  to their plain-string value (``ApplicationStatus.DRAFT == "draft"``), serialise as that
  value through pydantic and :mod:`json`, and render as it under ``str()`` and f-strings —
  the last of which requires the explicit ``__str__`` override below, because a bare
  ``(str, Enum)`` renders as ``ClassName.MEMBER`` on Python 3.11+.
* :meth:`StrEnum._missing_` makes lookup forgiving of the shapes that arrive from ATS feeds
  and CSV exports: different case, hyphens instead of underscores, surrounding whitespace.
  Unknown values still raise ``ValueError`` — nothing is silently coerced to a default.
* Membership sets (terminal states, post-submit states, …) are module-level frozensets
  defined once after each enum and referenced by the helper methods, so no predicate is
  duplicated and every caller agrees on the same partitioning.

This module imports nothing from ``app`` and must stay that way: it sits at the very
bottom of the dependency graph.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

__all__ = [
    "APPLICATION_ACTIVE_STATES",
    "APPLICATION_POST_SUBMIT_STATES",
    "APPLICATION_PRE_SUBMIT_STATES",
    "APPLICATION_TERMINAL_STATES",
    "INDEX_TERMINAL_STATES",
    "POSTING_TERMINAL_STATES",
    "SESSION_FINISHED_STATES",
    "ATSProviderName",
    "ApplicationStatus",
    "CheckpointStatus",
    "DocumentKind",
    "EmploymentType",
    "EntityKind",
    "FactKind",
    "FieldKind",
    "IndexStatus",
    "MemoryKind",
    "PluginKind",
    "PostingStatus",
    "RelationKind",
    "ReviewReason",
    "SessionStatus",
    "SourceKind",
    "StrEnum",
    "WorkArrangement",
    "WorkAuthStatus",
]


class StrEnum(str, Enum):
    """Base class for every ApplicantOS enum: a string that is also an ``Enum``.

    Members are drop-in replacements for their string value in comparisons, dictionary
    keys, JSON payloads, and SQL binds, while retaining enum identity for exhaustive
    ``match`` statements and IDE completion.
    """

    __slots__ = ()

    def __str__(self) -> str:
        """Return the raw string value, not ``ClassName.MEMBER``.

        Python 3.11 changed ``__format__`` on mixin enums to include the class name; this
        override restores value-only rendering so f-strings, log lines, URL paths, and
        template substitutions all produce the wire value.
        """
        return str(self.value)

    def __repr__(self) -> str:
        """Return a debugger-friendly ``ClassName.MEMBER`` representation."""
        return f"{type(self).__name__}.{self.name}"

    @classmethod
    def _missing_(cls, value: object) -> StrEnum | None:
        """Resolve near-miss strings from external feeds to a real member.

        Accepts differences in case, surrounding whitespace, and hyphen/space instead of
        underscore, and also accepts a member's Python name. Anything else returns
        ``None``, which makes :class:`Enum` raise ``ValueError`` as usual.

        Args:
            value: The raw value that failed exact lookup.

        Returns:
            The matching member, or ``None`` when no member corresponds.
        """
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == normalized or member.name.lower() == normalized:
                return member
        return None

    @classmethod
    def values(cls) -> list[str]:
        """Return every member value, in declaration order.

        Useful for building SQLAlchemy ``Enum`` columns with ``values_callable``, for
        OpenAPI documentation, and for validating configuration.
        """
        return [member.value for member in cls]

    @classmethod
    def has_value(cls, value: object) -> bool:
        """Return whether *value* corresponds to a member of this enum.

        Args:
            value: A candidate value, typically a string from an external source.

        Returns:
            ``True`` when :meth:`_missing_`-style lookup would succeed.
        """
        try:
            cls(value)  # type: ignore[call-arg]
        except ValueError:
            return False
        return True

    @classmethod
    def coerce(cls, value: object, default: StrEnum | None = None) -> StrEnum | None:
        """Convert *value* to a member, falling back to *default* instead of raising.

        Args:
            value: A candidate value from an untrusted source.
            default: Member returned when *value* cannot be resolved.

        Returns:
            The resolved member, or *default*.
        """
        if value is None:
            return default
        try:
            return cls(value)  # type: ignore[call-arg,return-value]
        except ValueError:
            return default


# ======================================================================================
# Providers and discovery
# ======================================================================================


class ATSProviderName(StrEnum):
    """Applicant-tracking systems ApplicantOS can talk to.

    Auto-apply posture is binding (``docs/CONTRACTS.md`` §9): ``greenhouse``, ``lever`` and
    ``ashby`` support real submission; ``workday`` and ``linkedin`` do not and route every
    attempt to manual review. ``manual`` covers postings entered by hand.
    """

    LINKEDIN = "linkedin"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    MANUAL = "manual"


class PostingStatus(StrEnum):
    """Lifecycle of a discovered job posting.

    Happy path: ``discovered`` → ``deduped`` → ``scored`` → ``queued`` → ``processing`` →
    ``applied``. A posting that scores below threshold or matches a block rule goes to
    ``skipped``; one that needs a human goes to ``needs_review``; a posting that vanished
    upstream goes to ``expired``.
    """

    DISCOVERED = "discovered"
    DEDUPED = "deduped"
    SCORED = "scored"
    QUEUED = "queued"
    PROCESSING = "processing"
    APPLIED = "applied"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    EXPIRED = "expired"

    @classmethod
    def terminal_states(cls) -> frozenset[PostingStatus]:
        """Return the statuses from which the pipeline will never advance a posting.

        ``needs_review`` is deliberately excluded: it is awaiting a human decision and will
        move again once that decision arrives.
        """
        return POSTING_TERMINAL_STATES

    def is_terminal(self) -> bool:
        """Return whether this posting will receive no further automated processing."""
        return self in POSTING_TERMINAL_STATES

    def is_actionable(self) -> bool:
        """Return whether the pipeline may still pick this posting up."""
        return self not in POSTING_TERMINAL_STATES


#: Statuses from which :class:`PostingStatus` never advances automatically.
POSTING_TERMINAL_STATES: Final[frozenset[PostingStatus]] = frozenset(
    {
        PostingStatus.APPLIED,
        PostingStatus.SKIPPED,
        PostingStatus.FAILED,
        PostingStatus.EXPIRED,
    }
)


# ======================================================================================
# Applications
# ======================================================================================


class ApplicationStatus(StrEnum):
    """Lifecycle of a single application to a single posting.

    The states split into three bands:

    * **Pre-submit** — ``draft``, ``preparing``, ``ready``, ``submitting``: documents are
      being generated and the browser flow has not completed. Nothing has been sent.
    * **Post-submit** — ``submitted``, ``confirmed`` and every outcome that follows
      (``rejected``, ``interview``, ``offer``, ``ghosted``): the employer has the
      application. Golden rule #1 forbids ever re-submitting from these states.
    * **Off-path** — ``needs_review`` (waiting on a human), ``failed`` and ``abandoned``.
    """

    DRAFT = "draft"
    PREPARING = "preparing"
    READY = "ready"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    ABANDONED = "abandoned"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    OFFER = "offer"
    GHOSTED = "ghosted"

    @classmethod
    def terminal_states(cls) -> frozenset[ApplicationStatus]:
        """Return the statuses that end an application's life.

        ``confirmed`` and ``interview`` are *not* terminal: an outcome is still pending and
        the status will change again. ``offer`` is terminal because it is the end of the
        automated funnel — everything afterwards happens off-platform.
        """
        return APPLICATION_TERMINAL_STATES

    @classmethod
    def post_submit_states(cls) -> frozenset[ApplicationStatus]:
        """Return the statuses that imply the employer already received this application."""
        return APPLICATION_POST_SUBMIT_STATES

    @classmethod
    def pre_submit_states(cls) -> frozenset[ApplicationStatus]:
        """Return the statuses in which nothing has been sent to the employer yet."""
        return APPLICATION_PRE_SUBMIT_STATES

    @classmethod
    def active_states(cls) -> frozenset[ApplicationStatus]:
        """Return every non-terminal status."""
        return APPLICATION_ACTIVE_STATES

    def is_post_submit(self) -> bool:
        """Return whether the application has already been sent.

        ``Pipeline.submit`` refuses to run when this is ``True`` — together with the
        ``UNIQUE(user_id, posting_id)`` constraint this enforces "never apply twice".
        """
        return self in APPLICATION_POST_SUBMIT_STATES

    def is_pre_submit(self) -> bool:
        """Return whether the application is still being prepared and nothing was sent."""
        return self in APPLICATION_PRE_SUBMIT_STATES

    def is_terminal(self) -> bool:
        """Return whether the application has reached an end state."""
        return self in APPLICATION_TERMINAL_STATES

    def is_active(self) -> bool:
        """Return whether the application is still in flight or awaiting an outcome."""
        return self not in APPLICATION_TERMINAL_STATES

    def is_successful_outcome(self) -> bool:
        """Return whether this status represents a positive result for analytics."""
        return self in {ApplicationStatus.INTERVIEW, ApplicationStatus.OFFER}


#: Statuses that end an application's life; nothing transitions out of these.
APPLICATION_TERMINAL_STATES: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.FAILED,
        ApplicationStatus.ABANDONED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
        ApplicationStatus.GHOSTED,
    }
)

#: Statuses that mean the employer already holds this application.
APPLICATION_POST_SUBMIT_STATES: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.OFFER,
        ApplicationStatus.GHOSTED,
    }
)

#: Statuses in which nothing has been transmitted to the employer.
APPLICATION_PRE_SUBMIT_STATES: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.DRAFT,
        ApplicationStatus.PREPARING,
        ApplicationStatus.READY,
        ApplicationStatus.SUBMITTING,
    }
)

#: Every status that is not terminal.
APPLICATION_ACTIVE_STATES: Final[frozenset[ApplicationStatus]] = frozenset(
    set(ApplicationStatus) - APPLICATION_TERMINAL_STATES
)


class ReviewReason(StrEnum):
    """Why an application was routed to the human review queue.

    Golden rule #2 — "never guess". Every one of these is a refusal to fabricate: the
    system stops and asks rather than submitting something it is not sure about.
    """

    TOO_MANY_ESSAYS = "too_many_essays"
    UNKNOWN_FIELD = "unknown_field"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA = "captcha"
    MFA = "mfa"
    FILE_UPLOAD_FAILED = "file_upload_failed"
    AMBIGUOUS_ANSWER = "ambiguous_answer"
    LOW_CONFIDENCE = "low_confidence"
    POLICY_BLOCK = "policy_block"
    SUBMIT_NOT_FOUND = "submit_not_found"
    UNSUPPORTED_FLOW = "unsupported_flow"
    VERIFICATION_FAILED = "verification_failed"
    RATE_LIMITED = "rate_limited"
    INSUFFICIENT_KNOWLEDGE = "insufficient_knowledge"
    """The knowledge graph held nothing relevant, so the resume would have been empty.

    Raised by ``Pipeline.prepare`` when tailoring produces zero facts or zero bullets — most
    often a user who has finished onboarding but whose sources have not finished indexing.
    Sending a resume containing only a contact header is worse than not applying, and golden
    rule #7 means the gap cannot be filled with invented content, so this asks the human to
    add a source instead.
    """


# ======================================================================================
# Documents and postings
# ======================================================================================


class DocumentKind(StrEnum):
    """Classification for every file ApplicantOS stores or generates."""

    MASTER_RESUME = "master_resume"
    TAILORED_RESUME = "tailored_resume"
    COVER_LETTER = "cover_letter"
    SCREENSHOT = "screenshot"
    TRANSCRIPT = "transcript"
    PORTFOLIO = "portfolio"
    SOURCE_DOCUMENT = "source_document"
    OTHER = "other"


class EmploymentType(StrEnum):
    """Employment arrangement advertised by a posting."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    INTERNSHIP = "internship"
    CONTRACT = "contract"
    NEW_GRAD = "new_grad"
    UNKNOWN = "unknown"


class WorkArrangement(StrEnum):
    """Where the work is performed.

    ``unknown`` is a first-class value, never a guess: postings frequently omit this and
    inferring it would corrupt both scoring and the user's location filters.
    """

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class FieldKind(StrEnum):
    """Type of an application-form input discovered by the browser autofiller.

    Drives both the answering strategy in :mod:`app.ai.field_answer` and the interaction
    strategy in :mod:`app.browser.autofill`. ``unknown`` always escalates to review rather
    than being filled speculatively.
    """

    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    MULTISELECT = "multiselect"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    FILE = "file"
    DATE = "date"
    NUMBER = "number"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    UNKNOWN = "unknown"


# ======================================================================================
# Plugins
# ======================================================================================


class PluginKind(StrEnum):
    """The five extension points of ApplicantOS.

    Everything pluggable is registered under one of these kinds and resolved through
    :mod:`app.plugins.registry`; concrete implementations are never imported directly from
    outside their own package (golden rule #5).
    """

    PROVIDER = "provider"
    MODEL = "model"
    TEMPLATE = "template"
    PARSER = "parser"
    ANALYZER = "analyzer"


# ======================================================================================
# Knowledge engine
# ======================================================================================


class EntityKind(StrEnum):
    """Node types in the user's knowledge graph."""

    PERSON = "person"
    SKILL = "skill"
    TECHNOLOGY = "technology"
    PROJECT = "project"
    ORGANIZATION = "organization"
    ROLE = "role"
    EDUCATION = "education"
    AWARD = "award"
    CERTIFICATION = "certification"
    COURSE = "course"
    LEADERSHIP = "leadership"
    PUBLICATION = "publication"
    INTEREST = "interest"
    GOAL = "goal"
    LANGUAGE = "language"


class RelationKind(StrEnum):
    """Edge types in the user's knowledge graph."""

    USED_IN = "used_in"
    WORKED_AT = "worked_at"
    BUILT = "built"
    STUDIED_AT = "studied_at"
    EARNED = "earned"
    LED = "led"
    CONTRIBUTED_TO = "contributed_to"
    RELATED_TO = "related_to"
    PUBLISHED = "published"
    MENTORED = "mentored"
    ACHIEVED = "achieved"
    REQUIRES = "requires"


class SourceKind(StrEnum):
    """Provenance of a knowledge document.

    Every fact in the graph traces back to a source of one of these kinds, which is what
    makes golden rule #7 — "nothing is fabricated" — auditable.
    """

    GITHUB_REPO = "github_repo"
    GITHUB_PROFILE = "github_profile"
    PORTFOLIO_PAGE = "portfolio_page"
    PERSONAL_WEBSITE = "personal_website"
    PROJECT_FOLDER = "project_folder"
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    README = "readme"
    DOCUMENTATION = "documentation"
    LINKEDIN_EXPORT = "linkedin_export"
    INTERVIEW_NOTE = "interview_note"
    USER_CORRECTION = "user_correction"
    GENERATED_RESUME = "generated_resume"
    BLOG_POST = "blog_post"
    VIDEO = "video"
    MANUAL_ENTRY = "manual_entry"


class FactKind(StrEnum):
    """Category of an atomic, source-attributed claim about the user.

    ``KnowledgeFact`` replaces the older "master resume bullet" concept: a resume is a
    generated *view* over these facts, never a stored document.
    """

    ACCOMPLISHMENT = "accomplishment"
    RESPONSIBILITY = "responsibility"
    METRIC = "metric"
    SKILL_USAGE = "skill_usage"
    AWARD = "award"
    EDUCATION_ITEM = "education_item"
    LEADERSHIP_ITEM = "leadership_item"
    PUBLICATION_ITEM = "publication_item"


class IndexStatus(StrEnum):
    """Indexing state of a knowledge source.

    ``stale`` marks a source whose upstream fingerprint changed since the last successful
    index; the refresh worker picks these up ahead of time-based reindexing.
    """

    PENDING = "pending"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
    SKIPPED = "skipped"
    STALE = "stale"

    @classmethod
    def terminal_states(cls) -> frozenset[IndexStatus]:
        """Return the statuses in which no indexing work is currently outstanding."""
        return INDEX_TERMINAL_STATES

    def is_terminal(self) -> bool:
        """Return whether indexing has settled for now (successfully or not)."""
        return self in INDEX_TERMINAL_STATES

    def needs_indexing(self) -> bool:
        """Return whether the refresh worker should schedule this source."""
        return self in {IndexStatus.PENDING, IndexStatus.STALE, IndexStatus.FAILED}


#: Index statuses in which no work is currently in flight or queued.
INDEX_TERMINAL_STATES: Final[frozenset[IndexStatus]] = frozenset(
    {IndexStatus.INDEXED, IndexStatus.FAILED, IndexStatus.SKIPPED}
)


class MemoryKind(StrEnum):
    """Category of a :class:`~app.models.knowledge.MemoryEntry`.

    Memories are the system's feedback loop: user corrections, application outcomes,
    free-form feedback, stated preferences, and operator notes. They are retrieved
    alongside facts so that later generations reflect what the user already taught the
    system. Values mirror ``docs/CONTRACTS.md`` §4.
    """

    CORRECTION = "correction"
    OUTCOME = "outcome"
    FEEDBACK = "feedback"
    PREFERENCE = "preference"
    NOTE = "note"


# ======================================================================================
# Runtime
# ======================================================================================


class SessionStatus(StrEnum):
    """State of an automation run session."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def finished_states(cls) -> frozenset[SessionStatus]:
        """Return every status that means the session is no longer executing."""
        return SESSION_FINISHED_STATES

    def is_finished(self) -> bool:
        """Return whether the session has stopped, however it stopped.

        The session watchdog uses this to decide which sessions to force-close after a
        crash: anything still ``running`` past its heartbeat window is stale.
        """
        return self in SESSION_FINISHED_STATES

    def is_running(self) -> bool:
        """Return whether the session is currently executing."""
        return self is SessionStatus.RUNNING


#: Session statuses that mean execution has stopped.
SESSION_FINISHED_STATES: Final[frozenset[SessionStatus]] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
)


class CheckpointStatus(StrEnum):
    """State of a resumable checkpoint step.

    ``compensated`` records a step that succeeded and was then deliberately rolled back,
    which distinguishes a clean undo from an outright failure during crash recovery.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATED = "compensated"

    def is_resumable(self) -> bool:
        """Return whether a crash-recovery pass should re-drive this checkpoint."""
        return self in {CheckpointStatus.PENDING, CheckpointStatus.RUNNING, CheckpointStatus.FAILED}


class WorkAuthStatus(StrEnum):
    """The user's right-to-work status in the hiring country.

    Drives the sponsorship scoring rules and the standard EEO/work-authorisation questions
    on application forms. Never inferred — only ever set from what the user stated during
    onboarding, with ``unknown`` as the honest default.
    """

    CITIZEN = "citizen"
    PERMANENT_RESIDENT = "permanent_resident"
    VISA_HOLDER = "visa_holder"
    NEEDS_SPONSORSHIP = "needs_sponsorship"
    UNKNOWN = "unknown"

    def requires_sponsorship(self) -> bool:
        """Return whether the user needs employer visa sponsorship to take a role."""
        return self is WorkAuthStatus.NEEDS_SPONSORSHIP
