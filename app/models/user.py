"""The user identity row and the policy preferences that govern automated applying.

Everything in ApplicantOS hangs off :class:`User`. Knowledge sources, extracted facts,
generated resumes, applications, and run sessions are all user-owned (see
:class:`~app.models.mixins.UserOwnedMixin`), and deleting a user cascades through the whole
graph at the database level.

Two objects live here and they serve very different purposes:

:class:`User`
    The ORM row: identity (``email``, ``full_name``), lifecycle (``is_active``,
    ``onboarded_at``), and a single opaque JSON column, ``preferences``.

:class:`UserPreferences`
    A pydantic v2 model — never a table — that gives the ``preferences`` JSON blob a typed,
    validated shape. It is the *policy* the pipeline obeys: score floor, kill switch, daily
    caps, block lists, sponsorship posture, resume template, cover-letter policy, and which
    ATS providers are enabled. ``docs/CONTRACTS.md`` §5 freezes every field name and default.

Preferences are stored as JSON rather than as columns on purpose. They are read as a whole
by the scorer, the resume engine, and the browser autofiller; they change shape as the
product grows; and they are edited as one document by the desktop settings screen. A JSON
column keeps that a single atomic read/write and keeps schema migrations out of the way of
product iteration.

The bridge between the two is :attr:`User.prefs`, which parses the blob into a validated
:class:`UserPreferences` and serialises it back on assignment. It **never raises on bad
stored data**: a preferences document that fails validation degrades to the defaults and
logs ``user.preferences_invalid``, because a corrupt settings blob must not take the whole
application down — and every default in §5 is the safe position (``auto_apply`` off,
``require_no_sponsorship`` on).

Note on ``__repr__``: every model in this package renders itself from ``self.__dict__``,
which holds only already-loaded attribute values. Reading mapped attributes directly would
emit SQL for an expired instance and raise ``DetachedInstanceError`` for a detached one, and
a ``__repr__`` that can raise is a debugging hazard.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database.base import Base
from app.database.types import JSONType, utcnow
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for runtime, kept for typing
    from app.models.application import Application
    from app.models.knowledge import KnowledgeFact, KnowledgeSource
    from app.models.profile import UserProfile
    from app.models.resume import Resume
    from app.models.session import RunSession

__all__ = [
    "DEFAULT_COVER_LETTER_POLICY",
    "DEFAULT_MAX_APPLICATIONS_PER_DAY",
    "DEFAULT_MAX_ESSAY_QUESTIONS",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_PROVIDERS_ENABLED",
    "DEFAULT_RESUME_TEMPLATE",
    "EMAIL_MAX_LENGTH",
    "FULL_NAME_MAX_LENGTH",
    "User",
    "UserPreferences",
]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Column sizing
# --------------------------------------------------------------------------------------

#: Maximum length of an email address per RFC 5321 (64-octet local part + "@" + 255-octet
#: domain). Sized to the standard rather than to observed data so no valid address is
#: silently truncated.
EMAIL_MAX_LENGTH: Final[int] = 320

#: Generous ceiling for a human name, including multi-part and non-Latin names.
FULL_NAME_MAX_LENGTH: Final[int] = 255


# --------------------------------------------------------------------------------------
# Preference defaults (docs/CONTRACTS.md §5 — frozen)
# --------------------------------------------------------------------------------------

#: Score floor, on the 0-100 normalised scale, below which a posting is not applied to.
DEFAULT_MIN_SCORE: Final[int] = 70

#: Daily submission cap. Mirrors ``Settings.max_applications_per_day``; whichever is lower
#: wins at run time.
DEFAULT_MAX_APPLICATIONS_PER_DAY: Final[int] = 50

#: Number of free-text essay questions tolerated before an application is routed to human
#: review instead of being answered automatically (golden rule #2 — never guess).
DEFAULT_MAX_ESSAY_QUESTIONS: Final[int] = 3

#: Resume template plugin used when a variant does not name its own.
DEFAULT_RESUME_TEMPLATE: Final[str] = "modern"

#: When a cover letter is generated. ``when_required`` writes one only if the posting's
#: form demands it, which is the cheapest correct behaviour.
DEFAULT_COVER_LETTER_POLICY: Final[str] = "when_required"

#: ATS providers enabled out of the box. These are exactly the three that support real
#: automated submission (``docs/CONTRACTS.md`` §9); ``workday`` and ``linkedin`` are omitted
#: because they route every attempt to manual review. Stored as a tuple so the module
#: constant cannot be mutated by a caller; the pydantic default copies it into a list.
DEFAULT_PROVIDERS_ENABLED: Final[tuple[str, ...]] = ("greenhouse", "lever", "ashby")


class UserPreferences(BaseModel):
    """Typed view of ``users.preferences`` — the policy the automation obeys.

    Imported across the entire codebase as ``from app.models.user import UserPreferences``
    (``docs/CONTRACTS.md`` §5). Field names, types and defaults are frozen: the desktop
    settings screen, the scorer, the resume engine and the autofiller all read this shape.

    Every default is the conservative choice. ``auto_apply`` is off and
    ``require_no_sponsorship`` is on, so a freshly created user with an empty preferences
    blob cannot submit anything and cannot be matched to a role that would need a visa they
    have not declared.

    Unknown keys are ignored rather than rejected, so a preferences document written by a
    newer build of the app still loads on an older one. Assignments are validated, so a
    caller cannot poke an invalid value into a live instance and have it persist.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    min_score: int = DEFAULT_MIN_SCORE
    auto_apply: bool = False
    max_applications_per_day: int = DEFAULT_MAX_APPLICATIONS_PER_DAY
    max_essay_questions: int = DEFAULT_MAX_ESSAY_QUESTIONS
    min_salary: int | None = None
    preferred_locations: list[str] = Field(default_factory=list)
    preferred_keywords: list[str] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)
    blocked_industries: list[str] = Field(default_factory=list)
    exclude_defense: bool = False
    skip_startups_under: int | None = None
    remote_only: bool = False
    require_no_sponsorship: bool = True
    resume_variant: str | None = None
    resume_template: str = DEFAULT_RESUME_TEMPLATE
    cover_letter_policy: Literal["always", "when_required", "when_high_score", "never"] = (
        "when_required"
    )
    providers_enabled: list[str] = Field(default_factory=lambda: list(DEFAULT_PROVIDERS_ENABLED))


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person applying for jobs — the root of every ownership chain in the schema.

    ApplicantOS ships as a single-user desktop application, but the schema is multi-tenant
    from the first row: every user-owned table carries ``user_id`` and every query filters
    on it. That costs nothing now and makes a shared deployment a configuration change
    rather than a rewrite.

    Attributes:
        email: Unique login and the address written into generated documents. Normalised to
            lowercase on assignment so ``Ada@Example.com`` cannot become a second account.
        full_name: Display name and the name printed on resumes. Nullable because a user
            record may exist before onboarding has collected one.
        is_active: Soft disable. An inactive user is skipped by every scheduled worker.
        onboarded_at: When onboarding completed, or ``None`` while it is still in progress.
            This is the flag the desktop app reads to decide whether to show the wizard.
        preferences: Raw JSON document backing :attr:`prefs`. Read and written through the
            property, not directly.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )
    full_name: Mapped[str | None] = mapped_column(
        String(FULL_NAME_MAX_LENGTH),
        nullable=True,
        default=None,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    onboarded_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONType(),
        nullable=False,
        default=dict,
    )

    # ----------------------------------------------------------------------------------
    # Relationships
    # ----------------------------------------------------------------------------------

    #: One-to-one profile. Eager-loaded because virtually every consumer of a ``User``
    #: (autofill, resume rendering, scoring) needs it, and because a lazy load inside an
    #: async session raises ``MissingGreenlet``.
    profile: Mapped[UserProfile | None] = relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    #: Configured knowledge inputs — GitHub, websites, project folders, resumes, exports.
    sources: Mapped[list[KnowledgeSource]] = relationship(
        "KnowledgeSource",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    #: Atomic, source-attributed claims extracted from those sources. Every resume bullet
    #: this system produces traces back to one of these rows (golden rule #7).
    facts: Mapped[list[KnowledgeFact]] = relationship(
        "KnowledgeFact",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # The three collections below pair with a one-directional ``user`` many-to-one declared
    # in ``application.py``, ``resume.py`` and ``session.py``. Those modules deliberately
    # omit ``back_populates`` — this module owns the collection names — so the pairing is
    # declared here with ``overlaps=`` instead. That tells SQLAlchemy the two relationships
    # knowingly write the same foreign key, which is exactly what ``overlaps`` is for and
    # is what keeps mapper configuration silent. The cost is that assigning
    # ``application.user = u`` does not also append to ``u.applications`` before a flush;
    # assign through the collection, or expire and reload.

    #: One row per posting ever applied to, unique on ``(user_id, posting_id)`` — the
    #: schema half of golden rule #1, "never apply twice".
    applications: Mapped[list[Application]] = relationship(
        "Application",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="user",
    )

    #: Resume variants. The rendered files are disposable; the versioned ``content_json``
    #: hanging off each variant is what is retained forever (golden rule #6).
    resumes: Mapped[list[Resume]] = relationship(
        "Resume",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="user",
    )

    #: Automation run sessions, each aggregating the counters for one pipeline run.
    sessions: Mapped[list[RunSession]] = relationship(
        "RunSession",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="user",
    )

    # ----------------------------------------------------------------------------------
    # Validation
    # ----------------------------------------------------------------------------------

    @validates("email")
    def _normalize_email(self, _key: str, value: str) -> str:
        """Trim and lowercase an assigned email address.

        Email local parts are technically case-sensitive; no mail provider in practice
        treats them that way, and a case-varying duplicate would defeat the unique
        constraint and silently create a second account with a second knowledge graph.

        Args:
            _key: Attribute name (unused; part of the ``validates`` signature).
            value: The address being assigned.

        Returns:
            The normalised address.

        Raises:
            TypeError: If *value* is not a string.
            ValueError: If *value* is blank after trimming.
        """
        if not isinstance(value, str):
            raise TypeError(f"email must be a string, got {type(value).__name__!r}")
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("email must not be blank")
        return normalized

    # ----------------------------------------------------------------------------------
    # Preferences bridge
    # ----------------------------------------------------------------------------------

    @property
    def prefs(self) -> UserPreferences:
        """Return :attr:`preferences` parsed into a validated :class:`UserPreferences`.

        Missing, empty, non-dictionary, or structurally invalid documents all resolve to
        the contract defaults rather than raising, because settings corruption must never
        prevent the application from starting — and the defaults are the safe posture.
        Invalid documents are logged once per read at WARNING so the condition is visible.

        The returned object is a **copy**. Mutating it does not change the row; assign it
        back through this property, or use :meth:`update_prefs`.

        Returns:
            The user's preferences, or :class:`UserPreferences` defaults.
        """
        raw = self.preferences
        if not isinstance(raw, dict) or not raw:
            return UserPreferences()
        try:
            return UserPreferences.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "user.preferences_invalid",
                user_id=str(self.__dict__.get("id")),
                error_count=exc.error_count(),
                errors=[error.get("loc") for error in exc.errors()],
            )
            return UserPreferences()

    @prefs.setter
    def prefs(self, value: UserPreferences) -> None:
        """Serialise *value* back into the :attr:`preferences` JSON column.

        A whole new dictionary is assigned rather than mutated in place, which is what makes
        SQLAlchemy's attribute instrumentation notice the change and include the column in
        the next flush.

        Args:
            value: The preferences to store.

        Raises:
            TypeError: If *value* is not a :class:`UserPreferences`.
        """
        if not isinstance(value, UserPreferences):
            raise TypeError(f"prefs must be a UserPreferences, got {type(value).__name__!r}")
        self.preferences = value.model_dump(mode="json")

    def update_prefs(self, **changes: Any) -> UserPreferences:
        """Apply a partial update to the user's preferences and persist it.

        The merged document is re-validated in full, so a bad value is rejected before it
        reaches the column instead of poisoning the next read.

        Args:
            **changes: Field names from :class:`UserPreferences` mapped to new values.

        Returns:
            The stored preferences after the update.

        Raises:
            ValueError: If any key is not a field of :class:`UserPreferences`. Silently
                dropping a misspelled key would look like the setting simply did not work.
            pydantic.ValidationError: If a value fails validation.
        """
        unknown = set(changes) - set(UserPreferences.model_fields)
        if unknown:
            raise ValueError("unknown preference field(s): " + ", ".join(sorted(unknown)))
        merged = self.prefs.model_dump(mode="json") | dict(changes)
        validated = UserPreferences.model_validate(merged)
        self.prefs = validated
        return validated

    # ----------------------------------------------------------------------------------
    # Lifecycle helpers
    # ----------------------------------------------------------------------------------

    @property
    def is_onboarded(self) -> bool:
        """Return whether onboarding has been completed for this user."""
        return self.onboarded_at is not None

    def mark_onboarded(self, at: datetime | None = None) -> datetime:
        """Stamp onboarding completion. Idempotent — an existing stamp is preserved.

        Args:
            at: Completion instant. Defaults to now (timezone-aware UTC).

        Returns:
            The effective ``onboarded_at`` value.
        """
        if self.onboarded_at is None:
            self.onboarded_at = at if at is not None else utcnow()
        return self.onboarded_at

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        return (
            f"<User id={loaded.get('id')!s} email={loaded.get('email')!r} "
            f"active={loaded.get('is_active')!r}>"
        )
