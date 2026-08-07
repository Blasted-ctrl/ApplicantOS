"""User, profile, and preference schemas.

Three objects cross the API boundary here, and they are deliberately separate:

* **User** (:class:`UserRead`, :class:`UserCreate`, :class:`UserUpdate`) — identity and
  lifecycle. Small, stable, rarely written.
* **Profile** (:class:`ProfileRead`, :class:`ProfileUpdate`) — the answer sheet the browser
  autofiller consults before it ever asks a model anything. Every column of
  :class:`~app.models.profile.UserProfile` is represented; ``docs/CONTRACTS.md`` §4 freezes
  the list.
* **Preferences** (:class:`PreferencesRead`, :class:`PreferencesUpdate`) — the *policy* the
  automation obeys, stored as JSON on ``users.preferences`` and frozen by §5. The read model
  mirrors :class:`~app.models.user.UserPreferences` field-for-field so the desktop settings
  screen has one shape to bind to.

**Partial-update semantics.** Every ``*Update`` model in this package declares each field as
optional with a ``None`` default. That makes ``None`` ambiguous — "leave alone" or "clear"? —
so the resolution is uniform across ApplicantOS: services apply
``payload.model_dump(exclude_unset=True)``, which distinguishes *absent* from *explicitly
null*. A field the client never sent is never touched; a field the client sent as ``null`` is
written as NULL where the column allows it.

**EEO fields are never inferred.** ``gender``, ``race_ethnicity``, ``disability_status`` and
``veteran_status`` accept :data:`~app.models.profile.DECLINE_TO_SELF_IDENTIFY` and default to
``None``. ``None`` means "not asked / not answered"; the sentinel means "asked, declined".
Only the sentinel may be submitted on a form.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.models.enums import WorkArrangement, WorkAuthStatus
from app.models.user import (
    DEFAULT_COVER_LETTER_POLICY,
    DEFAULT_MAX_APPLICATIONS_PER_DAY,
    DEFAULT_MAX_ESSAY_QUESTIONS,
    DEFAULT_MIN_SCORE,
    DEFAULT_PROVIDERS_ENABLED,
    DEFAULT_RESUME_TEMPLATE,
)
from app.schemas.common import Schema

__all__ = [
    "CoverLetterPolicy",
    "PreferencesRead",
    "PreferencesUpdate",
    "ProfileRead",
    "ProfileUpdate",
    "UserCreate",
    "UserRead",
    "UserUpdate",
]

#: Cover-letter generation policy, frozen by ``docs/CONTRACTS.md`` §5.
CoverLetterPolicy = Literal["always", "when_required", "when_high_score", "never"]

#: Bounds for the 0-100 normalised score threshold a user can set.
_MIN_SCORE_FLOOR: int = 0
_MIN_SCORE_CEILING: int = 100


def _normalize_email(value: str) -> str:
    """Trim and lowercase an email address, rejecting blanks.

    Mirrors :meth:`app.models.user.User._normalize_email` so the API rejects a duplicate
    that differs only in case *before* it reaches the unique constraint, and so the value
    stored is always the value looked up.

    Args:
        value: The address as submitted.

    Returns:
        The normalised address.

    Raises:
        ValueError: If the address is blank after trimming, or has no ``@``.
    """
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("email must not be blank")
    local, separator, domain = normalized.partition("@")
    if not separator or not local or not domain:
        raise ValueError("email must be of the form local@domain")
    return normalized


# ======================================================================================
# Preferences (users.preferences JSON document)
# ======================================================================================


class PreferencesRead(Schema):
    """The policy the automation obeys, as stored on ``users.preferences``.

    Mirrors :class:`app.models.user.UserPreferences` exactly (``docs/CONTRACTS.md`` §5).
    Every default is the conservative position: ``auto_apply`` off and
    ``require_no_sponsorship`` on, so a user who has configured nothing cannot submit
    anything and cannot be matched to a role needing a visa they never declared.
    """

    min_score: int = Field(
        default=DEFAULT_MIN_SCORE,
        ge=_MIN_SCORE_FLOOR,
        le=_MIN_SCORE_CEILING,
        description="Normalised score floor below which a posting is never applied to.",
    )
    auto_apply: bool = Field(
        default=False,
        description="Per-user half of the kill switch; settings.auto_apply_enabled is the other.",
    )
    max_applications_per_day: int = Field(
        default=DEFAULT_MAX_APPLICATIONS_PER_DAY,
        ge=0,
        description="Daily submission cap; the lower of this and the server setting wins.",
    )
    max_essay_questions: int = Field(
        default=DEFAULT_MAX_ESSAY_QUESTIONS,
        ge=0,
        description="Free-text questions tolerated before routing to human review.",
    )
    min_salary: int | None = Field(
        default=None,
        ge=0,
        description="Lowest acceptable advertised salary, in the posting's currency.",
    )
    preferred_locations: list[str] = Field(default_factory=list)
    preferred_keywords: list[str] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)
    blocked_industries: list[str] = Field(default_factory=list)
    exclude_defense: bool = Field(
        default=False,
        description="Skip employers flagged as defence or weapons contractors.",
    )
    skip_startups_under: int | None = Field(
        default=None,
        ge=0,
        description="Skip companies with fewer than this many employees.",
    )
    remote_only: bool = False
    require_no_sponsorship: bool = Field(
        default=True,
        description="Only consider roles that do not require visa sponsorship.",
    )
    resume_variant: str | None = Field(
        default=None,
        description="Name of the Resume variant to tailor from, or None for the default.",
    )
    resume_template: str = Field(
        default=DEFAULT_RESUME_TEMPLATE,
        description="TemplatePlugin used to render generated resumes.",
    )
    cover_letter_policy: CoverLetterPolicy = Field(
        default=DEFAULT_COVER_LETTER_POLICY,  # type: ignore[arg-type]
        description="When a cover letter is generated.",
    )
    providers_enabled: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROVIDERS_ENABLED),
        description="ATS providers this user's discovery runs poll.",
    )


class PreferencesUpdate(Schema):
    """Partial update to :class:`PreferencesRead`.

    Apply with ``model_dump(exclude_unset=True)`` and merge into the stored document; the
    merged result is re-validated in full by
    :meth:`app.models.user.User.update_prefs`, so an invalid combination never reaches the
    column.
    """

    min_score: int | None = Field(default=None, ge=_MIN_SCORE_FLOOR, le=_MIN_SCORE_CEILING)
    auto_apply: bool | None = None
    max_applications_per_day: int | None = Field(default=None, ge=0)
    max_essay_questions: int | None = Field(default=None, ge=0)
    min_salary: int | None = Field(default=None, ge=0)
    preferred_locations: list[str] | None = None
    preferred_keywords: list[str] | None = None
    blocked_companies: list[str] | None = None
    blocked_industries: list[str] | None = None
    exclude_defense: bool | None = None
    skip_startups_under: int | None = Field(default=None, ge=0)
    remote_only: bool | None = None
    require_no_sponsorship: bool | None = None
    resume_variant: str | None = None
    resume_template: str | None = None
    cover_letter_policy: CoverLetterPolicy | None = None
    providers_enabled: list[str] | None = None


# ======================================================================================
# User
# ======================================================================================


class UserRead(Schema):
    """A user as returned by the API.

    ``profile`` mirrors the ORM's eagerly loaded (``selectin``) one-to-one relationship, so
    validating a :class:`~app.models.user.User` directly is safe inside an async session.
    It defaults to ``None`` for users created before onboarding wrote a profile row.
    """

    id: uuid.UUID
    email: str
    full_name: str | None = None
    is_active: bool = True
    onboarded_at: datetime | None = Field(
        default=None,
        description="Completion time of onboarding; None while the wizard is unfinished.",
    )
    preferences: PreferencesRead = Field(
        default_factory=PreferencesRead,
        description="Policy document stored on users.preferences.",
    )
    profile: ProfileRead | None = Field(
        default=None,
        description="The user's application profile, when one exists.",
    )
    created_at: datetime
    updated_at: datetime

    @property
    def is_onboarded(self) -> bool:
        """Whether onboarding has completed for this user."""
        return self.onboarded_at is not None


class UserCreate(Schema):
    """Payload for creating a user.

    Only identity is accepted. Preferences and profile are written through their own
    endpoints so that account creation cannot silently enable automation.
    """

    email: str = Field(description="Unique login address; normalised to lowercase.")
    full_name: str | None = Field(
        default=None,
        description="Display name, also printed on generated resumes.",
    )

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        """Normalise and structurally validate the submitted address."""
        return _normalize_email(value)


class UserUpdate(Schema):
    """Partial update to a user's identity and lifecycle fields.

    ``preferences`` is not updatable here — it has its own endpoint and its own schema, so
    a whole-user PUT cannot flip the automation kill switch as a side effect.
    """

    email: str | None = None
    full_name: str | None = None
    is_active: bool | None = None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str | None) -> str | None:
        """Normalise the address when one is supplied."""
        return None if value is None else _normalize_email(value)


# ======================================================================================
# Profile
# ======================================================================================


class ProfileRead(Schema):
    """Every column of :class:`~app.models.profile.UserProfile`, as returned by the API.

    The EEO fields (``gender``, ``race_ethnicity``, ``disability_status``,
    ``veteran_status``) are nullable and never inferred. ``None`` means the user has not
    answered; :data:`~app.models.profile.DECLINE_TO_SELF_IDENTIFY` means they explicitly
    declined and that value is what gets submitted on a form.
    """

    id: uuid.UUID
    user_id: uuid.UUID

    # -- contact and identity ------------------------------------------------------
    phone: str | None = None
    pronouns: str | None = None
    location: str | None = Field(
        default=None,
        description='Human-readable "City, Region, Country" as written on a resume.',
    )
    address: dict[str, Any] = Field(
        default_factory=dict,
        description="Postal components: line1, line2, city, region, postal_code, country.",
    )
    links: dict[str, Any] = Field(
        default_factory=dict,
        description="github / linkedin / portfolio / website, plus a free-form 'other' map.",
    )

    # -- work authorisation ---------------------------------------------------------
    citizenship: str | None = None
    work_authorization: WorkAuthStatus = WorkAuthStatus.UNKNOWN
    requires_sponsorship: bool = False

    # -- EEO / self-identification ---------------------------------------------------
    veteran_status: str | None = None
    gender: str | None = None
    race_ethnicity: str | None = None
    disability_status: str | None = None
    clearance: str | None = None

    # -- compensation and location ----------------------------------------------------
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    remote_preference: WorkArrangement = WorkArrangement.UNKNOWN
    willing_to_relocate: bool = False
    relocation_targets: list[str] = Field(default_factory=list)

    # -- targeting ---------------------------------------------------------------------
    desired_roles: list[str] = Field(default_factory=list)
    desired_industries: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)

    # -- education, availability, overflow ----------------------------------------------
    education: list[dict[str, Any]] = Field(
        default_factory=list,
        description="institution / degree / field_of_study / start / end / gpa / honors.",
    )
    start_date_availability: str | None = Field(
        default=None,
        description='Free text — "Immediately", "2026-06-01", "after May graduation".',
    )
    notice_period_weeks: int | None = None
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific answers learned from resolved review questions.",
    )

    created_at: datetime
    updated_at: datetime


class ProfileUpdate(Schema):
    """Partial update to a profile — every column of ``user_profiles``, all optional.

    Apply with ``model_dump(exclude_unset=True)``: a field the client did not send is left
    untouched, and a field sent as ``null`` clears the column where it is nullable.
    """

    # -- contact and identity ------------------------------------------------------
    phone: str | None = None
    pronouns: str | None = None
    location: str | None = None
    address: dict[str, Any] | None = None
    links: dict[str, Any] | None = None

    # -- work authorisation ---------------------------------------------------------
    citizenship: str | None = None
    work_authorization: WorkAuthStatus | None = None
    requires_sponsorship: bool | None = None

    # -- EEO / self-identification ---------------------------------------------------
    veteran_status: str | None = None
    gender: str | None = None
    race_ethnicity: str | None = None
    disability_status: str | None = None
    clearance: str | None = None

    # -- compensation and location ----------------------------------------------------
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, min_length=3, max_length=3)
    remote_preference: WorkArrangement | None = None
    willing_to_relocate: bool | None = None
    relocation_targets: list[str] | None = None

    # -- targeting ---------------------------------------------------------------------
    desired_roles: list[str] | None = None
    desired_industries: list[str] | None = None
    excluded_companies: list[str] | None = None
    excluded_industries: list[str] | None = None

    # -- education, availability, overflow ----------------------------------------------
    education: list[dict[str, Any]] | None = None
    start_date_availability: str | None = None
    notice_period_weeks: int | None = Field(default=None, ge=0)
    extra: dict[str, Any] | None = None

    @field_validator("salary_currency")
    @classmethod
    def _uppercase_currency(cls, value: str | None) -> str | None:
        """Normalise an ISO 4217 code to upper case."""
        return None if value is None else value.strip().upper()


# ``UserRead.profile`` forward-references ``ProfileRead``, which is declared afterwards so
# that the profile block reads in the order the onboarding wizard collects it.
UserRead.model_rebuild()
