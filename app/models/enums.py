"""Canonical enumerations shared by every layer of ApplicantOS.

This module is the vocabulary of the system. The ORM stores these values, the pydantic
schemas serialise them, the REST API returns them, and ``desktop/src/lib/api/types.ts``
mirrors them character-for-character. The string values are therefore **frozen** by
``docs/CONTRACTS.md`` §3: renaming a value is a breaking change to the database, the HTTP
contract, and the desktop client simultaneously.

Design notes:

* Every enum derives from :class:`StrEnum`, which extends :class:`enum.StrEnum`. Members
  compare equal to their plain-string value (``ApplicationStatus.DRAFT == "draft"``),
  serialise as that value through pydantic and :mod:`json`, and render as it under
  ``str()`` and f-strings. The stdlib base is what guarantees that last property: a hand-
  rolled ``(str, Enum)`` mixin renders as ``ClassName.MEMBER`` on Python 3.11+ unless it
  overrides ``__str__`` itself.
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

from enum import StrEnum as _StdlibStrEnum
from typing import Final, Self

__all__ = [
    "APPLICATION_ACTIVE_STATES",
    "APPLICATION_POST_SUBMIT_STATES",
    "APPLICATION_PRE_SUBMIT_STATES",
    "APPLICATION_TERMINAL_STATES",
    "EMAIL_SIGNAL_SOURCES",
    "INDEX_TERMINAL_STATES",
    "MAIL_PROVIDER_TO_SIGNAL_SOURCE",
    "POSTING_TERMINAL_STATES",
    "SESSION_FINISHED_STATES",
    "SIGNAL_KIND_TO_STATUS",
    "STOP_REASONS_COMPLETING",
    "STOP_REASON_SENTENCES",
    "USER_FACING_STATUS",
    "ATSProviderName",
    "ApplicationStatus",
    "CheckpointStatus",
    "DocumentKind",
    "EmploymentType",
    "EntityKind",
    "FactKind",
    "FieldKind",
    "IndexStatus",
    "MailProvider",
    "MemoryKind",
    "PluginKind",
    "PostingStatus",
    "RelationKind",
    "ReviewReason",
    "SessionStatus",
    "SignalKind",
    "SignalSource",
    "SourceKind",
    "StatusSource",
    "StopReason",
    "StrEnum",
    "UserFacingStatus",
    "WorkArrangement",
    "WorkAuthStatus",
]


class StrEnum(_StdlibStrEnum):
    """Base class for every ApplicantOS enum: a string that is also an ``Enum``.

    Members are drop-in replacements for their string value in comparisons, dictionary
    keys, JSON payloads, and SQL binds, while retaining enum identity for exhaustive
    ``match`` statements and IDE completion.

    Derived from :class:`enum.StrEnum` rather than mixing ``str`` and ``Enum`` by hand.
    The two differ on exactly the thing this codebase depends on most: Python 3.11 changed
    ``__str__``/``__format__`` on a hand-rolled mixin to render ``ClassName.MEMBER``, so
    every f-string, log line and URL path built from a member would carry the wrong text
    unless the class overrode them. ``enum.StrEnum`` renders the value natively.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Return a debugger-friendly ``ClassName.MEMBER`` representation."""
        return f"{type(self).__name__}.{self.name}"

    @classmethod
    def _missing_(cls, value: object) -> StrEnum | None:
        """Resolve near-miss strings from external feeds to a real member.

        Accepts differences in case, surrounding whitespace, and hyphen/space instead of
        underscore, and also accepts a member's Python name. Anything else returns
        ``None``, which makes :class:`enum.Enum` raise ``ValueError`` as usual.

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

        Returns:
            Plain strings, never members. ``values_callable`` receives this list straight
            from SQLAlchemy and puts it in DDL, so handing back enum members — which are
            strings, but strings that carry an ``__repr__`` of their own — invites a driver
            to render the wrong text.
        """
        return [str(member.value) for member in cls]

    @classmethod
    def has_value(cls, value: object) -> bool:
        """Return whether *value* corresponds to a member of this enum.

        Args:
            value: A candidate value, typically a string from an external source.

        Returns:
            ``True`` when :meth:`_missing_`-style lookup would succeed.
        """
        # Non-strings can never resolve: exact lookup fails on the value map and
        # :meth:`_missing_` returns ``None`` for anything that is not a string. Rejecting
        # them here says that outright instead of routing an integer through a
        # ``ValueError``.
        if not isinstance(value, str):
            return False
        try:
            cls(value)
        except ValueError:
            return False
        return True

    @classmethod
    def coerce(cls, value: object, default: Self | None = None) -> Self | None:
        """Convert *value* to a member, falling back to *default* instead of raising.

        Typed with :data:`~typing.Self` rather than ``StrEnum`` because the member returned
        is always one of the *called* subclass: ``FactKind.coerce(...)`` yields a
        ``FactKind``, never a bare ``StrEnum``, and callers assign it straight into fields
        annotated with the concrete enum.

        Args:
            value: A candidate value from an untrusted source.
            default: Member returned when *value* cannot be resolved.

        Returns:
            The resolved member, or *default*.
        """
        if not isinstance(value, str):
            # Covers ``None`` and every other non-string: see :meth:`has_value`.
            return default
        try:
            return cls(value)
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
      (``rejected``, ``interview``, ``offer``, ``accepted``, ``ghosted``): the employer has
      the application. Golden rule #1 forbids ever re-submitting from these states.
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
    ACCEPTED = "accepted"
    GHOSTED = "ghosted"

    @classmethod
    def terminal_states(cls) -> frozenset[ApplicationStatus]:
        """Return the statuses that end an application's life.

        ``confirmed``, ``interview`` and ``offer`` are *not* terminal: an outcome is still
        pending and the status will change again. ``accepted`` is — it is the end of the
        funnel, and everything afterwards happens off-platform.

        ``offer`` used to be terminal, which made the product's own "Accepted" category
        unreachable: an offer could be received and never taken.
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

    def user_facing(self) -> UserFacingStatus | None:
        """Return which of the product's four categories this status shows as.

        Returns:
            The category, or ``None`` for a status that belongs in none of them — an
            application that was never sent. See :data:`USER_FACING_STATUS`.
        """
        return USER_FACING_STATUS[self]

    def is_successful_outcome(self) -> bool:
        """Return whether this status represents a positive result for analytics."""
        return self in {
            ApplicationStatus.INTERVIEW,
            ApplicationStatus.OFFER,
            ApplicationStatus.ACCEPTED,
        }


#: Statuses that end an application's life; nothing transitions out of these.
APPLICATION_TERMINAL_STATES: Final[frozenset[ApplicationStatus]] = frozenset(
    {
        ApplicationStatus.FAILED,
        ApplicationStatus.ABANDONED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.ACCEPTED,
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
        ApplicationStatus.ACCEPTED,
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


class UserFacingStatus(StrEnum):
    """The four words the product shows a user about an application.

    :class:`ApplicationStatus` has thirteen members because the automation needs to
    distinguish "generating documents" from "waiting on a human" from "the browser crashed".
    A person tracking their job hunt needs none of that: they want to know which pile this
    application is in. ``docs/CONTRACTS.md`` and every guard keep the thirteen; this is the
    view over them.

    It is a **view, and only a view**. Nothing routes on it, nothing gates on it, and
    :meth:`ApplicationStatus.is_post_submit` remains the sole authority on whether an
    employer actually holds an application. Deriving a policy decision from a display label
    is how a four-way summary turns into a safety bug.
    """

    APPLIED = "applied"
    REJECTED = "rejected"
    OFFERED = "offered"
    ACCEPTED = "accepted"


#: Which of the four each internal status rolls up into, and ``None`` for the states that
#: belong in none of them.
#:
#: ``None`` is not an oversight. ``failed`` and ``abandoned`` describe applications that were
#: never sent — a browser crash, a dismissal — and folding either into "Applied" would tell a
#: user they had applied for a job nobody received. They are excluded from the four-way
#: summary entirely, which is the honest treatment: the summary counts applications, and
#: these are not applications.
#:
#: The pre-submit states *are* included, following the product spec, and the label there
#: means "in your pile" rather than "the employer has it". That is a real gap between the
#: word and the fact, and it is why nothing may route on this mapping.
USER_FACING_STATUS: Final[dict[ApplicationStatus, UserFacingStatus | None]] = {
    ApplicationStatus.DRAFT: UserFacingStatus.APPLIED,
    ApplicationStatus.PREPARING: UserFacingStatus.APPLIED,
    ApplicationStatus.READY: UserFacingStatus.APPLIED,
    ApplicationStatus.SUBMITTING: UserFacingStatus.APPLIED,
    ApplicationStatus.SUBMITTED: UserFacingStatus.APPLIED,
    ApplicationStatus.CONFIRMED: UserFacingStatus.APPLIED,
    ApplicationStatus.NEEDS_REVIEW: UserFacingStatus.APPLIED,
    ApplicationStatus.INTERVIEW: UserFacingStatus.APPLIED,
    ApplicationStatus.GHOSTED: UserFacingStatus.APPLIED,
    ApplicationStatus.REJECTED: UserFacingStatus.REJECTED,
    ApplicationStatus.OFFER: UserFacingStatus.OFFERED,
    ApplicationStatus.ACCEPTED: UserFacingStatus.ACCEPTED,
    ApplicationStatus.FAILED: None,
    ApplicationStatus.ABANDONED: None,
}


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
    """The extension points of ApplicantOS.

    Everything pluggable is registered under one of these kinds and resolved through
    :mod:`app.plugins.registry`; concrete implementations are never imported directly from
    outside their own package (golden rule #5).

    ``tracker`` is the status-sync extension point (``docs/CONTRACTS.md`` §17): a
    :class:`~app.tracking.base.StatusTracker` polls one outcome channel — a mailbox, an ATS
    portal — and yields the signals that close the application loop.

    There is deliberately no ``parser`` kind. Document reading is not an extension point:
    it is one module, :mod:`app.knowledge.analyzers.document`, whose ``read_pdf_text`` /
    ``read_docx_text`` / ``read_html_text`` are dispatched by *content* — the ``%PDF-`` and
    ``PK\\x03\\x04`` magic numbers beat both the filename suffix and the server's
    ``Content-Type`` — which is a decision a registry keyed by ``(kind, name)`` cannot
    express. A source format that needs different *knowledge* out of a file is an
    ``analyzer``; that is what :class:`~app.knowledge.analyzers.resume_parser.ResumeParser`
    is. Removal recorded in ``docs/OPEN_QUESTIONS.md``.
    """

    PROVIDER = "provider"
    MODEL = "model"
    TEMPLATE = "template"
    ANALYZER = "analyzer"
    TRACKER = "tracker"


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


class StopReason(StrEnum):
    """Why a run stopped.

    :class:`SessionStatus` says *how* a run ended — cleanly, cancelled, or broken. It does
    not say why, and "why" is the only part a user can act on. A run that reached its cap and
    a run that ran out of eligible postings are both ``completed``; telling the user only
    that much is the "Done." this vocabulary exists to prevent.

    Every member maps to one sentence in :data:`STOP_REASON_SENTENCES`, which is what the
    desktop app renders. Adding a member without a sentence is a lookup error at render time
    rather than a silent blank, which is why the mapping is exhaustive and tested.
    """

    #: The user pressed Stop.
    USER_STOPPED = "user_stopped"

    #: The run reached its own ``max_applications`` cap.
    LIMIT_REACHED = "limit_reached"

    #: The user is at ``max_applications_per_day`` across every run.
    DAILY_LIMIT_REACHED = "daily_limit_reached"

    #: Nothing left to apply to: every qualified posting is already applied to or exhausted.
    NO_ELIGIBLE_JOBS = "no_eligible_jobs"

    #: ``auto_apply_enabled`` is off, so the run discovered and scored and then had nothing
    #: further it was permitted to do. This is the **shipped default**, which is why it has
    #: a reason of its own: reporting a fresh install's first run as "no eligible postings"
    #: would be false, and reporting it as a failure would be worse.
    SUBMISSION_DISABLED = "submission_disabled"

    #: The run stopped reporting progress and the watchdog closed it.
    STALLED = "stalled"

    #: Something below the run itself broke — no database, no provider, no storage.
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


#: One sentence per :class:`StopReason`, in the second person, naming the number that caused
#: it where there is one. ``{limit}`` is the only placeholder; reasons that need no number
#: simply do not contain it, so :func:`~app.models.session.stop_sentence` can format every
#: member with the same call.
STOP_REASON_SENTENCES: Final[dict[StopReason, str]] = {
    StopReason.USER_STOPPED: "Stopped because you asked it to stop.",
    StopReason.LIMIT_REACHED: "Stopped because the application limit of {limit} was reached.",
    StopReason.DAILY_LIMIT_REACHED: (
        "Stopped because you have reached your daily limit of {limit} applications. "
        "It will resume tomorrow."
    ),
    StopReason.NO_ELIGIBLE_JOBS: (
        "Stopped because no eligible postings are left — everything that scored high enough "
        "has already been applied to."
    ),
    StopReason.SUBMISSION_DISABLED: (
        "Stopped after discovering and scoring, because auto-apply is switched off. "
        "Turn it on in Settings to let a run submit."
    ),
    StopReason.STALLED: (
        "Stopped because the run went quiet for too long and was closed automatically. "
        "Nothing was driving it."
    ),
    StopReason.INFRASTRUCTURE_FAILURE: (
        "Stopped because something underneath the run failed. The event log has the detail."
    ),
}

#: Stop reasons that describe a run which did everything it was asked to do. A run that
#: reached its cap or ran out of postings is ``completed``; one the user cancelled is
#: ``cancelled``; anything else is ``failed``.
STOP_REASONS_COMPLETING: Final[frozenset[StopReason]] = frozenset(
    {
        StopReason.LIMIT_REACHED,
        StopReason.DAILY_LIMIT_REACHED,
        StopReason.NO_ELIGIBLE_JOBS,
        StopReason.SUBMISSION_DISABLED,
    }
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


# ======================================================================================
# Application status sync (docs/CONTRACTS.md §17)
# ======================================================================================


class SignalSource(StrEnum):
    """The channel a status signal was observed on.

    Email is the primary channel by design: every ATS — and LinkedIn itself — notifies by
    email, so one mailbox pipeline covers every provider at once, including ones ApplicantOS
    never integrated. ``ats_portal`` covers the optional direct checks made only where a
    browser session already exists, ``manual`` covers an outcome the user typed in, and
    ``webhook`` is reserved for providers that push.

    The three ``email_*`` members name the *mailbox implementation* that read the message,
    not the employer: :data:`EMAIL_SIGNAL_SOURCES` is the set of them, so no caller has to
    test ``value.startswith("email_")``.
    """

    EMAIL_GMAIL = "email_gmail"
    EMAIL_IMAP = "email_imap"
    EMAIL_OUTLOOK = "email_outlook"
    ATS_PORTAL = "ats_portal"
    MANUAL = "manual"
    WEBHOOK = "webhook"

    def is_email(self) -> bool:
        """Return whether this signal was read out of a mailbox."""
        return self in EMAIL_SIGNAL_SOURCES


#: Signal sources backed by a mailbox, and therefore governed by the read-only,
#: narrow-query, minimal-retention privacy invariants of ``docs/CONTRACTS.md`` §17.8.
EMAIL_SIGNAL_SOURCES: Final[frozenset[SignalSource]] = frozenset(
    {
        SignalSource.EMAIL_GMAIL,
        SignalSource.EMAIL_IMAP,
        SignalSource.EMAIL_OUTLOOK,
    }
)


class SignalKind(StrEnum):
    """What a status signal actually says about an application.

    Produced by ``StatusClassifier``: deterministic phrase rules first, an LLM only when
    those return :attr:`UNKNOWN` or land below the confidence floor. ``unknown`` is a
    first-class outcome, never a default that gets acted on — golden rule #2 means an
    unclassifiable message parks for review instead of moving an application.

    :data:`SIGNAL_KIND_TO_STATUS` is the single authority on what each kind *means*, so the
    classifier and :class:`~app.services.status_sync` cannot disagree.
    """

    APPLICATION_RECEIVED = "application_received"
    VIEWED = "viewed"
    ASSESSMENT_REQUESTED = "assessment_requested"
    INTERVIEW_INVITE = "interview_invite"
    OFFER = "offer"
    OFFER_ACCEPTED = "offer_accepted"
    REJECTION = "rejection"
    WITHDRAWN = "withdrawn"
    GHOSTED_INFERRED = "ghosted_inferred"
    UNKNOWN = "unknown"

    def to_status(self) -> ApplicationStatus | None:
        """Return the application status this kind implies.

        Returns:
            The implied :class:`ApplicationStatus`, or ``None`` when the kind carries
            information but no status change (a "your application was viewed" notice, or a
            message nothing could be made of).
        """
        return SIGNAL_KIND_TO_STATUS[self]

    def changes_status(self) -> bool:
        """Return whether acting on this kind would move the application's status."""
        return SIGNAL_KIND_TO_STATUS[self] is not None


#: What each :class:`SignalKind` means in :class:`ApplicationStatus` terms. Defined once,
#: beside the vocabulary itself, because the classifier that produces a kind and the service
#: that acts on one must never disagree about it.
#:
#: Three entries deserve their reasoning stated:
#:
#: * ``assessment_requested`` maps to ``needs_review``, not to a new outcome. A take-home or
#:   an online assessment is work only the human can do, and the honest response is to
#:   surface it rather than to record progress that has not happened (golden rule #2).
#: * ``viewed`` and ``unknown`` map to ``None``. A view is real information for analytics but
#:   says nothing about the outcome, and an unclassifiable message must never move anything.
#: * ``ghosted_inferred`` maps to ``ghosted`` — the one status this system derives from
#:   silence rather than from a message, gated by ``settings.ghosted_after_days``.
#: * ``offer_accepted`` maps to ``accepted``, the end of the funnel. It is a separate kind
#:   from ``offer`` because an acceptance confirmation restates the offer's own vocabulary
#:   ("your offer", "accept"), so a single kind would classify both as a fresh offer and the
#:   application would never leave ``offer``.
SIGNAL_KIND_TO_STATUS: Final[dict[SignalKind, ApplicationStatus | None]] = {
    SignalKind.APPLICATION_RECEIVED: ApplicationStatus.SUBMITTED,
    SignalKind.VIEWED: None,
    SignalKind.ASSESSMENT_REQUESTED: ApplicationStatus.NEEDS_REVIEW,
    SignalKind.INTERVIEW_INVITE: ApplicationStatus.INTERVIEW,
    SignalKind.OFFER: ApplicationStatus.OFFER,
    SignalKind.OFFER_ACCEPTED: ApplicationStatus.ACCEPTED,
    SignalKind.REJECTION: ApplicationStatus.REJECTED,
    SignalKind.WITHDRAWN: ApplicationStatus.ABANDONED,
    SignalKind.GHOSTED_INFERRED: ApplicationStatus.GHOSTED,
    SignalKind.UNKNOWN: None,
}


class MailProvider(StrEnum):
    """The mailbox implementation behind an :class:`~app.models.tracking.EmailAccount`.

    ``gmail`` and ``outlook`` authenticate through OAuth with read-only scopes
    (``gmail.readonly``, ``Mail.Read``); ``imap`` opens the mailbox with ``readonly=True``.
    Nothing in any of the three sends, deletes, moves, or re-flags a message.
    """

    GMAIL = "gmail"
    OUTLOOK = "outlook"
    IMAP = "imap"

    def signal_source(self) -> SignalSource:
        """Return the :class:`SignalSource` that signals from this mailbox carry."""
        return MAIL_PROVIDER_TO_SIGNAL_SOURCE[self]


#: Mailbox implementation to the source recorded on every signal it produces. Keeps the
#: ``gmail`` → ``email_gmail`` correspondence in one place instead of in each tracker.
MAIL_PROVIDER_TO_SIGNAL_SOURCE: Final[dict[MailProvider, SignalSource]] = {
    MailProvider.GMAIL: SignalSource.EMAIL_GMAIL,
    MailProvider.OUTLOOK: SignalSource.EMAIL_OUTLOOK,
    MailProvider.IMAP: SignalSource.EMAIL_IMAP,
}


class StatusSource(StrEnum):
    """Where an application's *current* status came from.

    Recorded on :attr:`~app.models.application.Application.status_source` so that a status
    the user typed in is never silently overwritten by a lower-confidence inference, and so
    that "how did we learn this?" is answerable per application.

    ``manual`` is the default and the highest authority: the human said so. ``pipeline`` is
    what the submission flow writes. ``email`` and ``portal`` come from status sync.
    ``inferred`` covers a status derived from the absence of news — ghosting.
    """

    MANUAL = "manual"
    EMAIL = "email"
    PORTAL = "portal"
    INFERRED = "inferred"
    PIPELINE = "pipeline"
