"""Onboarding wizard schemas — the step descriptors and the per-step payloads.

Onboarding is *server-driven*. ``GET /onboarding/steps`` returns a list of
:class:`OnboardingStep` descriptors, each carrying its own :class:`OnboardingField`
definitions, and the desktop app renders whatever it is given. That keeps a new question —
or a reworded one, or a newly conditional one — a backend change rather than a coordinated
release of two codebases.

``POST /onboarding/steps/{step}`` accepts the payload model for that step.
:data:`ONBOARDING_PAYLOAD_MODELS` maps the step key to its model, so the route validates the
body against the right shape by lookup instead of a growing ``if/elif`` chain — and an
unknown step key is a ``KeyError`` at the boundary rather than a silently ignored write.

The eight steps, in order (:data:`ONBOARDING_STEP_KEYS`):

``identity``
    Who the user is. The only strictly required step — a resume needs a name.
``contact``
    Phone and postal address, which application forms ask for as structured fields.
``work_authorization``
    Right to work and sponsorship posture. Drives the hard-negative scoring rules, so an
    honest ``unknown`` is preferable to a guess.
``demographics``
    Voluntary EEO self-identification. **Every field is optional**, and skipping the step
    is a first-class outcome that records
    :data:`~app.models.profile.DECLINE_TO_SELF_IDENTIFY` rather than leaving forms
    unanswerable.
``preferences``
    The policy document of ``docs/CONTRACTS.md`` §5.
``links``
    GitHub, LinkedIn, portfolio, personal site — which double as knowledge sources.
``sources``
    Everything else to index: repositories, project folders, exports, notes.
``master_resume``
    An existing resume to parse into facts. Not stored as a master document — golden rule
    #6 makes it a *source*, and every future resume is a generated view of the graph.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import FieldKind, WorkAuthStatus
from app.schemas.common import Schema
from app.schemas.knowledge import SourceCreate
from app.schemas.user import PreferencesUpdate

__all__ = [
    "ContactPayload",
    "DemographicsPayload",
    "IdentityPayload",
    "LinksPayload",
    "MasterResumePayload",
    "ONBOARDING_PAYLOAD_MODELS",
    "ONBOARDING_STEP_KEYS",
    "OnboardingField",
    "OnboardingStatus",
    "OnboardingStep",
    "OnboardingStepKey",
    "PreferencesPayload",
    "SourcesPayload",
    "WorkAuthorizationPayload",
]


#: The onboarding step keys, in presentation order. Frozen as a tuple so a caller cannot
#: reorder the wizard by mutating it.
ONBOARDING_STEP_KEYS: Final[tuple[str, ...]] = (
    "identity",
    "contact",
    "work_authorization",
    "demographics",
    "preferences",
    "links",
    "sources",
    "master_resume",
)

#: Type-level spelling of :data:`ONBOARDING_STEP_KEYS`, so a route can declare
#: ``step: OnboardingStepKey`` and reject an unknown key before any handler runs.
OnboardingStepKey = Literal[
    "identity",
    "contact",
    "work_authorization",
    "demographics",
    "preferences",
    "links",
    "sources",
    "master_resume",
]

#: Progress is reported as a fraction in [0, 1] rather than a percentage, so the client
#: chooses its own formatting.
_PROGRESS_FLOOR: Final[float] = 0.0
_PROGRESS_CEILING: Final[float] = 1.0


# ======================================================================================
# Step descriptors (server-driven form rendering)
# ======================================================================================


class OnboardingField(Schema):
    """One input in a step, described well enough for the client to render it blind.

    Attributes:
        name: Key this field's value appears under in the step payload.
        label: Human-facing label.
        kind: Widget type, shared with the browser autofiller's
            :class:`~app.models.enums.FieldKind` vocabulary so one enum drives both.
        required: Whether the step rejects a payload that omits this field.
        options: Allowed values for ``select``/``multiselect``/``radio`` fields, as
            ``{"value": ..., "label": ...}`` pairs. Empty for free-text kinds.
        placeholder: Ghost text shown in an empty input.
        help: Longer explanation rendered beneath the input.
    """

    name: str = Field(description="Payload key this field populates.")
    label: str = Field(description="Human-facing label.")
    kind: FieldKind = Field(
        default=FieldKind.TEXT,
        description="Widget type; shares the autofiller's FieldKind vocabulary.",
    )
    required: bool = Field(default=False, description="Whether the step demands a value.")
    options: list[dict[str, Any]] = Field(
        default_factory=list,
        description='Choices for select-like kinds, as {"value": ..., "label": ...}.',
    )
    placeholder: str | None = Field(default=None, description="Ghost text for empty input.")
    help: str | None = Field(default=None, description="Explanatory text below the input.")


class OnboardingStep(Schema):
    """One screen of the wizard.

    Attributes:
        key: Stable identifier, one of :data:`ONBOARDING_STEP_KEYS`. Also the path segment
            of ``POST /onboarding/steps/{step}``.
        title: Heading shown to the user.
        description: Sub-heading explaining why the step is being asked.
        fields: The inputs to render, in order.
        required: Whether onboarding can complete without this step. Only ``identity`` is
            required; every other step is skippable and can be revisited from settings.
        complete: Whether this user has already satisfied the step.
    """

    key: str = Field(description="Stable step identifier and URL path segment.")
    title: str
    description: str | None = None
    fields: list[OnboardingField] = Field(default_factory=list)
    required: bool = Field(default=False, description="Whether completion is mandatory.")
    complete: bool = Field(default=False, description="Whether this user satisfied it.")


class OnboardingStatus(Schema):
    """Where the user is in the wizard — the payload behind ``GET /onboarding/status``.

    Attributes:
        complete: Whether onboarding has finished. Mirrors ``users.onboarded_at is not
            None``; the desktop app shows the wizard when this is ``False``.
        current_step: Key of the first incomplete step, or ``None`` once everything is
            done. This is what the client resumes to after a restart.
        steps: Every step with its completion state, so the wizard can render a progress
            rail and allow jumping back.
        progress: Fraction of steps completed, in [0, 1].
    """

    complete: bool = Field(default=False, description="Whether onboarding has finished.")
    current_step: str | None = Field(
        default=None,
        description="Key of the first incomplete step; None when finished.",
    )
    steps: list[OnboardingStep] = Field(default_factory=list)
    progress: float = Field(
        default=_PROGRESS_FLOOR,
        ge=_PROGRESS_FLOOR,
        le=_PROGRESS_CEILING,
        description="Fraction of steps completed, in [0, 1].",
    )


# ======================================================================================
# Per-step payloads
# ======================================================================================


class IdentityPayload(Schema):
    """``identity`` — who the user is.

    The only step with a required field: a generated resume must carry a name, and there is
    no honest way to invent one.
    """

    full_name: str = Field(min_length=1, description="Name printed on generated documents.")
    email: str | None = Field(
        default=None,
        description="Contact address. Defaults to the account email when omitted.",
    )
    pronouns: str | None = None
    location: str | None = Field(
        default=None,
        description='Resume-style "City, Region, Country".',
    )

    @field_validator("full_name")
    @classmethod
    def _require_non_blank_name(cls, value: str) -> str:
        """Reject a name that is only whitespace."""
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("full_name must not be blank")
        return trimmed


class ContactPayload(Schema):
    """``contact`` — phone and postal address, as application forms ask for them."""

    phone: str | None = Field(
        default=None,
        description="Stored exactly as typed; reformatting breaks provider validation.",
    )
    location: str | None = None
    address: dict[str, Any] = Field(
        default_factory=dict,
        description="line1, line2, city, region, postal_code, country.",
    )


class WorkAuthorizationPayload(Schema):
    """``work_authorization`` — right to work, sponsorship posture, and clearance.

    Never inferred. ``unknown`` is the honest default and is preferred over a guess,
    because these values feed hard-negative scoring rules that the LLM may not override.
    """

    citizenship: str | None = None
    work_authorization: WorkAuthStatus = Field(
        default=WorkAuthStatus.UNKNOWN,
        description="Stated right-to-work status; never inferred from anything else.",
    )
    requires_sponsorship: bool = Field(
        default=False,
        description="Whether the user needs employer visa sponsorship.",
    )
    clearance: str | None = Field(
        default=None,
        description='Security clearance level, if any ("none", "secret", "ts_sci").',
    )


class DemographicsPayload(Schema):
    """``demographics`` — voluntary EEO self-identification. Every field is optional.

    Omitting a field means "not answered". Sending
    :data:`~app.models.profile.DECLINE_TO_SELF_IDENTIFY` means "asked and declined", which
    is the only one of the two that may be submitted on an application form.

    ``decline_all`` is the express path for a user who skips the step: the service sets
    every still-unanswered field to the decline sentinel so later form fills have a
    submittable value instead of escalating to human review.
    """

    gender: str | None = None
    race_ethnicity: str | None = None
    disability_status: str | None = None
    veteran_status: str | None = None
    decline_all: bool = Field(
        default=False,
        description="Set every unanswered field to the decline-to-self-identify sentinel.",
    )


class PreferencesPayload(PreferencesUpdate):
    """``preferences`` — the automation policy of ``docs/CONTRACTS.md`` §5.

    Inherits :class:`~app.schemas.user.PreferencesUpdate` verbatim rather than restating
    seventeen fields: the wizard and the settings screen write exactly the same document,
    and duplicating the shape would guarantee they eventually diverge.
    """


class LinksPayload(Schema):
    """``links`` — profile URLs, which double as knowledge sources.

    The four named slots are the ones application forms ask for by name. Anything else goes
    in ``other`` as a ``{label: url}`` map.
    """

    github: str | None = None
    linkedin: str | None = None
    portfolio: str | None = None
    website: str | None = None
    other: dict[str, str] = Field(
        default_factory=dict,
        description="Additional labelled URLs that have no named slot.",
    )
    index_now: bool = Field(
        default=True,
        description="Register the supplied links as knowledge sources immediately.",
    )


class SourcesPayload(Schema):
    """``sources`` — everything else the knowledge indexer should read.

    Repositories, project folders, LinkedIn exports, interview notes. Each entry is a
    :class:`~app.schemas.knowledge.SourceCreate`, so this step and
    ``POST /knowledge/sources`` accept the identical shape.
    """

    sources: list[SourceCreate] = Field(
        default_factory=list,
        description="Knowledge sources to register for this user.",
    )
    index_now: bool = Field(
        default=True,
        description="Queue an indexing pass for the new sources immediately.",
    )


class MasterResumePayload(Schema):
    """``master_resume`` — an existing resume to parse into facts.

    Golden rule #6: the uploaded document is *not* stored as a master copy. It is
    registered as a ``resume`` knowledge source, parsed into
    :class:`~app.models.knowledge.KnowledgeFact` rows, and every future resume is generated
    from those. The original file remains available as evidence, never as a template.

    Exactly one of ``file_id``, ``uri`` or ``text`` must be supplied.
    """

    file_id: uuid.UUID | None = Field(
        default=None,
        description="Identifier of a previously uploaded file to parse.",
    )
    uri: str | None = Field(
        default=None,
        description="Absolute filesystem path or URL of the resume to parse.",
    )
    text: str | None = Field(
        default=None,
        description="Raw resume text, for a paste-in flow with no file at all.",
    )
    label: str | None = Field(
        default=None,
        description="Human name for the resulting knowledge source.",
    )

    @model_validator(mode="after")
    def _require_exactly_one_input(self) -> MasterResumePayload:
        """Reject a payload that supplies zero or several resume inputs.

        Accepting more than one would make the precedence rule invisible, and accepting
        none would create an empty source that silently indexes nothing.

        Returns:
            This same instance.

        Raises:
            ValueError: If the number of supplied inputs is not exactly one.
        """
        supplied = [
            name
            for name, value in (
                ("file_id", self.file_id),
                ("uri", self.uri),
                ("text", self.text),
            )
            if value not in (None, "")
        ]
        if len(supplied) != 1:
            raise ValueError(
                "exactly one of file_id, uri or text is required, got "
                + (", ".join(supplied) if supplied else "none")
            )
        return self


#: Step key to the payload model that step accepts. ``POST /onboarding/steps/{step}``
#: resolves the request body's schema through this map, so adding a step is a matter of
#: adding one entry rather than extending a conditional.
ONBOARDING_PAYLOAD_MODELS: Final[dict[str, type[BaseModel]]] = {
    "identity": IdentityPayload,
    "contact": ContactPayload,
    "work_authorization": WorkAuthorizationPayload,
    "demographics": DemographicsPayload,
    "preferences": PreferencesPayload,
    "links": LinksPayload,
    "sources": SourcesPayload,
    "master_resume": MasterResumePayload,
}
