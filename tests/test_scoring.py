"""The deterministic scoring engine (``docs/CONTRACTS.md`` §10).

``Scorer.score_rules`` is the one part of the AI layer that is **pure, synchronous and
deterministic**, and everything downstream leans on that: it decides which postings get a
resume generated, and — through ``auto_apply_min_score`` — which ones get submitted at all. A
scorer that drifted between runs would make the auto-apply floor meaningless.

Four properties are asserted here:

**The canonical 70.** ``app/config/scoring_rules.yaml`` documents a worked example in its own
header and states that the pack "must reproduce it exactly". That is a claim the file makes
about itself, so it is checked component by component rather than only on the total — a total
of 70 reached by a different combination of rules would be a coincidence, not agreement.

**Determinism over 100 runs**, because a set iteration order or a ``hash()`` call is exactly
the kind of thing that produces a stable answer nineteen times and a different one on the
twentieth.

**Word boundaries.** ``"go"`` must not match "going", and ``"c++"`` must survive tokenisation
that would ordinarily strip punctuation.

**The hard-negative lock.** §10 forbids the optional LLM pass from flipping a sponsorship,
blocked-company or blocked-industry negative into "apply". That is the rule that stops a
model talking the system into applying for a job the user cannot legally take.
"""

from __future__ import annotations

import pytest

from app.ai.scoring import (
    Scorer,
    default_rules,
    load_rules,
    matches_term,
    normalize_text,
)
from app.jobs.base import RawPosting
from app.models.enums import ATSProviderName, EmploymentType, WorkArrangement
from app.models.user import UserPreferences

#: The posting from the pack's own worked example: a fully remote software engineering
#: internship for summer, in Python and React on AWS, from an employer that cannot sponsor.
#:
#: The employer's inability to sponsor is deliberately still in the text. The pack is
#: written for a US citizen, so it carries no signal and must contribute nothing — a rule
#: that fired here would be subtracting points for a fact that does not affect this
#: candidate at all.
CANONICAL_DESCRIPTION = (
    "Join our summer 2027 internship program. You will write Python and React, deploying "
    "services on AWS alongside a mentor. We are unable to sponsor visas for this role."
)

#: The exact components the header says must fire, and their points.
CANONICAL_COMPONENTS: dict[str, int] = {
    "internship_title": 45,
    "software_engineering_title": 30,
    "remote": 25,
    "structured_program": 10,
    "python": 10,
    "summer_season": 8,
    "web_frameworks": 8,
    "cloud_platform": 8,
    "sponsorship_unavailable": -2,
}

CANONICAL_TOTAL = 142


def _posting(**overrides) -> RawPosting:
    """A :class:`RawPosting` shaped like the canonical example."""
    values = {
        "provider": ATSProviderName.GREENHOUSE,
        "external_id": "canonical-1",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "title": "Software Engineer Intern",
        "company_name": "Acme Software",
        "description": CANONICAL_DESCRIPTION,
        "location": "Remote — US",
        "work_arrangement": WorkArrangement.REMOTE,
        # Deliberately UNKNOWN: the worked example states no employment type, and
        # declaring `internship` fires `internship_employment_type` (+15), which is a real
        # rule but not one of the seven the header enumerates.
        "employment_type": EmploymentType.UNKNOWN,
    }
    values.update(overrides)
    return RawPosting(**values)


@pytest.fixture
def scorer() -> Scorer:
    """A scorer over the packaged rules and default preferences."""
    return Scorer(prefs=UserPreferences())


# ======================================================================================
# The canonical worked example
# ======================================================================================


def test_the_canonical_posting_totals_its_documented_figure(scorer) -> None:
    """The header of ``scoring_rules.yaml`` documents a total. The pack must reproduce it.

    The number itself is not the point — keeping the file's own worked example honest is.
    A pack whose documentation describes a different score than the code produces is worse
    than an undocumented one, because the reader has no reason to doubt it.
    """
    result = scorer.score_rules(_posting())
    assert result.total == CANONICAL_TOTAL


def test_the_canonical_total_is_reached_by_the_documented_rules(scorer) -> None:
    """Component-by-component, so 70 cannot be reached by a different accident."""
    result = scorer.score_rules(_posting())
    fired = {component.rule: component.points for component in result.matched_components}

    assert fired == CANONICAL_COMPONENTS


def test_the_canonical_total_equals_the_sum_of_its_components(scorer) -> None:
    """The arithmetic itself, so a scoring bug cannot hide behind the right component set."""
    result = scorer.score_rules(_posting())
    assert sum(c.contribution() for c in result.matched_components) == CANONICAL_TOTAL
    assert sum(CANONICAL_COMPONENTS.values()) == CANONICAL_TOTAL


def test_cannot_sponsor_is_a_penalty_not_a_veto_for_a_candidate_who_needs_none(
    scorer,
) -> None:
    """ "We cannot sponsor" is scored but never vetoes for someone who needs no sponsorship.

    A posting refusing to sponsor is irrelevant to such an applicant, so treating it as a
    hard negative would silently delete valid jobs from their feed. The points are near
    zero for the same reason — this pack is written for a US citizen, and the rule survives
    mainly so the veto path below stays reachable.
    """
    result = scorer.score_rules(_posting())
    fired = {c.rule: c.points for c in result.matched_components}

    assert fired["sponsorship_unavailable"] == -2
    assert result.has_hard_negative is False
    assert result.total == CANONICAL_TOTAL


def test_cannot_sponsor_is_a_veto_for_a_candidate_who_needs_sponsorship() -> None:
    """The symmetrical case, which is the one that matters in practice."""
    prefs = UserPreferences(require_no_sponsorship=False)
    result = Scorer(prefs=prefs).score_rules(_posting())

    assert result.has_hard_negative is True
    assert any("sponsorship" in c.rule for c in result.hard_negatives)


# ======================================================================================
# Determinism
# ======================================================================================


def test_score_rules_is_deterministic_over_a_hundred_runs(scorer) -> None:
    """Same posting, same answer, every time.

    A set iteration order or a salted ``hash()`` produces a stable result most of the time,
    which is precisely why one run is not enough evidence.
    """
    posting = _posting()
    totals = {scorer.score_rules(posting).total for _ in range(100)}
    assert totals == {CANONICAL_TOTAL}


def test_the_component_breakdown_is_deterministic_too(scorer) -> None:
    """Not just the total: the explanation shown to the user must be stable as well."""
    posting = _posting()
    first = tuple((c.rule, c.points) for c in scorer.score_rules(posting).matched_components)
    for _ in range(50):
        again = tuple((c.rule, c.points) for c in scorer.score_rules(posting).matched_components)
        assert again == first


def test_two_scorers_over_the_same_pack_agree(scorer) -> None:
    """Determinism across instances, not merely across calls on one object."""
    posting = _posting()
    other = Scorer(prefs=UserPreferences())
    assert other.score_rules(posting).total == scorer.score_rules(posting).total
    assert other.rules_hash == scorer.rules_hash


def test_score_rules_does_not_mutate_the_posting(scorer) -> None:
    """A pure function leaves its input alone."""
    posting = _posting()
    before = (posting.title, posting.description, posting.location)
    scorer.score_rules(posting)
    assert (posting.title, posting.description, posting.location) == before


def test_score_rules_needs_no_event_loop_and_no_model(scorer) -> None:
    """It is synchronous by contract — callable from a plain function, with no LLM."""
    assert isinstance(scorer.score_rules(_posting()).total, int)


# ======================================================================================
# Word boundaries
# ======================================================================================


@pytest.mark.parametrize(
    ("haystack", "term", "expected"),
    [
        ("we use go for services", "go", True),
        ("the project is going well", "go", False),
        ("a golang shop", "go", False),
        ("experience with r and python", "r", True),
        ("great career growth", "r", False),
        ("written in c++", "c++", True),
        ("written in c", "c++", False),
        ("modern c++17 codebase", "c++", True),
        ("we love rust", "rust", True),
        ("trustworthy engineers", "rust", False),
    ],
)
def test_term_matching_respects_word_boundaries(haystack, term, expected) -> None:
    """ "go" must not match "going", and "c++" must survive punctuation stripping."""
    assert matches_term(normalize_text(haystack), term) is expected


def test_a_substring_company_name_does_not_fire_a_keyword_rule(scorer) -> None:
    """A rule keyed on a word must not fire on a longer word containing it."""
    benign = _posting(
        title="Sales Associate",
        description="Retail sales for a growing team. Going places.",
        work_arrangement=WorkArrangement.ONSITE,
        company_name="Gopher Retail",
    )
    result = scorer.score_rules(benign)
    assert "embedded" not in {c.rule for c in result.matched_components}


def test_cpp_is_matched_and_c_alone_is_not(scorer) -> None:
    """The punctuation case, end to end through the scorer rather than the matcher."""
    with_cpp = _posting(description="Firmware written in C++ for embedded robotics.")
    without = _posting(description="Firmware written in C for embedded robotics.")

    keys_with = {c.rule for c in scorer.score_rules(with_cpp).matched_components}
    keys_without = {c.rule for c in scorer.score_rules(without).matched_components}

    assert "cpp" in keys_with
    assert "cpp" not in keys_without


# ======================================================================================
# The hard-negative lock
# ======================================================================================


async def test_the_llm_may_not_flip_a_hard_negative_into_apply(scorer, monkeypatch) -> None:
    """§10: the adjustment pass may move the total by ±10 and may never flip a hard negative.

    The model here is maximally adversarial: it returns a huge positive adjustment and an
    explicit "apply" verdict for a posting that cannot sponsor.
    """

    class OverEagerModel:
        async def complete_json(self, **_kwargs):
            return {"adjustment": 100, "verdict": "apply", "rationale": "great fit!"}

        async def complete(self, **_kwargs):
            raise AssertionError("scoring should use complete_json")

        def count_tokens(self, text: str) -> int:
            return 1

    # A candidate who needs sponsorship, applying to a posting that refuses to sponsor:
    # a preference gate, which is where hard negatives actually come from.
    prefs = UserPreferences(require_no_sponsorship=False)
    baseline = Scorer(prefs=prefs).score_rules(_posting())
    assert baseline.has_hard_negative is True, "the fixture must carry a hard negative"

    hostile = Scorer(prefs=prefs, llm=OverEagerModel())
    result = await hostile.score(_posting(), use_llm=True)

    assert result.has_hard_negative is True
    assert result.verdict != "apply", (
        "the model talked the scorer into applying for a job the user cannot legally take"
    )


async def test_the_llm_adjustment_is_bounded(monkeypatch) -> None:
    """Even without a hard negative, the model may not rewrite the score wholesale."""

    class Runaway:
        async def complete_json(self, **_kwargs):
            return {"adjustment": 10_000, "verdict": "apply", "rationale": ""}

        def count_tokens(self, text: str) -> int:
            return 1

    posting = _posting(description="Embedded robotics firmware in C++ for new grads.")
    baseline = Scorer(prefs=UserPreferences()).score_rules(posting).total
    adjusted = await Scorer(prefs=UserPreferences(), llm=Runaway()).score(posting, use_llm=True)

    assert abs(adjusted.total - baseline) <= 10


async def test_use_llm_false_reproduces_the_rule_total(scorer) -> None:
    """``use_llm=False`` is exactly ``score_rules``; no hidden adjustment sneaks in."""
    posting = _posting()
    assert (await scorer.score(posting, use_llm=False)).total == scorer.score_rules(posting).total


def test_a_blocked_company_is_a_hard_negative() -> None:
    """A user's block list is a hard negative, not a soft preference."""
    prefs = UserPreferences(blocked_companies=["Acme Software"])
    result = Scorer(prefs=prefs).score_rules(_posting())
    assert result.has_hard_negative is True


async def test_a_defense_employer_is_a_hard_negative(session, make_posting) -> None:
    """The third veto class, driven by the enriched ``companies.is_defense`` flag.

    Deliberately not keyword-driven: a posting mentioning "defense" is a textual signal worth
    a penalty, whereas the employer actually *being* a defence contractor is a decision the
    user already made. Scored against a persisted posting so the company relationship is real.
    """
    from app.models.company import Company

    employer = Company(
        name="General Defense Systems",
        normalized_name="general defense systems",
        is_defense=True,
    )
    session.add(employer)
    await session.commit()

    posting = await make_posting(company_id=employer.id, title="Embedded Firmware Engineer")
    await session.refresh(posting, ["company"])

    permissive = Scorer(prefs=UserPreferences(exclude_defense=False)).score_rules(posting)
    strict = Scorer(prefs=UserPreferences(exclude_defense=True)).score_rules(posting)

    assert permissive.has_hard_negative is False
    assert strict.has_hard_negative is True


# ======================================================================================
# The pack itself
# ======================================================================================


def test_the_packaged_rules_load() -> None:
    """``scoring_rules.yaml`` parses and is not empty."""
    rules = default_rules()
    assert len(rules) > 5
    assert all(rule.key for rule in rules)


def test_rule_keys_are_unique() -> None:
    """A duplicated key would silently shadow a rule in the breakdown."""
    keys = [rule.key for rule in default_rules()]
    assert len(keys) == len(set(keys))


def test_load_rules_is_idempotent() -> None:
    """Two loads produce equal packs, so the pack hash is stable across processes."""
    first = [rule.to_dict() for rule in load_rules()]
    second = [rule.to_dict() for rule in load_rules()]
    assert first == second


def test_every_canonical_rule_exists_in_the_pack() -> None:
    """Guards the worked example against a rule being renamed out from under it."""
    keys = {rule.key for rule in default_rules()}
    missing = set(CANONICAL_COMPONENTS) - keys
    assert not missing, f"the worked example references rules that no longer exist: {missing}"


def test_normalized_score_is_clamped(scorer) -> None:
    """The stored ``normalized`` figure is always inside 0-100 whatever the raw total."""
    assert Scorer.normalize_total(500) == 100
    assert Scorer.normalize_total(-500) == 0
    assert Scorer.normalize_total(70) == 70


@pytest.mark.parametrize(
    ("total", "expected"),
    [(100, "apply"), (70, "apply"), (69, "review"), (0, "skip")],
)
def test_verdict_thresholds(total, expected) -> None:
    """The routing decision the pipeline reads."""
    assert Scorer(prefs=UserPreferences(min_score=70)).verdict_for(total) == expected


def test_score_read_derives_components_from_a_stored_breakdown() -> None:
    """``ScoreRead`` reconstructs the arithmetic from the JSON column it was built from.

    ``Score`` has a ``breakdown`` column and no ``components`` attribute, so every
    ``ScoreRead.model_validate(row)`` in the API used to return an empty component list and
    the desktop score panel reported "No rule contributed to this score" over a full
    breakdown. Deriving it in the schema is what makes that unforgettable.
    """
    import uuid
    from datetime import UTC, datetime

    from app.schemas.scoring import ScoreRead

    result = Scorer(prefs=UserPreferences(min_score=70)).score_rules(_posting())
    now = datetime.now(UTC)
    read = ScoreRead(
        id=uuid.uuid4(),
        posting_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        total=result.total,
        normalized=result.normalized,
        breakdown=result.to_breakdown(),
        created_at=now,
        updated_at=now,
    )

    assert [component.key for component in read.components] == [
        component.rule for component in result.components
    ]
    assert sum(component.points for component in read.components) == result.total


def test_score_read_survives_a_breakdown_it_cannot_parse() -> None:
    """A malformed stored breakdown yields no components rather than a 500."""
    import uuid
    from datetime import UTC, datetime

    from app.schemas.scoring import ScoreRead

    now = datetime.now(UTC)
    read = ScoreRead(
        id=uuid.uuid4(),
        posting_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        breakdown={"components": ["not a mapping", {"points": "not a number"}, {"key": "ok"}]},
        created_at=now,
        updated_at=now,
    )

    assert [component.key for component in read.components] == ["ok"]
