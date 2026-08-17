"""Matching a posting to a person, deterministically (directive §5).

The contract these tests defend is the same one
:meth:`app.ai.scoring.Scorer.score_rules` holds: pure, synchronous, no clock, no network, no
model, identical output for identical input forever. The dashboard ranks postings against
each other, so a match score that drifts between runs makes every comparison it shows a lie.

Two of these tests exist because the obvious implementation is wrong in a way that still
produces plausible numbers:

* ``test_a_two_document_corpus_is_refused`` — a textbook TF-IDF over {résumé, posting}
  weights the shared vocabulary *lowest*, suppressing exactly the overlap that constitutes
  the match.
* ``test_a_posting_with_no_recognised_skills_is_neutral`` — scoring the skills signal zero
  when the posting names none punishes a posting for being written in prose.
"""

from __future__ import annotations

import pytest

from app.ai.similarity import (
    DEFAULT_WEIGHTS,
    MIN_CORPUS_DOCUMENTS,
    ApplicantProfile,
    MatchWeights,
    build_idf,
    cosine,
    match,
    title_relevance,
    tokenize,
    weight,
)

ROBOTICS_EVIDENCE = [
    "Built an autonomous robot navigation stack in C++ and ROS2 on Linux, with path planning "
    "and sensor fusion running on an embedded controller.",
    "Wrote Python tooling for firmware test automation; used Git, Docker and CMake.",
]

ROBOTICS_POSTING = (
    "Robotics Software Engineer",
    "You will build autonomous navigation for our mobile robots using C++ and ROS2 on Linux. "
    "Experience with path planning, sensor fusion and real-time embedded systems required. "
    "Python is a plus.",
)

MARKETING_POSTING = (
    "Senior Marketing Manager",
    "Own our brand strategy, run paid social campaigns, manage agency relationships and report "
    "on funnel performance to the executive team. Salesforce experience required.",
)


@pytest.fixture
def profile() -> ApplicantProfile:
    """One indexed applicant: a robotics and embedded engineer."""
    return ApplicantProfile.build(
        ROBOTICS_EVIDENCE,
        titles=["Robotics Software Engineer", "Embedded Software Engineer"],
    )


# ======================================================================================
# Tokenisation
# ======================================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C++ and C#", ["c++", "c#"]),
        ("We use .NET and Node.js.", ["use", ".net", "node.js"]),
        ("Python, python, PYTHON", ["python", "python", "python"]),
    ],
)
def test_technology_punctuation_survives(text: str, expected: list[str]) -> None:
    """``c++`` must not become ``c``.

    Stripping punctuation merges distinct technologies, and a résumé that says C would then
    look like a match for a posting that wants C++ on the strongest signal there is.
    """
    assert tokenize(text) == expected


def test_stopwords_and_single_letters_go_but_real_ones_stay() -> None:
    """``c`` and ``r`` are languages; ``a`` and ``the`` are not."""
    tokens = tokenize("The candidate should know C and R and a bit of Go")
    assert "c" in tokens
    assert "r" in tokens
    assert "go" in tokens
    assert "the" not in tokens
    assert "a" not in tokens


def test_tokenize_is_total() -> None:
    """``None`` and empty text are documents with no terms, not errors."""
    assert tokenize(None) == []
    assert tokenize("") == []
    assert tokenize("   ") == []


# ======================================================================================
# Vectors
# ======================================================================================


def test_repeating_a_word_does_not_repeat_its_weight() -> None:
    """Sublinear TF: nine mentions of Python is not nine times more about Python.

    Raw counts let one repeated word dominate a whole vector, which is exactly how a posting
    with a keyword-stuffed footer scores as a perfect match for anything.
    """
    once = weight(tokenize("python"))
    many = weight(tokenize("python " * 9))
    assert once == many  # L2-normalised, single term: both are {python: 1.0}

    mixed = weight(tokenize("python " * 9 + "rust"))
    assert mixed["python"] < 9 * mixed["rust"]


def test_cosine_is_bounded_and_symmetric(profile: ApplicantProfile) -> None:
    """A similarity outside ``[0, 1]`` would break every threshold that compares against it."""
    other = weight(tokenize(ROBOTICS_POSTING[1]))
    forward = cosine(profile.vector, other)
    assert 0.0 <= forward <= 1.0
    assert forward == cosine(other, profile.vector)
    assert cosine({}, other) == 0.0


def test_a_two_document_corpus_is_refused() -> None:
    """The trap this module exists to avoid.

    With two documents, a term appearing in *both* gets the lowest IDF weight in the vector,
    so the shared vocabulary that constitutes the match is precisely what gets suppressed.
    :func:`build_idf` returns nothing below :data:`MIN_CORPUS_DOCUMENTS`, which callers read
    as "use sublinear TF" — corpus-free, and honest about it.
    """
    assert build_idf([ROBOTICS_POSTING[1], "\n".join(ROBOTICS_EVIDENCE)]) == {}


def test_a_real_corpus_produces_weights() -> None:
    """Past the floor, common terms are weighted below rare ones."""
    corpus = [f"software engineer python posting number {index}" for index in range(30)]
    corpus.append("software engineer rust embedded systems")

    idf = build_idf(corpus)

    assert idf
    assert idf["rust"] > idf["software"]


# ======================================================================================
# Titles
# ======================================================================================


@pytest.mark.parametrize(
    ("posting", "targets", "floor"),
    [
        ("Robotics Software Engineer", ["Robotics Software Engineer"], 1.0),
        ("Robotics Software Engineering", ["Robotics Software Engineer"], 1.0),
        ("Software Engineer, Robotics (Ann Arbor)", ["Robotics Software Engineer"], 1.0),
    ],
)
def test_the_same_job_named_differently_still_matches(
    posting: str, targets: list[str], floor: float
) -> None:
    """Grammatical form and padding are not a different job.

    "Engineering" against "Engineer" shared no token before the role-word table, costing a
    third of a perfect title match on one of the most common title pairs there is.
    """
    assert title_relevance(posting, targets) >= floor


def test_a_wildly_different_seniority_is_not_a_match() -> None:
    """Every other token is shared, so token overlap alone rates this near-perfect."""
    intern = "Intern, Robotics Software"
    assert title_relevance(intern, ["Senior Robotics Software Engineer"]) == 0.0


def test_one_step_of_seniority_is_discounted_not_denied() -> None:
    """A senior applicant looking at a staff role is a stretch, not a mismatch."""
    score = title_relevance("Staff Robotics Engineer", ["Senior Robotics Engineer"])
    assert 0.0 < score < 1.0


def test_an_applicant_who_named_no_target_gets_no_title_credit() -> None:
    """Silence is not a claim. Inventing relevance here is the fabrication rule #7 forbids."""
    assert title_relevance("Robotics Software Engineer", []) == 0.0


# ======================================================================================
# The whole match
# ======================================================================================


def test_the_right_job_beats_the_wrong_one_by_a_wide_margin(profile: ApplicantProfile) -> None:
    """The end-to-end sanity check, and the one a user would run first."""
    good = match(profile, title=ROBOTICS_POSTING[0], description=ROBOTICS_POSTING[1])
    bad = match(profile, title=MARKETING_POSTING[0], description=MARKETING_POSTING[1])

    assert good.combined > 0.6
    assert bad.combined < 0.15
    assert good.combined > bad.combined * 3


def test_the_breakdown_names_the_skills_on_both_sides(profile: ApplicantProfile) -> None:
    """Directive §5: the UI has to be able to show *why*, not just how much."""
    result = match(profile, title=ROBOTICS_POSTING[0], description=ROBOTICS_POSTING[1])

    assert "C++" in result.matched_skills
    assert "ROS 2" in result.matched_skills
    assert result.missing_skills
    assert not set(result.matched_skills) & set(result.missing_skills)


def test_the_same_inputs_always_give_the_same_numbers(profile: ApplicantProfile) -> None:
    """Reproducibility is the whole reason no model is involved."""
    first = match(profile, title=ROBOTICS_POSTING[0], description=ROBOTICS_POSTING[1])
    second = match(profile, title=ROBOTICS_POSTING[0], description=ROBOTICS_POSTING[1])

    assert first.to_dict() == second.to_dict()


def test_percentages_are_whole_numbers_that_agree_with_the_ratios(
    profile: ApplicantProfile,
) -> None:
    """The stored ratio and the displayed percentage must not disagree."""
    result = match(profile, title=ROBOTICS_POSTING[0], description=ROBOTICS_POSTING[1])
    percent = result.as_percent()

    assert percent["combined"] == round(result.combined * 100)
    assert all(isinstance(value, int) for value in percent.values())


def test_an_unindexed_applicant_is_incomparable_rather_than_a_zero() -> None:
    """"We have not indexed you" and "you do not fit" are different statements.

    Reporting the first as a confident zero would tell a new user that every posting on
    every board was a bad fit for them.
    """
    empty = ApplicantProfile.build([])
    result = match(empty, title=ROBOTICS_POSTING[0], description=ROBOTICS_POSTING[1])

    assert result.comparable is False
    assert result.combined == 0.0


def test_a_posting_stripped_to_nothing_is_incomparable(profile: ApplicantProfile) -> None:
    """An aggressive scraper leaves four words. That is not evidence of a poor fit."""
    result = match(profile, title="Engineer", description="Apply now.")

    assert result.comparable is False


def test_a_posting_with_no_recognised_skills_is_neutral(profile: ApplicantProfile) -> None:
    """A posting written in prose makes no demand the skills signal can fail.

    Blending in a zero it never earned would rank every plainly-written posting below every
    keyword list, which is the opposite of what a user wants.
    """
    prose = (
        "We are looking for someone thoughtful to help us build reliable things for people "
        "who depend on them. You will work closely with a small group and own what you ship."
    )
    # The *title* is part of the text skills are read from, so it has to name none either —
    # otherwise this would silently exercise the ordinary weighted path instead.
    result = match(profile, title="Member of Technical Staff", description=prose)

    assert result.skills_overlap == 0.0
    assert not result.missing_skills
    # The skills weight is redistributed rather than blended in as a zero, so the résumé
    # similarity this posting *does* have still counts for its full share.
    resume_only = match(
        profile,
        title="Member of Technical Staff",
        description=prose,
        weights=MatchWeights(title=0.0, skills=0.0, resume=1.0),
    )
    assert result.combined == pytest.approx(resume_only.resume_similarity, abs=0.35)


def test_weights_that_sum_to_zero_fall_back_rather_than_silently_disabling(
    profile: ApplicantProfile,
) -> None:
    """A blend of nothing is a misconfiguration, not a score of zero for every posting."""
    assert MatchWeights(title=0.0, skills=0.0, resume=0.0).normalized() == DEFAULT_WEIGHTS


def test_weights_are_scaled_rather_than_taken_literally() -> None:
    """Callers may express a blend in any units; only the ratio matters."""
    scaled = MatchWeights(title=2.0, skills=2.0, resume=1.0).normalized()

    assert scaled.title == pytest.approx(0.4)
    assert scaled.skills == pytest.approx(0.4)
    assert scaled.resume == pytest.approx(0.2)


def test_reweighting_moves_the_combined_score(profile: ApplicantProfile) -> None:
    """The weights are real, not decoration."""
    title_heavy = match(
        profile,
        title=ROBOTICS_POSTING[0],
        description=ROBOTICS_POSTING[1],
        weights=MatchWeights(title=1.0, skills=0.0, resume=0.0),
    )
    resume_heavy = match(
        profile,
        title=ROBOTICS_POSTING[0],
        description=ROBOTICS_POSTING[1],
        weights=MatchWeights(title=0.0, skills=0.0, resume=1.0),
    )

    assert title_heavy.combined == pytest.approx(title_heavy.title_relevance)
    assert resume_heavy.combined == pytest.approx(resume_heavy.resume_similarity)
    assert title_heavy.combined != resume_heavy.combined


def test_the_corpus_floor_is_a_real_number() -> None:
    """Guards the constant itself: a floor of two would reintroduce the bug it prevents."""
    assert MIN_CORPUS_DOCUMENTS > 2
