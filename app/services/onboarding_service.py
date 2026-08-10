"""Onboarding — the eight screens that turn an empty install into a working knowledge base.

Onboarding here is *server-driven*. :data:`STEPS` describes every screen — its title, its
inputs, their widget kinds and their options — and ``GET /onboarding/steps`` hands that
description to the desktop app, which renders it blind. A reworded question, a new option, an
extra field: all of them are backend changes rather than a coordinated release of two
codebases. :data:`~app.schemas.onboarding.ONBOARDING_PAYLOAD_MODELS` is the other half of the
same idea: ``POST /onboarding/steps/{step}`` resolves the body's schema by lookup, so adding a
step is one entry rather than another branch in a growing conditional.

Four behaviours in this module are policy rather than plumbing, and each is deliberate.

**Demographics default to declining.** Every EEO field is optional, and submitting the step
leaves anything still unanswered set to
:data:`~app.models.profile.DECLINE_TO_SELF_IDENTIFY`. ``None`` and the sentinel are different
states — "never asked" versus "asked and declined" — and only the sentinel is submittable on
a form. Without this, the first application containing a voluntary self-identification
question would escalate to human review for a question the user already chose not to answer
(golden rule #2 cuts both ways: never guess, but also never stop for something already
settled).

**A resume is a source, never a master document.** The ``master_resume`` step registers the
file as a ``resume`` :class:`~app.models.knowledge.KnowledgeSource` and nothing else. Golden
rule #6 makes every future resume a generated view over the facts parsed out of it; the
original stays as evidence, never as a template.

**LinkedIn profile URLs are collected but never indexed.** The ``links`` step stores the URL,
because application forms ask for it by name, and pointedly does *not* register it as a
knowledge source: LinkedIn's terms prohibit automated scraping, so the only indexable
LinkedIn artefact is a user-supplied export, which arrives through the ``sources`` step as
:attr:`~app.models.enums.SourceKind.LINKEDIN_EXPORT` (golden rule #10).

**Completing onboarding does not index anything.** :meth:`OnboardingService.complete` stamps
``users.onboarded_at`` and *returns the source ids to index*. Indexing is a task — crawling a
GitHub account and embedding a hundred documents is minutes of work on the ``knowledge``
queue, not something to run inside an HTTP request. The caller enqueues
``knowledge.index_source`` for each id.

Errors follow the package convention: :class:`LookupError` for an id that does not exist
(``404``) and :class:`ValueError` — which ``pydantic.ValidationError`` already is — for input
the step refuses (``400``).
"""

from __future__ import annotations

import types
import typing
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from app.config.settings import Settings, get_settings
from app.models.enums import (
    ATSProviderName,
    FieldKind,
    IndexStatus,
    PluginKind,
    SourceKind,
    WorkAuthStatus,
)
from app.models.file import UploadedFile
from app.models.knowledge import KnowledgeSource
from app.models.profile import (
    DECLINE_TO_SELF_IDENTIFY,
    EEO_FIELD_NAMES,
    PROFILE_LINK_KEYS,
    UserProfile,
)
from app.models.user import User, UserPreferences
from app.schemas.onboarding import (
    ONBOARDING_PAYLOAD_MODELS,
    ONBOARDING_STEP_KEYS,
    ContactPayload,
    DemographicsPayload,
    IdentityPayload,
    LinksPayload,
    MasterResumePayload,
    OnboardingField,
    OnboardingStatus,
    OnboardingStep,
    PreferencesPayload,
    SourcesPayload,
    WorkAuthorizationPayload,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ADDRESS_COMPONENTS",
    "INDEXABLE_LINK_KINDS",
    "LOCAL_STORAGE_BACKEND",
    "PASTED_RESUME_DIRNAME",
    "REQUIRED_STEP_KEYS",
    "RESUME_ANALYZER_NAME",
    "STEPS",
    "OnboardingService",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: The postal-address components collected by the ``contact`` step. They are rendered as
#: fields named ``address.<component>``; :func:`_expand_dotted_keys` folds them back into the
#: nested ``address`` mapping that :class:`~app.schemas.onboarding.ContactPayload` declares,
#: so the client can render a flat form without knowing the payload's shape.
ADDRESS_COMPONENTS: Final[tuple[str, ...]] = (
    "line1",
    "line2",
    "city",
    "region",
    "postal_code",
    "country",
)

#: Steps onboarding cannot finish without. Only ``identity`` qualifies: a generated resume
#: must carry a name and there is no honest way to invent one. Everything else is skippable
#: and revisitable from settings.
REQUIRED_STEP_KEYS: Final[frozenset[str]] = frozenset({"identity"})

#: Rejection for a step submitted before the account exists. Only ``identity`` can create
#: one, so every other step has nothing to write onto.
_NO_ACCOUNT_YET_DETAIL: Final[str] = (
    "No account exists yet. Submit the `identity` step first — it is what creates one."
)

#: Rejection when ``identity`` creates the account without an email. Optional once an
#: account exists, because it falls back to the account's address; on the call that is
#: *creating* the account there is nothing to fall back to.
_EMAIL_REQUIRED_DETAIL: Final[str] = (
    "An email address is required to create your account. It is the address employers "
    "reply to, and it is what every application form asks for first."
)

#: Profile link slot to the knowledge source it becomes when ``index_now`` is set.
#: ``linkedin`` is deliberately absent — see the module docstring.
INDEXABLE_LINK_KINDS: Final[dict[str, SourceKind]] = {
    "github": SourceKind.GITHUB_PROFILE,
    "portfolio": SourceKind.PORTFOLIO_PAGE,
    "website": SourceKind.PERSONAL_WEBSITE,
}

#: Name the ``resume`` analyzer is registered under. Resolved through
#: :mod:`app.plugins.registry`, never imported directly (golden rule #5).
RESUME_ANALYZER_NAME: Final[str] = "resume_parser"

#: The only storage backend whose bytes this service can hand to an analyzer as a path.
LOCAL_STORAGE_BACKEND: Final[str] = "local"

#: Directory under ``settings.data_path`` where a pasted-in resume is written before it is
#: registered as a source. Analyzers read files, so the paste-in flow needs one.
PASTED_RESUME_DIRNAME: Final[str] = "resumes"

#: Index statuses that mean a source still has outstanding work, in a deterministic order for
#: the ``IN`` clause in :meth:`OnboardingService.complete`.
_PENDING_INDEX_STATES: Final[tuple[IndexStatus, ...]] = (
    IndexStatus.PENDING,
    IndexStatus.STALE,
    IndexStatus.FAILED,
)

#: Providers offered on the preferences screen, with the ToS posture of ``docs/CONTRACTS.md``
#: §9 written into the label. ``manual`` is omitted: it describes a hand-entered posting, not
#: a feed anything can poll.
_PROVIDER_LABELS: Final[dict[ATSProviderName, str]] = {
    ATSProviderName.GREENHOUSE: "Greenhouse",
    ATSProviderName.LEVER: "Lever",
    ATSProviderName.ASHBY: "Ashby",
    ATSProviderName.WORKDAY: "Workday (discovery only)",
    ATSProviderName.LINKEDIN: "LinkedIn (discovery only)",
}

#: Labels for the work-authorisation options, written out rather than derived so the wording
#: on a legally sensitive question is reviewable in one place.
_WORK_AUTH_LABELS: Final[dict[WorkAuthStatus, str]] = {
    WorkAuthStatus.CITIZEN: "Citizen",
    WorkAuthStatus.PERMANENT_RESIDENT: "Permanent resident",
    WorkAuthStatus.VISA_HOLDER: "Visa holder",
    WorkAuthStatus.NEEDS_SPONSORSHIP: "Will need sponsorship",
    WorkAuthStatus.UNKNOWN: "Prefer not to say",
}

#: Labels for the cover-letter policy options of ``docs/CONTRACTS.md`` §5.
_COVER_LETTER_LABELS: Final[dict[str, str]] = {
    "always": "Always write one",
    "when_required": "Only when the form requires it",
    "when_high_score": "Only for high-scoring roles",
    "never": "Never",
}

#: Help text shared by the four voluntary self-identification fields.
_EEO_HELP: Final[str] = (
    "Voluntary. Leaving this blank records 'decline to self-identify', which is what gets "
    "submitted on application forms — it is never inferred from anything else."
)


def _option(value: str, label: str) -> dict[str, Any]:
    """Build one choice for a select-like :class:`~app.schemas.onboarding.OnboardingField`.

    Args:
        value: The value submitted in the payload.
        label: What the user reads.

    Returns:
        The ``{"value": ..., "label": ...}`` pair the schema declares.
    """
    return {"value": value, "label": label}


def _options(labels: Mapping[Any, str]) -> list[dict[str, Any]]:
    """Build a choice list from a value-to-label mapping, preserving declaration order.

    Args:
        labels: Enum member (or string) to human label.

    Returns:
        The choices, in the mapping's order.
    """
    return [_option(str(getattr(key, "value", key)), label) for key, label in labels.items()]


def _field(
    name: str,
    label: str,
    kind: FieldKind = FieldKind.TEXT,
    *,
    required: bool = False,
    options: Sequence[dict[str, Any]] | None = None,
    placeholder: str | None = None,
    help: str | None = None,
) -> OnboardingField:
    """Build one input descriptor.

    Args:
        name: Payload key this field populates. A dotted name addresses into a nested
            mapping — see :data:`ADDRESS_COMPONENTS`.
        label: Human-facing label.
        kind: Widget type, from the autofiller's shared vocabulary.
        required: Whether the step rejects a payload that omits it.
        options: Choices for select-like kinds.
        placeholder: Ghost text for an empty input.
        help: Explanatory text below the input.

    Returns:
        The descriptor.
    """
    return OnboardingField(
        name=name,
        label=label,
        kind=kind,
        required=required,
        options=list(options or []),
        placeholder=placeholder,
        help=help,
    )


#: **The wizard.** One :class:`~app.schemas.onboarding.OnboardingStep` per screen, in the
#: order of :data:`~app.schemas.onboarding.ONBOARDING_STEP_KEYS`. ``complete`` is ``False``
#: on every definition here because completion is a property of a *user*, not of the wizard;
#: :meth:`OnboardingService.steps` returns copies with it filled in.
STEPS: Final[tuple[OnboardingStep, ...]] = (
    OnboardingStep(
        key="identity",
        title="Who are you?",
        description="The name and contact details printed on every document we generate.",
        required=True,
        fields=[
            _field(
                "full_name",
                "Full name",
                required=True,
                help="Printed at the top of every generated resume.",
            ),
            _field(
                "email",
                "Email",
                FieldKind.EMAIL,
                required=True,
                placeholder="you@example.com",
                help="Where employers reply. This step is also what creates your account.",
            ),
            _field("pronouns", "Pronouns"),
            _field(
                "location",
                "Location",
                placeholder="City, Region, Country",
                help="Written on your resume exactly as you type it.",
            ),
        ],
    ),
    OnboardingStep(
        key="contact",
        title="How can employers reach you?",
        description="Application forms ask for these as separate structured fields.",
        fields=[
            _field(
                "phone",
                "Phone",
                FieldKind.PHONE,
                help="Stored exactly as typed — reformatting breaks provider validation.",
            ),
            _field("location", "Location", placeholder="City, Region, Country"),
            *(
                _field(f"address.{component}", component.replace("_", " ").capitalize())
                for component in ADDRESS_COMPONENTS
            ),
        ],
    ),
    OnboardingStep(
        key="work_authorization",
        title="Right to work",
        description=(
            "Never inferred, and never guessed at. These answers drive hard scoring rules "
            "that the model is not allowed to overrule."
        ),
        fields=[
            _field("citizenship", "Citizenship"),
            _field(
                "work_authorization",
                "Work authorisation",
                FieldKind.SELECT,
                options=_options(_WORK_AUTH_LABELS),
                help="'Prefer not to say' is honest and is the safe default.",
            ),
            _field(
                "requires_sponsorship",
                "I will need visa sponsorship",
                FieldKind.CHECKBOX,
            ),
            _field(
                "clearance",
                "Security clearance",
                placeholder="none, secret, ts_sci, …",
            ),
        ],
    ),
    OnboardingStep(
        key="demographics",
        title="Voluntary self-identification",
        description=(
            "Entirely optional. Anything you leave blank is recorded as "
            "'decline to self-identify', which is what we submit — we never infer these."
        ),
        fields=[
            _field(
                "gender",
                "Gender",
                placeholder=DECLINE_TO_SELF_IDENTIFY,
                help=_EEO_HELP,
            ),
            _field(
                "race_ethnicity",
                "Race / ethnicity",
                placeholder=DECLINE_TO_SELF_IDENTIFY,
                help=_EEO_HELP,
            ),
            _field(
                "disability_status",
                "Disability status",
                placeholder=DECLINE_TO_SELF_IDENTIFY,
                help=_EEO_HELP,
            ),
            _field(
                "veteran_status",
                "Veteran status",
                placeholder=DECLINE_TO_SELF_IDENTIFY,
                help=_EEO_HELP,
            ),
            _field(
                "decline_all",
                "Decline all of these",
                FieldKind.CHECKBOX,
                help="The express path — identical to leaving every field above blank.",
            ),
        ],
    ),
    OnboardingStep(
        key="preferences",
        title="What should the automation do?",
        description=(
            "The policy the pipeline obeys. Automatic submission stays off until you turn "
            "it on here and the application's own kill switch is also off."
        ),
        fields=[
            _field(
                "min_score",
                "Minimum score",
                FieldKind.NUMBER,
                help="0–100. Postings below this are never applied to.",
            ),
            _field(
                "auto_apply",
                "Submit applications automatically",
                FieldKind.CHECKBOX,
                help=(
                    "One half of the kill switch. Submission also requires the server-side "
                    "switch, and both default to off."
                ),
            ),
            _field("max_applications_per_day", "Daily application cap", FieldKind.NUMBER),
            _field(
                "max_essay_questions",
                "Essay questions tolerated",
                FieldKind.NUMBER,
                help="Above this, the application stops and asks you instead of guessing.",
            ),
            _field("min_salary", "Minimum salary", FieldKind.NUMBER),
            _field("preferred_locations", "Preferred locations", FieldKind.MULTISELECT),
            _field("preferred_keywords", "Keywords to favour", FieldKind.MULTISELECT),
            _field("blocked_companies", "Companies to skip", FieldKind.MULTISELECT),
            _field("blocked_industries", "Industries to skip", FieldKind.MULTISELECT),
            _field("exclude_defense", "Skip defence contractors", FieldKind.CHECKBOX),
            _field("remote_only", "Remote roles only", FieldKind.CHECKBOX),
            _field(
                "require_no_sponsorship",
                "Only roles that need no sponsorship",
                FieldKind.CHECKBOX,
            ),
            _field(
                "cover_letter_policy",
                "Cover letters",
                FieldKind.SELECT,
                options=_options(_COVER_LETTER_LABELS),
            ),
            _field("resume_template", "Resume template"),
            _field(
                "providers_enabled",
                "Job boards to poll",
                FieldKind.MULTISELECT,
                options=_options(_PROVIDER_LABELS),
                help=(
                    "Workday and LinkedIn are discovery-only: their terms forbid automated "
                    "submission, so those applications always route to you."
                ),
            ),
        ],
    ),
    OnboardingStep(
        key="links",
        title="Where can we find your work?",
        description=(
            "These go on your resume, and — except LinkedIn — become knowledge sources we index."
        ),
        fields=[
            _field("github", "GitHub", FieldKind.URL),
            _field(
                "linkedin",
                "LinkedIn",
                FieldKind.URL,
                help=(
                    "Stored for application forms only. We never scrape LinkedIn; to index "
                    "it, add your official data export on the next step."
                ),
            ),
            _field("portfolio", "Portfolio", FieldKind.URL),
            _field("website", "Personal website", FieldKind.URL),
            _field(
                "index_now",
                "Index these now",
                FieldKind.CHECKBOX,
                help="Registers the links above as knowledge sources straight away.",
            ),
        ],
    ),
    OnboardingStep(
        key="sources",
        title="Anything else worth reading?",
        description=(
            "Repositories, project folders, a LinkedIn export, interview notes. Each one is "
            "picked with its own control; everything found in them becomes traceable facts."
        ),
        fields=[
            _field(
                "index_now",
                "Index these now",
                FieldKind.CHECKBOX,
                help="Queues an indexing pass as soon as onboarding finishes.",
            ),
        ],
    ),
    OnboardingStep(
        key="master_resume",
        title="Do you already have a resume?",
        description=(
            "We parse it into facts and then generate every future resume from those. The "
            "file you give us stays as evidence — it is never edited and never re-sent."
        ),
        fields=[
            _field("file_id", "Upload a file", FieldKind.FILE),
            _field("uri", "…or point at a path or URL"),
            _field("text", "…or paste the text", FieldKind.TEXTAREA),
            _field(
                "label",
                "Name for this source",
                help="Supply exactly one of the three inputs above.",
            ),
        ],
    ),
)

# A wizard whose steps drifted from the frozen key list would render screens no route can
# accept. Checked at import so the mismatch is a startup failure rather than a 404 later.
if tuple(step.key for step in STEPS) != ONBOARDING_STEP_KEYS:  # pragma: no cover - guard
    raise RuntimeError(
        "STEPS does not match ONBOARDING_STEP_KEYS: "
        f"{[step.key for step in STEPS]} != {list(ONBOARDING_STEP_KEYS)}"
    )


def _nullable_preference_fields() -> frozenset[str]:
    """Return the :class:`~app.models.user.UserPreferences` fields that accept ``None``.

    A partial preferences update sends every untouched field as ``None``. Writing that back
    verbatim would fail validation for the fields whose stored type is not optional
    (``preferred_locations`` is a ``list[str]``, not ``list[str] | None``), so ``None`` has
    to mean "leave alone" for those and "clear it" for the genuinely nullable ones. Deriving
    the set from the model rather than listing it keeps the two from drifting.

    Returns:
        The names of every field whose annotation includes ``None``.
    """
    nullable: set[str] = set()
    for name, info in UserPreferences.model_fields.items():
        annotation = info.annotation
        origin = typing.get_origin(annotation)
        if origin in (typing.Union, types.UnionType) and type(None) in typing.get_args(annotation):
            nullable.add(name)
    return frozenset(nullable)


#: Preference fields for which an explicit ``null`` means "clear it" rather than
#: "leave alone".
_NULLABLE_PREFERENCE_FIELDS: Final[frozenset[str]] = _nullable_preference_fields()


class OnboardingService:
    """Drives the onboarding wizard: what to show, what was answered, and what to index.

    Bound to one session for its lifetime. Mutating methods commit, because the desktop app
    re-reads the status after every step and a step that was not committed would appear not to
    have been taken.

    Args:
        session: The unit of work.
        settings: Application settings, used to resolve uploaded files and to write a
            pasted-in resume. Resolved from :func:`~app.config.settings.get_settings` when
            omitted.

    Usage::

        onboarding = OnboardingService(session)
        await onboarding.submit_step(user.id, "identity", {"full_name": "Ada Lovelace"})
        status = await onboarding.status(user.id)
        source_ids = await onboarding.complete(user.id)   # caller enqueues the indexing
    """

    #: The wizard definition, exposed on the class so a caller does not have to import the
    #: module constant separately.
    STEPS: Final[tuple[OnboardingStep, ...]] = STEPS

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        """Bind the service to a session."""
        self._session = session
        self._settings = settings if settings is not None else get_settings()

    # ----------------------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------------------

    async def steps(self, user_id: uuid.UUID | str | None) -> list[OnboardingStep]:
        """Return every step with this user's completion state filled in.

        Args:
            user_id: The user being onboarded, or ``None`` on a first run where no account
                exists yet — the wizard has to render before it can create one.

        Returns:
            Copies of :data:`STEPS` with ``complete`` computed from the real profile — never
            the shared definitions themselves, which must stay unmutated. With no user, the
            definitions as written: nothing is complete because nothing has been answered.

        Raises:
            LookupError: If *user_id* is malformed or names no user.
        """
        completion = await self._completion_for(user_id)
        return [
            step.model_copy(update={"complete": completion.get(step.key, False)}) for step in STEPS
        ]

    async def status(self, user_id: uuid.UUID | str | None) -> OnboardingStatus:
        """Return where the user is in the wizard.

        Args:
            user_id: The user being onboarded, or ``None`` when the install has no account.

        Returns:
            The steps, the key of the first incomplete one (what the client resumes to after
            a restart), the completed fraction, and whether onboarding has finished. With no
            account: not complete, zero progress, resuming at the first step.

        Raises:
            LookupError: If *user_id* is malformed or names no user.
        """
        user = None if user_id is None else await self._user(user_id)
        completion = {} if user is None else await self._completion(user)
        steps = [
            step.model_copy(update={"complete": completion.get(step.key, False)}) for step in STEPS
        ]
        done = sum(1 for step in steps if step.complete)
        total = len(steps) or 1
        current = next((step.key for step in steps if not step.complete), None)
        return OnboardingStatus(
            complete=user is not None and user.onboarded_at is not None,
            current_step=current,
            steps=steps,
            progress=min(1.0, max(0.0, done / total)),
        )

    # ----------------------------------------------------------------------------------
    # Writing
    # ----------------------------------------------------------------------------------

    async def submit_step(
        self,
        user_id: uuid.UUID | str | None,
        step: str,
        payload: Mapping[str, Any] | BaseModel,
    ) -> OnboardingStatus:
        """Validate one step's answers, write them, and return the updated status.

        The body is validated against the model
        :data:`~app.schemas.onboarding.ONBOARDING_PAYLOAD_MODELS` names for *step*, so an
        unknown key fails at the boundary rather than becoming a silently ignored write.

        **This is where the account comes from.** On a first run *user_id* is ``None`` and the
        ``identity`` step creates the row every other endpoint resolves; see
        :meth:`_create_account`.

        Args:
            user_id: The user being onboarded, or ``None`` on a first run with no account.
            step: One of :data:`~app.schemas.onboarding.ONBOARDING_STEP_KEYS`.
            payload: The step's answers, as a mapping or as an already-built payload model.

        Returns:
            The status after the write, so the client needs one round trip per step.

        Raises:
            LookupError: If *user_id* is malformed or names no user.
            ValueError: If *step* is unknown, if the payload fails validation (as
                ``pydantic.ValidationError``, which is a :class:`ValueError`), or if a step
                other than ``identity`` is submitted before an account exists.
        """
        key = str(step or "").strip()
        model = ONBOARDING_PAYLOAD_MODELS.get(key)
        if model is None:
            raise ValueError(
                f"unknown onboarding step {key!r}. "
                f"Expected one of: {', '.join(ONBOARDING_STEP_KEYS)}"
            )

        data = model.model_validate(
            payload if isinstance(payload, BaseModel) else _expand_dotted_keys(payload)
        )
        user = (
            await self._create_account(data)
            if user_id is None
            else await self._user(user_id)
        )

        # The payload model and the writer are chosen by the *same* key, so ``data`` is always
        # the model the writer declares — but nothing in the type system ties the two lookups
        # together, and each writer takes a different payload class. ``Any`` in the payload
        # position states exactly that: the pairing is guaranteed by
        # :data:`ONBOARDING_PAYLOAD_MODELS`, not by the annotation.
        handlers: dict[str, Callable[[User, Any], Awaitable[None]]] = {
            "identity": self._write_identity,
            "contact": self._write_contact,
            "work_authorization": self._write_work_authorization,
            "demographics": self._write_demographics,
            "preferences": self._write_preferences,
            "links": self._write_links,
            "sources": self._write_sources,
            "master_resume": self._write_master_resume,
        }
        await handlers[key](user, data)

        await self._session.flush()
        await self._session.commit()
        logger.info("onboarding.step_submitted", user_id=str(user.id), step=key)
        return await self.status(user.id)

    async def complete(self, user_id: uuid.UUID | str) -> list[uuid.UUID]:
        """Finish onboarding and report what still needs indexing.

        Stamps ``users.onboarded_at`` — idempotently, so re-completing preserves the original
        instant — and returns the ids of every enabled source with outstanding indexing work.
        **It does not index them.** Crawling a GitHub account and embedding its documents is
        minutes of work belonging on the ``knowledge`` queue; the caller enqueues
        ``knowledge.index_source`` for each id returned.

        Args:
            user_id: The user finishing onboarding.

        Returns:
            Source ids to enqueue, oldest first. Empty when the user registered nothing —
            which is a legitimate outcome, not an error.

        Raises:
            LookupError: If *user_id* is malformed or names no user.
            ValueError: If a required step is still unanswered. Only ``identity`` is
                required, and finishing without a name would produce resumes with nobody's
                name on them.
        """
        user = await self._user(user_id)
        completion = await self._completion(user)
        missing = sorted(key for key in REQUIRED_STEP_KEYS if not completion.get(key, False))
        if missing:
            raise ValueError(
                "onboarding cannot be completed while these steps are unanswered: "
                + ", ".join(missing)
            )

        user.mark_onboarded()
        await self._session.flush()
        await self._session.commit()

        source_ids = list(
            (
                await self._session.execute(
                    select(KnowledgeSource.id)
                    .where(
                        KnowledgeSource.user_id == user.id,
                        KnowledgeSource.enabled.is_(True),
                        KnowledgeSource.index_status.in_(_PENDING_INDEX_STATES),
                    )
                    .order_by(KnowledgeSource.created_at.asc(), KnowledgeSource.id.asc())
                )
            )
            .scalars()
            .all()
        )
        logger.info(
            "onboarding.completed",
            user_id=str(user.id),
            sources_to_index=len(source_ids),
        )
        return source_ids

    async def prefill_from_resume(
        self,
        user_id: uuid.UUID | str,
        file_id: uuid.UUID | str,
    ) -> dict[str, Any]:
        """Read an uploaded resume's contact block and fill the profile's empty fields.

        The resume is parsed by the registered ``resume_parser`` analyzer — resolved through
        :mod:`app.plugins.registry`, never imported directly (golden rule #5) — and only its
        contact block is used here. The rest of what the parse found becomes knowledge when
        the file is registered as a source by the ``master_resume`` step; this method exists
        purely so the user does not retype a name and a phone number the document already
        states.

        **Nothing already answered is overwritten**, and the return value is exactly what was
        filled, so the desktop app can present those values as suggestions the user confirms
        rather than as a silent rewrite of their profile.

        Args:
            user_id: The user being onboarded.
            file_id: An :class:`~app.models.file.UploadedFile` holding the resume.

        Returns:
            ``{field: value}`` for every field this call filled. Empty when the resume states
            nothing new — which is the honest answer, not a failure.

        Raises:
            LookupError: If either identifier is malformed, or the file does not exist.
            ValueError: If the file's bytes are not on the local backend, or the resume
                cannot be read or contains no text layer.
        """
        from app.knowledge.analyzers.base import AnalyzerError, SourceRef
        from app.plugins.loader import load_all
        from app.plugins.registry import registry

        user = await self._user(user_id)
        path = await self._resume_path(file_id)

        load_all()
        analyzer = registry.get(PluginKind.ANALYZER, RESUME_ANALYZER_NAME)
        try:
            result = await analyzer.analyze(  # type: ignore[attr-defined]
                SourceRef(kind=SourceKind.RESUME, uri=path.as_posix())
            )
        except AnalyzerError as exc:
            raise ValueError(f"could not read the resume at {path.name}: {exc}") from exc

        contact = _contact_block(result)
        if not contact:
            raise ValueError(
                f"{path.name} produced no readable text — a scanned PDF with no text layer "
                "cannot be parsed."
            )

        profile = await self._profile(user)
        filled: dict[str, Any] = {}

        name = _clean(contact.get("name"))
        if name and not _clean(user.full_name):
            user.full_name = name
            filled["full_name"] = name

        for attribute, source_key in (("phone", "phone"), ("location", "location")):
            value = _clean(contact.get(source_key))
            if value and not _clean(getattr(profile, attribute)):
                setattr(profile, attribute, value)
                filled[attribute] = value

        links = profile.canonical_links()
        link_changes: dict[str, str] = {}
        for slot in ("github", "linkedin", "portfolio"):
            value = _clean(contact.get(slot))
            if value and not _clean(links.get(slot)):
                link_changes[slot] = value
        if link_changes:
            # Reassigned wholesale: JSON columns are not change tracked.
            profile.links = {**links, **link_changes}
            filled["links"] = dict(link_changes)

        if filled:
            await self._session.flush()
            await self._session.commit()
        logger.info(
            "onboarding.prefilled_from_resume",
            user_id=str(user.id),
            file_id=str(file_id),
            fields=sorted(filled),
        )
        return filled

    # ----------------------------------------------------------------------------------
    # Per-step writers
    # ----------------------------------------------------------------------------------

    async def _write_identity(self, user: User, payload: IdentityPayload) -> None:
        """Write the ``identity`` step.

        Args:
            user: The user being onboarded.
            payload: The validated answers.

        Raises:
            ValueError: If the supplied email already belongs to another account. The insert
                is attempted inside a SAVEPOINT so the collision rolls back alone rather than
                poisoning the whole unit of work.
        """
        user.full_name = payload.full_name
        if payload.email:
            await self._set_email(user, payload.email)

        profile = await self._profile(user)
        if payload.pronouns is not None:
            profile.pronouns = _clean(payload.pronouns)
        if payload.location is not None:
            profile.location = _clean(payload.location)

    async def _set_email(self, user: User, email: str) -> None:
        """Change the account address, refusing a duplicate cleanly.

        Args:
            user: The user being onboarded.
            email: The new address; normalised to lowercase by the model's validator.

        Raises:
            ValueError: If another account already holds that address.
        """
        candidate = email.strip().lower()
        if not candidate or candidate == (user.email or "").lower():
            return
        previous = user.email
        user.email = candidate
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as exc:
            user.email = previous
            self._forget(user)
            raise ValueError(f"{candidate!r} already belongs to another account") from exc

    async def _write_contact(self, user: User, payload: ContactPayload) -> None:
        """Write the ``contact`` step.

        Args:
            user: The user being onboarded.
            payload: The validated answers.
        """
        profile = await self._profile(user)
        if payload.phone is not None:
            profile.phone = _clean(payload.phone)
        if payload.location is not None:
            profile.location = _clean(payload.location)
        if payload.address:
            stored = dict(profile.address or {})
            stored.update(
                {key: value for key, value in payload.address.items() if value not in (None, "")}
            )
            profile.address = stored

    async def _write_work_authorization(
        self, user: User, payload: WorkAuthorizationPayload
    ) -> None:
        """Write the ``work_authorization`` step.

        Args:
            user: The user being onboarded.
            payload: The validated answers.
        """
        profile = await self._profile(user)
        profile.citizenship = _clean(payload.citizenship)
        profile.work_authorization = WorkAuthStatus(payload.work_authorization)
        profile.requires_sponsorship = bool(payload.requires_sponsorship)
        profile.clearance = _clean(payload.clearance)

    async def _write_demographics(self, user: User, payload: DemographicsPayload) -> None:
        """Write the ``demographics`` step, declining whatever is left unanswered.

        Submitting this step means the user was asked. Anything they did not answer therefore
        becomes :data:`~app.models.profile.DECLINE_TO_SELF_IDENTIFY` — a submittable value —
        rather than ``None``, which would make the first form containing a voluntary
        self-identification question escalate to review over a question already settled.
        ``decline_all`` is the same outcome reached explicitly.

        Args:
            user: The user being onboarded.
            payload: The validated answers.
        """
        profile = await self._profile(user)
        if not payload.decline_all:
            for name in EEO_FIELD_NAMES:
                value = _clean(getattr(payload, name, None))
                if value:
                    setattr(profile, name, value)
        profile.decline_eeo_self_identification()

    async def _write_preferences(self, user: User, payload: PreferencesPayload) -> None:
        """Write the ``preferences`` step.

        Args:
            user: The user being onboarded.
            payload: The validated answers.

        Raises:
            ValueError: If the merged document fails validation, raised by
                :meth:`~app.models.user.User.update_prefs`.
        """
        supplied = payload.model_dump(exclude_unset=True)
        changes = {
            name: value
            for name, value in supplied.items()
            if value is not None or name in _NULLABLE_PREFERENCE_FIELDS
        }
        if changes:
            user.update_prefs(**changes)

    async def _write_links(self, user: User, payload: LinksPayload) -> None:
        """Write the ``links`` step and register the indexable ones as sources.

        LinkedIn is stored and never registered: its terms prohibit automated scraping, so
        the only indexable LinkedIn artefact is the user's own export, which arrives through
        the ``sources`` step (golden rule #10).

        Args:
            user: The user being onboarded.
            payload: The validated answers.
        """
        profile = await self._profile(user)
        links = profile.canonical_links()
        supplied = payload.model_dump(exclude_unset=True)

        for slot in PROFILE_LINK_KEYS:
            if slot not in supplied:
                continue
            if slot == "other":
                extra = {
                    str(label): str(url)
                    for label, url in (supplied.get("other") or {}).items()
                    if str(url).strip()
                }
                links["other"] = {**(links.get("other") or {}), **extra}
                continue
            links[slot] = _clean(supplied.get(slot))
        profile.links = links

        if not payload.index_now:
            return
        for slot, kind in INDEXABLE_LINK_KINDS.items():
            uri = _clean(links.get(slot))
            if uri:
                await self._ensure_source(user.id, kind, uri, label=slot.capitalize())

    async def _write_sources(self, user: User, payload: SourcesPayload) -> None:
        """Write the ``sources`` step.

        ``index_now`` is honoured by :meth:`complete`, which returns every source with
        outstanding indexing work for the caller to enqueue. Sources registered with
        ``index_now=False`` are disabled instead, so the periodic refresh worker leaves them
        alone until the user turns them on.

        Args:
            user: The user being onboarded.
            payload: The validated answers.
        """
        for source in payload.sources:
            if source.uri is None:  # a wizard source is always a typed-in location
                continue
            await self._ensure_source(
                user.id,
                SourceKind(source.kind),
                source.uri,
                label=source.label,
                config=source.config,
                enabled=bool(source.enabled and payload.index_now),
                auto_refresh=source.auto_refresh,
            )

    async def _write_master_resume(self, user: User, payload: MasterResumePayload) -> None:
        """Write the ``master_resume`` step by registering the resume as a source.

        Golden rule #6: the document is never stored as a master copy. It becomes a ``resume``
        knowledge source, is parsed into facts, and every future resume is generated from
        those.

        Args:
            user: The user being onboarded.
            payload: The validated answers — exactly one of ``file_id``, ``uri`` or ``text``.

        Raises:
            LookupError: If ``file_id`` names no uploaded file.
            ValueError: If the file's bytes are not on the local backend, or a pasted resume
                cannot be written to disk.
        """
        if payload.file_id is not None:
            uri = (await self._resume_path(payload.file_id)).as_posix()
        elif payload.uri:
            uri = payload.uri.strip()
        else:
            uri = self._write_pasted_resume(user.id, payload.text or "").as_posix()

        await self._ensure_source(
            user.id,
            SourceKind.RESUME,
            uri,
            label=payload.label or "Existing resume",
        )

    # ----------------------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------------------

    async def _completion_for(self, user_id: uuid.UUID | str | None) -> dict[str, bool]:
        """Per-step completion for a user that may not exist yet.

        Args:
            user_id: The user being onboarded, or ``None`` on a first run.

        Returns:
            Step key to completion. Empty when there is no account, which reads correctly
            everywhere: nothing has been answered, so nothing is complete.

        Raises:
            LookupError: If *user_id* is malformed or names no user.
        """
        if user_id is None:
            return {}
        return await self._completion(await self._user(user_id))

    async def _create_account(self, data: BaseModel) -> User:
        """Create the account the rest of the application resolves as the current user.

        Called only from :meth:`submit_step`, and only when the install has no account. That
        makes ``identity`` the one step that can run without one — which is also the only
        step that carries what an account needs, a name and an address to be reached at.

        Args:
            data: The validated payload for the step being submitted.

        Returns:
            The persisted user, flushed so its id is available to the rest of the step.

        Raises:
            ValueError: If *data* is not an identity payload, or carries no email. Both are
                reported against the field so the wizard can mark it rather than showing a
                bare error: the placeholder promises the email "defaults to your account
                address", and on a first run that account is what this call is creating.
        """
        if not isinstance(data, IdentityPayload):
            raise ValueError(_NO_ACCOUNT_YET_DETAIL)
        email = (data.email or "").strip()
        if not email:
            raise ValueError(_EMAIL_REQUIRED_DETAIL)

        # The empty profile is created here rather than left to :meth:`_profile`, and that is
        # not tidiness. ``User.profile`` is ``lazy="selectin"``, which is eager only for a row
        # that was *loaded*; this one was constructed, so the attribute is unloaded and the
        # first read of it emits a lazy SELECT from synchronous attribute access — which under
        # asyncio is a `MissingGreenlet`, not a query. Populating the relationship in memory
        # means there is nothing left to load.
        user = User(email=email, full_name=data.full_name)
        user.profile = UserProfile()
        self._session.add(user)
        await self._session.flush()
        logger.info("onboarding.account_created", user_id=str(user.id))
        return user

    async def _user(self, user_id: uuid.UUID | str) -> User:
        """Load the user, with the eagerly loaded profile attached.

        Args:
            user_id: The user's identifier, as a UUID or its string form.

        Returns:
            The row.

        Raises:
            LookupError: If the identifier is malformed or names no user.
        """
        identifier = _as_uuid(user_id, "user id")
        user = await self._session.scalar(select(User).where(User.id == identifier).limit(1))
        if user is None:
            raise LookupError(f"user {identifier} not found")
        return user

    async def _profile(self, user: User) -> UserProfile:
        """Return the user's profile, creating an empty one on first use.

        A profile with nothing in it is valid: the autofiller reports its empty fields as
        unanswerable and routes to review rather than guessing, which is exactly right for a
        user who has not filled the wizard in yet.

        Args:
            user: The user being onboarded.

        Returns:
            The profile row, attached to this session.
        """
        if user.profile is not None:
            return user.profile
        profile = UserProfile(user_id=user.id)
        self._session.add(profile)
        await self._session.flush()
        user.profile = profile
        return profile

    async def _completion(self, user: User) -> dict[str, bool]:
        """Compute which steps this user has satisfied.

        Completion is read off the real profile state rather than from a "steps I clicked
        through" column, so a user who edits their profile from settings sees the wizard agree
        with reality. Source presence is one grouped aggregate, never a query per step.

        Args:
            user: The user being onboarded.

        Returns:
            Step key to whether it is satisfied.
        """
        profile = user.profile
        kinds = {
            SourceKind(kind): int(count or 0)
            for kind, count in (
                await self._session.execute(
                    select(KnowledgeSource.kind, func.count(KnowledgeSource.id))
                    .where(KnowledgeSource.user_id == user.id)
                    .group_by(KnowledgeSource.kind)
                )
            ).all()
        }
        links = profile.canonical_links() if profile is not None else {}

        return {
            "identity": bool(_clean(user.full_name)),
            "contact": bool(
                profile is not None and (_clean(profile.phone) or (profile.address or {}))
            ),
            "work_authorization": bool(
                profile is not None
                and WorkAuthStatus(profile.work_authorization) is not WorkAuthStatus.UNKNOWN
            ),
            "demographics": bool(
                profile is not None
                and all(getattr(profile, name) is not None for name in EEO_FIELD_NAMES)
            ),
            "preferences": bool(isinstance(user.preferences, dict) and user.preferences),
            "links": any(_clean(links.get(slot)) for slot in PROFILE_LINK_KEYS if slot != "other"),
            "sources": sum(kinds.values()) > 0,
            "master_resume": kinds.get(SourceKind.RESUME, 0) > 0,
        }

    async def _ensure_source(
        self,
        user_id: uuid.UUID,
        kind: SourceKind,
        uri: str,
        *,
        label: str | None = None,
        config: Mapping[str, Any] | None = None,
        enabled: bool = True,
        auto_refresh: bool = True,
    ) -> KnowledgeSource:
        """Register a knowledge source, folding a repeat into the existing row.

        Identity is ``(user_id, kind, uri)``, so pointing the wizard at the same repository
        twice updates one row rather than indexing the same content into the graph twice. The
        insert runs inside a SAVEPOINT for the same reason it does everywhere else in this
        codebase: a collision must roll back alone.

        Args:
            user_id: Owning user.
            kind: Which analyzer handles it.
            uri: URL, path, or provider-specific identifier.
            label: Human name shown in the app.
            config: Analyzer options.
            enabled: Whether passes should read it.
            auto_refresh: Whether the periodic refresh worker may re-index it.

        Returns:
            The stored source, flushed but not committed — :meth:`submit_step` owns the
            commit for the whole step.
        """
        cleaned = str(uri).strip()
        existing = await self._session.scalar(
            select(KnowledgeSource)
            .where(
                KnowledgeSource.user_id == user_id,
                KnowledgeSource.kind == kind,
                KnowledgeSource.uri == cleaned,
            )
            .limit(1)
        )
        if existing is not None:
            existing.label = label or existing.label
            existing.enabled = enabled
            existing.auto_refresh = auto_refresh
            if config:
                existing.config = {**(existing.config or {}), **dict(config)}
            return existing

        source = KnowledgeSource(
            user_id=user_id,
            kind=kind,
            uri=cleaned,
            label=label,
            config=dict(config or {}),
            enabled=enabled,
            auto_refresh=auto_refresh,
            index_status=IndexStatus.PENDING,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(source)
                await self._session.flush()
        except IntegrityError:
            self._forget(source)
            raced = await self._session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.user_id == user_id,
                    KnowledgeSource.kind == kind,
                    KnowledgeSource.uri == cleaned,
                )
                .limit(1)
            )
            if raced is None:
                raise
            logger.info("onboarding.source_raced", user_id=str(user_id), kind=kind.value)
            return raced

        logger.info(
            "onboarding.source_registered",
            user_id=str(user_id),
            source_id=str(source.id),
            kind=kind.value,
        )
        return source

    async def _resume_path(self, file_id: uuid.UUID | str) -> Path:
        """Resolve an uploaded file to a path an analyzer can read.

        Args:
            file_id: The uploaded file's identifier.

        Returns:
            The absolute path of the stored bytes.

        Raises:
            LookupError: If the identifier is malformed or names no file.
            ValueError: If the bytes live on a remote backend. An analyzer reads a filesystem
                path, and silently downloading a bucket object here would hide both the cost
                and the failure mode from the caller.
        """
        identifier = _as_uuid(file_id, "file id")
        record = await self._session.scalar(
            select(UploadedFile).where(UploadedFile.id == identifier).limit(1)
        )
        if record is None:
            raise LookupError(f"uploaded file {identifier} not found")
        if (record.backend or LOCAL_STORAGE_BACKEND) != LOCAL_STORAGE_BACKEND:
            raise ValueError(
                f"uploaded file {identifier} lives on the {record.backend!r} backend; "
                "only local files can be parsed during onboarding"
            )
        return (self._settings.storage_root / record.storage_key).resolve()

    def _write_pasted_resume(self, user_id: uuid.UUID, text: str) -> Path:
        """Persist a pasted-in resume so an analyzer has a file to read.

        Args:
            user_id: Owning user, used only to name the file.
            text: The pasted resume.

        Returns:
            The path written.

        Raises:
            ValueError: If the text is blank, or the file cannot be written.
        """
        body = (text or "").strip()
        if not body:
            raise ValueError("pasted resume text must not be blank")
        directory = self._settings.data_path / PASTED_RESUME_DIRNAME
        path = directory / f"{user_id}-{uuid.uuid4().hex}.txt"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"could not store the pasted resume at {path} ({exc.strerror or exc})"
            ) from exc
        return path

    def _forget(self, instance: object) -> None:
        """Remove a failed insert from the session so a later flush does not retry it.

        Args:
            instance: The object whose write lost a race or violated a constraint.
        """
        try:
            self._session.expunge(instance)
        except InvalidRequestError:
            logger.debug("onboarding.already_detached")


# ======================================================================================
# Helpers
# ======================================================================================


def _expand_dotted_keys(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fold ``"address.city"``-style keys into the nested mapping they address.

    The wizard renders a flat form; :class:`~app.schemas.onboarding.ContactPayload` declares
    ``address`` as one mapping. Expanding here means the client never has to know the payload's
    shape and the schema never has to be flattened to accommodate a form layout.

    Args:
        payload: The raw request body.

    Returns:
        A new mapping with dotted keys nested. A dotted key never overwrites an explicitly
        supplied nested value for the same leaf — an explicit ``address`` object wins.
    """
    expanded: dict[str, Any] = {}
    nested: dict[str, dict[str, Any]] = {}
    for key, value in (payload or {}).items():
        parent, separator, leaf = str(key).partition(".")
        if separator and leaf:
            nested.setdefault(parent, {})[leaf] = value
        else:
            expanded[key] = value

    for parent, values in nested.items():
        supplied = expanded.get(parent)
        merged = dict(values)
        if isinstance(supplied, Mapping):
            merged.update(supplied)
        expanded[parent] = merged
    return expanded


def _contact_block(result: Any) -> dict[str, Any]:
    """Pull the parsed contact block out of an analyzer result.

    Args:
        result: The :class:`~app.knowledge.analyzers.base.AnalysisResult` the resume analyzer
            returned.

    Returns:
        The contact mapping, or an empty mapping when the parse produced no document (a
        scanned PDF with no text layer) or no contact block.
    """
    documents: Iterable[Any] = getattr(result, "documents", ()) or ()
    for document in documents:
        contact = (getattr(document, "metadata", None) or {}).get("contact")
        if isinstance(contact, Mapping):
            return dict(contact)
    return {}


def _clean(value: Any) -> str | None:
    """Trim a value to a non-empty string, mapping blanks and ``None`` alike to ``None``.

    Args:
        value: The candidate.

    Returns:
        The trimmed string, or ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_uuid(value: uuid.UUID | str, label: str) -> uuid.UUID:
    """Coerce an identifier to a :class:`~uuid.UUID`.

    Args:
        value: The identifier, already a UUID or its string form.
        label: What the identifier names, used in the error message.

    Returns:
        The parsed UUID.

    Raises:
        LookupError: If the value is not a well-formed UUID. Malformed and missing are the
            same outcome for a caller — ``404`` — so they raise the same class.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise LookupError(f"{value!r} is not a valid {label}") from exc
