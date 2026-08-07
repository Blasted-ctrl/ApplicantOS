"""Scoring schemas — the number, the arithmetic behind it, and the rule pack.

A score is the decision record standing between discovery and action, so
``docs/CONTRACTS.md`` §10 makes explainability a hard requirement rather than a nicety. That
requirement shapes these models:

* :class:`ScoreRead` keeps the deterministic result (``total``, ``normalized``) apart from
  the optional LLM commentary (``rationale``, ``model_used``). The model may nudge a score
  by ±10 and write prose; it may never flip a hard negative — sponsorship mismatch, blocked
  company, blocked industry — into "apply". Keeping the two in separate fields is what makes
  that separation visible in the API rather than merely intended.
* :class:`ScoreComponentRead` is one line of the arithmetic: which rule fired, for how many
  points, and why. ``breakdown`` on the row is the serialised list; ``components`` is that
  list parsed, so the desktop score panel renders the sum without re-running the engine.
* :class:`ScoreRuleSchema` and :class:`ScoringRulesUpdate` back
  ``GET|PUT /settings/scoring-rules``, letting a user retune the pack that
  ``app/config/scoring_rules.yaml`` seeds.

``hard_negative`` is the load-bearing flag on both the component and the rule: a component
carrying it is the one thing the LLM adjustment step is forbidden to overturn.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas.common import Schema

__all__ = [
    "MAX_NORMALIZED_SCORE",
    "MIN_NORMALIZED_SCORE",
    "ScoreComponentRead",
    "ScoreRead",
    "ScoreRuleSchema",
    "ScoreVerdict",
    "ScoringRulesUpdate",
]

#: Lower bound of the normalised score band. ``total`` is unbounded — rules may sum to
#: anything — while ``normalized`` is the clamped figure every threshold compares against.
MIN_NORMALIZED_SCORE: int = 0

#: Upper bound of the normalised score band.
MAX_NORMALIZED_SCORE: int = 100

#: Routing decision derived from the normalised score and the hard-negative rules. Stored
#: as a plain string column (``docs/CONTRACTS.md`` §3 declares no verdict enum), so the
#: literal is enforced at the schema boundary instead.
ScoreVerdict = Literal["apply", "review", "skip"]


class ScoreComponentRead(Schema):
    """One line of a score's arithmetic: a rule that fired, and what it contributed.

    Attributes:
        key: Stable identifier of the rule that produced this component.
        label: Human-facing name rendered in the score panel.
        points: Signed contribution to ``ScoreRead.total``. Negative components are just as
            important to show as positive ones.
        weight: Multiplier applied to the rule's base points, when the pack defines one.
        matched: Whether the rule's condition held. Non-matching components are retained
            with ``points=0`` so the panel can show what was *checked*, not only what fired.
        hard_negative: Whether this component is a policy veto (sponsorship mismatch,
            blocked company, blocked industry). The LLM adjustment step may never overturn
            a component carrying this flag.
        detail: Short explanation of why the rule matched, quoting the evidence.
    """

    key: str = Field(description="Stable rule identifier.")
    label: str = Field(default="", description="Human-facing rule name.")
    points: float = Field(default=0.0, description="Signed contribution to the total.")
    weight: float = Field(default=1.0, description="Multiplier applied to base points.")
    matched: bool = Field(default=True, description="Whether the rule's condition held.")
    hard_negative: bool = Field(
        default=False,
        description="Policy veto the LLM adjustment step may never overturn.",
    )
    detail: str | None = Field(default=None, description="Why the rule matched.")


class ScoreRead(Schema):
    """How well one posting fits one user, with the reasoning attached.

    Exactly one row exists per ``(posting, user)`` pair — re-scoring is an upsert — so this
    is always the current verdict, never one of several.
    """

    id: uuid.UUID
    posting_id: uuid.UUID
    user_id: uuid.UUID
    total: int = Field(
        default=0,
        description="Raw sum of every fired rule's points; may be negative or exceed 100.",
    )
    normalized: int = Field(
        default=0,
        ge=MIN_NORMALIZED_SCORE,
        le=MAX_NORMALIZED_SCORE,
        description="`total` clamped to 0-100; the figure every threshold compares against.",
    )
    breakdown: dict[str, Any] = Field(
        default_factory=dict,
        description="Serialised component list exactly as the engine stored it.",
    )
    components: list[ScoreComponentRead] = Field(
        default_factory=list,
        description="`breakdown` parsed for rendering; populated by the service layer.",
    )
    verdict: ScoreVerdict | None = Field(
        default=None,
        description="Routing decision derived from `normalized` and the hard negatives.",
    )
    model_used: str | None = Field(
        default=None,
        description="Model that wrote `rationale`; None when scoring ran purely on rules.",
    )
    rationale: str | None = Field(
        default=None,
        description="Advisory explanation. Never the source of the number.",
    )
    created_at: datetime
    updated_at: datetime

    @property
    def is_clamped(self) -> bool:
        """Whether ``total`` fell outside the normalised band and had to be clipped.

        A pack that clamps constantly has drifted out of calibration; surfacing it is
        cheaper than discovering it from user complaints.
        """
        return not (MIN_NORMALIZED_SCORE <= self.total <= MAX_NORMALIZED_SCORE)


class ScoreRuleSchema(Schema):
    """One rule in the scoring pack — the editable form of ``app/config/scoring_rules.yaml``.

    The engine is deterministic and pure: given a posting, a profile and this pack, it
    produces the same components every time. That is what makes a score reproducible months
    later, and it is why the pack is data rather than code.

    Attributes:
        key: Stable identifier, unique within the pack. Referenced by
            :attr:`ScoreComponentRead.key`.
        label: Human-facing name.
        description: What the rule is trying to capture.
        points: Base contribution when the rule matches. Negative for penalties.
        weight: Multiplier applied to ``points``, so a pack can be retuned without
            rewriting every rule's arithmetic.
        category: Grouping for the score panel ("skills", "location", "compensation",
            "policy").
        enabled: Disabled rules stay in the pack — and stay visible — but never fire.
        hard_negative: Marks a policy veto. A matching hard negative forces the verdict to
            ``skip`` regardless of the numeric total, and the LLM may not override it.
        criteria: Rule-specific matching configuration (keywords, thresholds, field paths),
            interpreted by the rule's own evaluator.
    """

    key: str = Field(min_length=1, description="Stable rule identifier, unique in the pack.")
    label: str = Field(default="", description="Human-facing rule name.")
    description: str | None = None
    points: float = Field(default=0.0, description="Base contribution when matched.")
    weight: float = Field(default=1.0, description="Multiplier applied to `points`.")
    category: str = Field(default="general", description="Grouping for the score panel.")
    enabled: bool = Field(default=True, description="Disabled rules never fire.")
    hard_negative: bool = Field(
        default=False,
        description="Policy veto: forces `skip` and cannot be overturned by the LLM.",
    )
    criteria: dict[str, Any] = Field(
        default_factory=dict,
        description="Rule-specific matching configuration.",
    )


class ScoringRulesUpdate(Schema):
    """Replacement rule pack — the body of ``PUT /settings/scoring-rules``.

    The whole pack is submitted at once rather than patched rule by rule, because rules are
    tuned relative to each other: a partial update would let a user raise one weight without
    seeing what it did to the balance of the rest. Duplicate keys are rejected at the
    boundary, since a duplicate would make ``ScoreComponentRead.key`` ambiguous.
    """

    rules: list[ScoreRuleSchema] = Field(
        default_factory=list,
        description="The complete rule pack, replacing the stored one.",
    )
    reset_to_defaults: bool = Field(
        default=False,
        description="Discard `rules` and restore the packaged DEFAULT_RULES.",
    )

    @model_validator(mode="after")
    def _reject_duplicate_keys(self) -> ScoringRulesUpdate:
        """Reject a pack containing two rules with the same key.

        ``ScoreComponentRead.key`` is how the score panel attributes points to a rule and
        how a saved pack is diffed against the defaults. A duplicate key would make both
        ambiguous and would silently shadow one of the two rules, so it is rejected here
        rather than resolved by an invisible last-wins policy.

        Returns:
            This same instance.

        Raises:
            ValueError: If any key appears more than once.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for rule in self.rules:
            if rule.key in seen:
                duplicates.append(rule.key)
            seen.add(rule.key)
        if duplicates:
            raise ValueError(
                "duplicate scoring rule key(s): " + ", ".join(sorted(set(duplicates)))
            )
        return self
