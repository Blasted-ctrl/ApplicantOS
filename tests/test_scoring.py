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

#: The posting from the pack's own worked example: remote, new-grad, embedded robotics
#: firmware in C++, asking for 8+ years, and unable to sponsor.
CANONICAL_DESCRIPTION = (
    "We are hiring a new grad engineer to work on embedded systems for our robotics "
    "platform. You will write firmware and low-level drivers in modern C++ for our "
    "autonomous mobile robot fleet. Requires 8+ years of relevant experience. "
    "We cannot sponsor visas for this position."
)

#: The exact components the header says must fire, and their points.
CANONICAL_COMPONENTS: dict[str, int] = {
    "embedded": 40,
    "robotics": 30,
    "firmware": 25,
    "cpp": 15,
    "new_grad": 10,
    "remote": 10,
    "requires_senior_experience": -20,
    "sponsorship_unavailable": -40,
}

CANONICAL_TOTAL = 70


def _posting(**overrides) -> RawPosting:
    """A :class:`RawPosting` shaped like the canonical example."""
    values = {
        "provider": ATSProviderName.GREENHOUSE,
        "external_id": "canonical-1",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "title": "Embedded Robotics Firmware Engineer",
        "company_name": "Acme Robotics",
        "description": CANONICAL_DESCRIPTION,
        "location": "Remote — US",
        "work_arrangement": WorkArrangement.REMOTE,
        # Deliberately UNKNOWN: the worked example states no employment type, and
        # declaring one fires the extra `full_time_role` rule (+5) that is not in it.
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


def test_the_canonical_posting_totals_seventy(scorer) -> None:
    """The header of ``scoring_rules.yaml`` says this pack must reproduce 70. It must."""
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
    """"We cannot sponsor" costs 40 points but does not veto — and that is correct.

    A posting refusing to sponsor is irrelevant to an applicant who needs no sponsorship, so
    treating it as a hard negative would silently delete valid jobs from their feed. The
    worked example depends on exactly this: the penalty applies, the veto does not, and the
    total is 70 rather than a floored score.
    """
    result = scorer.score_rules(_posting())
    fired = {c.rule: c.points for c in result.matched_components}

    assert fired["sponsorship_unavailable"] == -40
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
    """"go" must not match "going", and "c++" must survive punctuation stripping."""
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
    prefs = UserPreferences(blocked_companies=["Acme Robotics"])
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
