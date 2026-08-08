"""The user's application profile — everything a job form ever asks for.

:class:`UserProfile` is the answer sheet. When
:class:`~app.browser.autofill.AutoFiller` walks an application form it resolves each
discovered field against this row first, and only escalates to the LLM (and then to human
review) when the profile has no answer. Keeping the common fields as real, typed columns —
rather than deriving them from the knowledge graph — is what makes the majority of an
application fill deterministically, with no model call and no chance of fabrication.

The row is one-to-one with :class:`~app.models.user.User` and holds five groups of data:

* **Contact and identity** — phone, pronouns, location, postal address, and the canonical
  set of profile links (GitHub, LinkedIn, portfolio, personal website, plus a free-form
  ``other`` bag).
* **Work authorisation** — citizenship, :class:`~app.models.enums.WorkAuthStatus`, and an
  explicit ``requires_sponsorship`` flag. These drive the hard-negative scoring rules; the
  scorer may never flip a sponsorship mismatch into "apply" (``docs/CONTRACTS.md`` §10).
* **EEO / self-identification** — ``gender``, ``race_ethnicity``, ``disability_status`` and
  ``veteran_status``, plus security ``clearance``. These are nullable, **never inferred**,
  and always answerable with :data:`DECLINE_TO_SELF_IDENTIFY`. ``None`` means "the user has
  not told us"; the sentinel means "the user chose not to say". Those are different states
  and the distinction is load-bearing: only the sentinel may be submitted on a form.
* **Preferences that are facts, not policy** — salary band, remote preference, relocation
  willingness and targets, desired roles and industries, exclusions, availability, notice
  period. Scoring *policy* (thresholds, kill switches) lives in
  :class:`~app.models.user.UserPreferences`; this is the factual side of the same coin.
* **Education and overflow** — a JSON list of education entries and an ``extra`` bag for
  provider-specific answers that do not deserve a column.

:meth:`UserProfile.to_dto` flattens the row into a plain dictionary suitable for
constructing the DB-free ``UserProfileDTO`` that ``app/jobs`` and ``app/browser`` consume
(``docs/CONTRACTS.md`` §9), so nothing below the service layer ever holds a live ORM
instance or a session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import JSONType
from app.models.enums import StrEnum, WorkArrangement, WorkAuthStatus
from app.models.mixins import TimestampMixin, UserOwnedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for runtime, kept for typing
    from app.models.user import User

__all__ = [
    "DECLINE_TO_SELF_IDENTIFY",
    "DEFAULT_SALARY_CURRENCY",
    "EEO_FIELD_NAMES",
    "PROFILE_LINK_KEYS",
    "UserProfile",
]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: The literal value every EEO field accepts as a refusal to answer. Written verbatim into
#: application forms, so it must never be reworded. ``None`` (not yet asked) and this
#: sentinel (asked, declined) are deliberately distinct.
DECLINE_TO_SELF_IDENTIFY: Final[str] = "decline_to_self_identify"

#: The four self-identification fields governed by :data:`DECLINE_TO_SELF_IDENTIFY`.
EEO_FIELD_NAMES: Final[tuple[str, ...]] = (
    "gender",
    "race_ethnicity",
    "disability_status",
    "veteran_status",
)

#: Canonical keys of the ``links`` JSON document. ``other`` is a nested mapping of
#: free-form label → URL for anything that does not fit the four named slots.
PROFILE_LINK_KEYS: Final[tuple[str, ...]] = (
    "github",
    "linkedin",
    "portfolio",
    "website",
    "other",
)

#: ISO 4217 code assumed when the user states a salary band without naming a currency.
DEFAULT_SALARY_CURRENCY: Final[str] = "USD"

#: Width of the VARCHAR backing every enum column. Comfortably larger than the longest
#: current value so a new enum member never requires a column alter.
ENUM_COLUMN_LENGTH: Final[int] = 64

#: Length ceiling for short free-text answers (pronouns, clearance level, citizenship).
SHORT_TEXT_MAX_LENGTH: Final[int] = 255

#: Phone numbers are stored exactly as the user typed them — extensions, separators and
#: country prefixes included — because reformatting them breaks provider validation.
PHONE_MAX_LENGTH: Final[int] = 64

#: ISO 4217 currency codes are three characters.
CURRENCY_CODE_LENGTH: Final[int] = 3

#: ``start_date_availability`` is free text ("Immediately", "2026-06-01", "after May
#: graduation") because that is what application forms actually accept.
AVAILABILITY_MAX_LENGTH: Final[int] = 64


def _enum_column_type(enum_class: type[StrEnum], *, name: str) -> SAEnum:
    """Build a portable, value-persisted column type for a :class:`StrEnum`.

    ``native_enum=False`` keeps the column a plain ``VARCHAR`` on every backend rather than
    creating a PostgreSQL ``TYPE``, which would need a bespoke migration every time an enum
    gains a member. ``values_callable`` persists the member *value* (``"citizen"``) instead
    of SQLAlchemy's default of the member *name* (``"CITIZEN"``), which is what keeps the
    database, the JSON API and ``desktop/src/lib/api/types.ts`` in agreement.

    Args:
        enum_class: The enumeration to store.
        name: Type name, used for the (unemitted) constraint name and by Alembic.

    Returns:
        A configured :class:`sqlalchemy.Enum` instance.
    """
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        length=ENUM_COLUMN_LENGTH,
        values_callable=lambda members: [str(member.value) for member in members],
        validate_strings=True,
    )


class UserProfile(UUIDPrimaryKeyMixin, TimestampMixin, UserOwnedMixin, Base):
    """Structured answers to the questions every application form asks.

    Exactly one row per user, enforced by a unique constraint on ``user_id`` rather than by
    reusing the user's primary key, so the profile keeps its own identity and can be
    replaced wholesale during onboarding without disturbing foreign keys.

    Every field is nullable or has a safe default. A profile created the moment a user is
    created is immediately valid and simply has nothing to say yet — which the autofiller
    correctly reports as "unanswerable field" and routes to review rather than guessing.
    """

    __tablename__ = "user_profiles"
    __table_args__ = (UniqueConstraint("user_id"),)

    # ----------------------------------------------------------------------------------
    # Contact and identity
    # ----------------------------------------------------------------------------------

    phone: Mapped[str | None] = mapped_column(String(PHONE_MAX_LENGTH), nullable=True, default=None)
    pronouns: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    #: Human-readable "City, Region, Country" as the user would write it on a resume.
    location: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    #: Postal address components: ``line1``, ``line2``, ``city``, ``region``,
    #: ``postal_code``, ``country``. Kept as JSON because form field granularity varies
    #: wildly between providers.
    address: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    #: Profile URLs keyed by :data:`PROFILE_LINK_KEYS`.
    links: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)

    # ----------------------------------------------------------------------------------
    # Work authorisation
    # ----------------------------------------------------------------------------------

    citizenship: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    work_authorization: Mapped[WorkAuthStatus] = mapped_column(
        _enum_column_type(WorkAuthStatus, name="work_auth_status"),
        nullable=False,
        default=WorkAuthStatus.UNKNOWN,
    )
    #: Stated separately from :attr:`work_authorization` because forms ask it as its own
    #: yes/no question and because a visa holder may or may not need future sponsorship.
    requires_sponsorship: Mapped[bool] = mapped_column(nullable=False, default=False)

    # ----------------------------------------------------------------------------------
    # EEO / self-identification — nullable, never inferred, always declinable
    # ----------------------------------------------------------------------------------

    veteran_status: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    gender: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    race_ethnicity: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    disability_status: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )
    #: Security clearance level, if any ("none", "secret", "ts_sci", …). Free text because
    #: the vocabulary is jurisdiction-specific.
    clearance: Mapped[str | None] = mapped_column(
        String(SHORT_TEXT_MAX_LENGTH), nullable=True, default=None
    )

    # ----------------------------------------------------------------------------------
    # Compensation and location preferences
    # ----------------------------------------------------------------------------------

    salary_min: Mapped[int | None] = mapped_column(nullable=True, default=None)
    salary_max: Mapped[int | None] = mapped_column(nullable=True, default=None)
    salary_currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_SALARY_CURRENCY,
    )
    remote_preference: Mapped[WorkArrangement] = mapped_column(
        _enum_column_type(WorkArrangement, name="work_arrangement"),
        nullable=False,
        default=WorkArrangement.UNKNOWN,
    )
    willing_to_relocate: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: Cities/regions the user would move to, as free-text strings.
    relocation_targets: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)

    # ----------------------------------------------------------------------------------
    # Targeting
    # ----------------------------------------------------------------------------------

    desired_roles: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)
    desired_industries: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)
    excluded_companies: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)
    excluded_industries: Mapped[list[str]] = mapped_column(JSONType(), nullable=False, default=list)

    # ----------------------------------------------------------------------------------
    # Education, availability and overflow
    # ----------------------------------------------------------------------------------

    #: Education entries: ``institution``, ``degree``, ``field_of_study``, ``start``,
    #: ``end``, ``gpa``, ``honors``. Stored here as well as in the knowledge graph because
    #: application forms ask for it as structured data, not prose.
    education: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType(), nullable=False, default=list
    )
    start_date_availability: Mapped[str | None] = mapped_column(
        String(AVAILABILITY_MAX_LENGTH), nullable=True, default=None
    )
    notice_period_weeks: Mapped[int | None] = mapped_column(nullable=True, default=None)
    #: Provider-specific or one-off answers that do not warrant a column. Written by
    #: :class:`~app.browser.autofill.AutoFiller` when a human resolves an unknown field, so
    #: the same question answers itself next time.
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)

    # ----------------------------------------------------------------------------------
    # Relationships
    # ----------------------------------------------------------------------------------

    user: Mapped[User] = relationship("User", back_populates="profile")

    # ----------------------------------------------------------------------------------
    # Behaviour
    # ----------------------------------------------------------------------------------

    def canonical_links(self) -> dict[str, Any]:
        """Return :attr:`links` with every canonical key present.

        Callers building a resume header or filling a form should not have to guard each
        lookup. Missing named slots come back as ``None`` and the ``other`` bag always comes
        back as a dictionary.

        Returns:
            A new dictionary keyed by :data:`PROFILE_LINK_KEYS`.
        """
        stored = self.links if isinstance(self.links, dict) else {}
        resolved: dict[str, Any] = {key: stored.get(key) for key in PROFILE_LINK_KEYS}
        other = resolved.get("other")
        resolved["other"] = other if isinstance(other, dict) else {}
        return resolved

    def eeo_answers(self) -> dict[str, str | None]:
        """Return the four self-identification fields as a mapping.

        Returns:
            ``{field_name: value}`` for every name in :data:`EEO_FIELD_NAMES`. A ``None``
            value means the user has not been asked or has not answered; the string
            :data:`DECLINE_TO_SELF_IDENTIFY` means they explicitly declined.
        """
        return {name: getattr(self, name) for name in EEO_FIELD_NAMES}

    def decline_eeo_self_identification(self) -> None:
        """Set every *unanswered* EEO field to :data:`DECLINE_TO_SELF_IDENTIFY`.

        Fields the user has already answered are left untouched — declining is a default
        for silence, never an overwrite of a stated answer. This is what onboarding calls
        when the user skips the voluntary self-identification step, so that later form fills
        have a submittable value instead of escalating to review.
        """
        defaulted = [name for name in EEO_FIELD_NAMES if getattr(self, name) is None]
        for name in defaulted:
            setattr(self, name, DECLINE_TO_SELF_IDENTIFY)
        if defaulted:
            logger.info(
                "profile.eeo_declined",
                user_id=str(self.__dict__.get("user_id")),
                fields=defaulted,
            )

    def to_dto(self) -> dict[str, Any]:
        """Flatten the profile into a plain, JSON-safe dictionary.

        The result is the constructor payload for the DB-free ``UserProfileDTO`` of
        ``docs/CONTRACTS.md`` §9, so providers and the browser layer never touch a session.
        Enum columns are rendered as their string values and JSON columns are normalised to
        an empty container when NULL, so consumers need no defensive checks.

        ``full_name`` and ``email`` are read from the owning :class:`~app.models.user.User`
        **only when that relationship is already loaded**. Nothing here emits SQL: a lazy
        load inside an async session would raise ``MissingGreenlet``, and a ``to_dto`` that
        can raise is useless in the worker paths that need it. Callers that need identity
        fields must load ``user`` (it is ``selectin``-loaded from the ``User`` side, so
        arriving via ``user.profile`` already satisfies this).

        Returns:
            A dictionary of every profile field plus ``user_id``, ``full_name`` and
            ``email``.
        """
        owner = self.__dict__.get("user")
        return {
            "user_id": self.user_id,
            "full_name": getattr(owner, "full_name", None),
            "email": getattr(owner, "email", None),
            "phone": self.phone,
            "pronouns": self.pronouns,
            "location": self.location,
            "address": dict(self.address) if isinstance(self.address, dict) else {},
            "links": self.canonical_links(),
            "citizenship": self.citizenship,
            "work_authorization": str(self.work_authorization)
            if self.work_authorization is not None
            else None,
            "requires_sponsorship": bool(self.requires_sponsorship),
            "veteran_status": self.veteran_status,
            "gender": self.gender,
            "race_ethnicity": self.race_ethnicity,
            "disability_status": self.disability_status,
            "clearance": self.clearance,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "remote_preference": str(self.remote_preference)
            if self.remote_preference is not None
            else None,
            "willing_to_relocate": bool(self.willing_to_relocate),
            "relocation_targets": list(self.relocation_targets or []),
            "desired_roles": list(self.desired_roles or []),
            "desired_industries": list(self.desired_industries or []),
            "excluded_companies": list(self.excluded_companies or []),
            "excluded_industries": list(self.excluded_industries or []),
            "education": list(self.education or []),
            "start_date_availability": self.start_date_availability,
            "notice_period_weeks": self.notice_period_weeks,
            "extra": dict(self.extra) if isinstance(self.extra, dict) else {},
        }

    def __repr__(self) -> str:
        """Return a debug representation built only from already-loaded values."""
        loaded = self.__dict__
        return (
            f"<UserProfile id={loaded.get('id')!s} user_id={loaded.get('user_id')!s} "
            f"location={loaded.get('location')!r}>"
        )
