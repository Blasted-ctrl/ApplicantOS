"""Deterministic resume-to-posting matching (directive §5).

Three numbers and their blend, computed from text alone:

    title relevance      does the job's title name work this applicant does?
    skills overlap       what fraction of the skills the posting asks for do they have?
    resume similarity    how much of the posting's language appears in their evidence?

    combined             the weighted blend, and the number a threshold compares against

**No model is involved and none may be.** The dashboard ranks postings against each other,
so a score that drifts between runs makes every comparison it shows a lie. Everything here is
pure, synchronous and reads no clock, no network and no random source — the same contract
:meth:`app.ai.scoring.Scorer.score_rules` holds, for the same reason. An LLM may *enrich* a
decision elsewhere; it may not be required to reach one.

Two design choices are worth stating, because both are places the obvious approach is wrong.

**TF-IDF is computed against a corpus, or not at all.** Running a textbook TF-IDF over a
two-document corpus — the résumé and the posting — is a well-known way to get a number that
looks right and means the opposite: with two documents, a term appearing in *both* gets the
lowest IDF weight in the vector, so the shared vocabulary that constitutes the match is
exactly what gets suppressed. :func:`build_idf` therefore takes a real corpus (the postings
already ingested for this user), and :func:`weight` accepts ``idf=None`` and falls back to
sublinear-TF cosine, which is corpus-free and honest about it. Both paths are reproducible;
the corpus path is sharper.

**Skills are matched through one vocabulary, in both directions.** The posting's requirements
and the applicant's evidence are both run through
:func:`app.knowledge.extractors.extract_skills`, so ``react`` and ``React.js`` are the same
skill on both sides and the overlap is a real fraction rather than a string-equality
coincidence. Counting the intersection against the *posting's* requirement count is what
makes the number answer the question a user is actually asking — "how much of what they want
do I have?" — rather than the flattering inverse.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import structlog

__all__ = [
    "DEFAULT_WEIGHTS",
    "MIN_CORPUS_DOCUMENTS",
    "SIGNAL_RESUME",
    "SIGNAL_SKILLS",
    "SIGNAL_TITLE",
    "SKILLS_FULL_WEIGHT_AT",
    "ApplicantProfile",
    "MatchBreakdown",
    "MatchWeights",
    "build_idf",
    "cosine",
    "match",
    "title_relevance",
    "tokenize",
    "usable_target",
    "usable_targets",
    "weight",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Tokenisation
# ======================================================================================

#: Token pattern. ``+``, ``#`` and ``.`` are kept *inside* a token because dropping them
#: silently merges distinct technologies: ``c++`` becomes ``c``, ``c#`` becomes ``c``, and
#: ``.net`` becomes ``net``. A résumé that says C and a posting that wants C++ would then
#: look like a match on the strongest possible signal.
#:
#: The leading ``\.?`` is what keeps ``.net`` whole. It cannot swallow sentence punctuation,
#: because a dot only joins when a letter or digit follows it immediately — ``ship. Next``
#: has a space and tokenises as two words.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\.?[a-z0-9][a-z0-9+#.\-]*")

#: Trailing punctuation to strip once a token has been captured — a sentence-final ``.`` is
#: not part of ``python.``, but the ``.`` in ``.net`` and ``node.js`` is.
_TRAILING_RE: Final[re.Pattern[str]] = re.compile(r"[.\-]+$")

#: Source text for :data:`_STOPWORDS`, kept as prose rather than a list literal because a
#: hundred and fifty quoted strings on one line is not reviewable and this list is meant to
#: be argued with.
_STOPWORD_SOURCE: Final[str] = """
    a about above after again against all am an and any are as at be because been before
    being below between both but by can cannot could did do does doing down during each few
    for from further had has have having he her here hers herself him himself his how i if in
    into is it its itself me more most my myself no nor not of off on once only or other
    ought our ours ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why with would you your yours
    yourself yourselves will shall may might must etc via per within across including include
    includes ability able work working works role position job opportunity candidate
    candidates applicant applicants team teams company companies experience years year
    strong excellent good great new plus preferred required requirements responsibilities
    qualifications skills skill us our we you re ll ve
"""

#: Words carrying no discriminating signal in a résumé or a job posting. Deliberately short:
#: an over-eager stopword list removes domain words (``design``, ``systems``, ``control``)
#: that are exactly what distinguishes one engineering role from another.
_STOPWORDS: Final[frozenset[str]] = frozenset(_STOPWORD_SOURCE.split())

#: Single characters that are real technologies and must survive the length filter.
_SHORT_KEEP: Final[frozenset[str]] = frozenset({"c", "r", "go", "ai", "ml", "qa", "ui", "ux"})

#: Fewest documents a corpus must hold before its document frequencies are worth using. Below
#: this an IDF is noise dressed as a weight — and the two-document case is actively harmful,
#: which is the whole reason this floor exists.
MIN_CORPUS_DOCUMENTS: Final[int] = 20

#: Fewest tokens a document must contribute before it is worth comparing. A posting reduced
#: to four words by an aggressive scraper should report *no* similarity rather than a
#: confident one computed from nothing.
_MIN_TOKENS: Final[int] = 8


def tokenize(text: str | None) -> list[str]:
    """Split *text* into comparable terms.

    Args:
        text: Free-form text, or ``None``.

    Returns:
        Lowercase tokens with stopwords and single characters removed, in order. Duplicates
        are kept — term frequency is the point.
    """
    if not text:
        return []
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        token = _TRAILING_RE.sub("", raw)
        if not token or token in _STOPWORDS:
            continue
        if len(token) < 2 and token not in _SHORT_KEEP:
            continue
        tokens.append(token)
    return tokens


# ======================================================================================
# Vectors
# ======================================================================================


def build_idf(documents: Iterable[str]) -> dict[str, float]:
    """Return smoothed inverse document frequencies over a corpus.

    Args:
        documents: The corpus. Realistically the postings already ingested for this user —
            the population the comparison is being drawn from.

    Returns:
        Term to IDF weight, or an **empty mapping** when the corpus is smaller than
        :data:`MIN_CORPUS_DOCUMENTS`. Empty is the signal to :func:`weight` that it should
        fall back to sublinear TF, which is the correct behaviour on a thin corpus rather
        than a degraded one: see this module's docstring for why a tiny corpus inverts the
        weighting it is supposed to supply.
    """
    frequencies: Counter[str] = Counter()
    total = 0
    for document in documents:
        terms = set(tokenize(document))
        if not terms:
            continue
        total += 1
        frequencies.update(terms)

    if total < MIN_CORPUS_DOCUMENTS:
        logger.debug("similarity.corpus_too_small", documents=total, minimum=MIN_CORPUS_DOCUMENTS)
        return {}

    return {
        term: math.log((1.0 + total) / (1.0 + count)) + 1.0
        for term, count in frequencies.items()
    }


def weight(tokens: Sequence[str], idf: Mapping[str, float] | None = None) -> dict[str, float]:
    """Return the L2-normalised weighted vector for *tokens*.

    Sublinear term frequency (``1 + log tf``) rather than raw counts: a posting that says
    "Python" nine times is not nine times more about Python than one that says it once, and
    raw counts let a single repeated word dominate a whole vector.

    Args:
        tokens: The document's terms, from :func:`tokenize`.
        idf: Inverse document frequencies from :func:`build_idf`, or ``None``/empty for
            corpus-free sublinear-TF weighting.

    Returns:
        Term to weight, L2-normalised so :func:`cosine` is a plain dot product. Empty for an
        empty document.
    """
    counts = Counter(tokens)
    if not counts:
        return {}

    vector = {
        term: (1.0 + math.log(count)) * (idf.get(term, 1.0) if idf else 1.0)
        for term, count in counts.items()
    }
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm <= 0.0:  # pragma: no cover - every weight above is strictly positive
        return {}
    return {term: value / norm for term, value in vector.items()}


def cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Return the cosine similarity of two L2-normalised sparse vectors.

    Args:
        a: The first vector, from :func:`weight`.
        b: The second vector, from :func:`weight`.

    Returns:
        A value in ``[0, 1]``. Zero when either vector is empty — every weight
        :func:`weight` produces is positive, so the dot product cannot be negative.
    """
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    total = sum(value * larger.get(term, 0.0) for term, value in smaller.items())
    return max(0.0, min(1.0, total))


# ======================================================================================
# Titles
# ======================================================================================

#: Seniority words, and how far apart two levels are allowed to be before the titles stop
#: describing the same job. A "Senior Staff Engineer" posting and an "Intern" target share
#: every other token, so token overlap alone rates them a near-perfect match.
_SENIORITY: Final[dict[str, int]] = {
    "intern": 0,
    "internship": 0,
    "co-op": 0,
    "coop": 0,
    "new": 1,
    "grad": 1,
    "graduate": 1,
    "entry": 1,
    "junior": 1,
    "associate": 2,
    "mid": 3,
    "senior": 4,
    "sr": 4,
    "staff": 5,
    "principal": 6,
    "lead": 5,
    "manager": 6,
    "director": 7,
    "head": 7,
    "vp": 8,
    "president": 9,
}

#: Levels apart at which a title match is fully discounted. Two steps — intern against
#: associate, senior against director — is already a different job.
_SENIORITY_TOLERANCE: Final[int] = 2

#: Role words that name the same job in different grammatical forms, folded to one spelling
#: before titles are compared.
#:
#: An explicit table rather than a stemmer, deliberately. "Robotics Software **Engineering**"
#: and "Robotics Software **Engineer**" are the same job and shared no token without this,
#: costing a third of a perfect title match — but the suffix rules that would fix it also
#: turn ``engineer`` into ``engine`` and ``designer`` into ``design``, which is a worse
#: failure than the one being fixed. Nine pairs cover the role words that actually appear in
#: engineering titles, and every one of them is auditable.
_ROLE_SYNONYMS: Final[dict[str, str]] = {
    "engineering": "engineer",
    "development": "developer",
    "dev": "developer",
    "management": "manager",
    "mgr": "manager",
    "programming": "programmer",
    "analytics": "analyst",
    "analysis": "analyst",
    "science": "scientist",
    "design": "designer",
    "architecture": "architect",
    "research": "researcher",
    "administration": "administrator",
    "admin": "administrator",
}


def _fold_role_words(tokens: Iterable[str]) -> set[str]:
    """Return *tokens* with role-word variants folded to one spelling.

    Args:
        tokens: A title's tokens.

    Returns:
        The token set, with :data:`_ROLE_SYNONYMS` applied.
    """
    return {_ROLE_SYNONYMS.get(token, token) for token in tokens}


def usable_target(tokens: Sequence[str]) -> bool:
    """Whether a target title names a *role* and can therefore establish relevance.

    A target made only of seniority words names no work. Measured on the development
    machine, where the user's stated targets are ``intern``, ``internship`` and ``co-op``:
    every posting on every board with "Intern" in its title scored **100% title relevance**,
    so "Human Resources Intern" and "Graphic Design Intern" outranked most engineering roles
    on the signal specifically meant to separate them.

    A target like that is a genuine preference — it belongs in the search query and in the
    rule pack, both of which already honour it. It simply cannot answer "does this title
    describe work this person does", and pretending it can is worse than admitting it
    cannot: :func:`match` redistributes the weight rather than scoring the signal zero.

    Args:
        tokens: The target's tokens, from :func:`tokenize`.

    Returns:
        ``True`` when at least one token names something other than a level.
    """
    return any(token not in _SENIORITY for token in tokens)


def usable_targets(targets: Sequence[str]) -> tuple[str, ...]:
    """Return the subset of *targets* that name a role.

    Args:
        targets: Candidate target titles.

    Returns:
        Those :func:`usable_target` accepts, in order.
    """
    return tuple(target for target in targets if usable_target(tokenize(target)))


def _seniority(tokens: Iterable[str]) -> int | None:
    """Return the seniority level a title states, or ``None`` when it states none.

    Args:
        tokens: The title's tokens.

    Returns:
        The lowest level named, so "Senior Staff" reads as senior rather than staff, and an
        unlabelled title reads as ``None`` rather than as mid — absence is not a claim.
    """
    levels = [_SENIORITY[token] for token in tokens if token in _SENIORITY]
    return min(levels) if levels else None


def title_relevance(posting_title: str | None, targets: Sequence[str]) -> float:
    """Return how well a posting's title matches the roles this applicant wants.

    Args:
        posting_title: The posting's title.
        targets: The applicant's target titles, in preference order.

    Returns:
        The best match across *targets*, in ``[0, 1]``. ``0.0`` when either side is empty:
        an applicant who has named no target role has not said this posting is relevant, and
        guessing that it is would be the fabrication golden rule #7 forbids.
    """
    posting_tokens = tokenize(posting_title)
    if not posting_tokens or not targets:
        return 0.0

    posting_terms = _fold_role_words(posting_tokens)
    posting_level = _seniority(posting_tokens)
    best = 0.0

    for target in targets:
        target_tokens = tokenize(target)
        if not usable_target(target_tokens):
            continue
        target_terms = _fold_role_words(target_tokens)

        # Overlap against the *target*, not the union: a posting title padded with a
        # location and a requisition number should not be penalised for the padding.
        shared = posting_terms & target_terms
        if not shared:
            continue
        score = len(shared) / len(target_terms)

        target_level = _seniority(target_tokens)
        if posting_level is not None and target_level is not None:
            distance = abs(posting_level - target_level)
            if distance >= _SENIORITY_TOLERANCE:
                score = 0.0
            elif distance:
                score *= 0.5

        best = max(best, score)

    return max(0.0, min(1.0, best))


# ======================================================================================
# The applicant, and the match
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ApplicantProfile:
    """One applicant, flattened into everything matching needs and nothing more.

    Built once per user per scoring batch: the vector is the expensive part, and rebuilding
    it per posting would dominate the cost of scoring two hundred of them.

    Attributes:
        vector: The L2-normalised weighted vector of the applicant's evidence.
        skills: Canonical skill names, from
            :func:`app.knowledge.extractors.extract_skills` — the same vocabulary the
            posting side is run through, which is what makes the overlap meaningful.
        titles: Target titles, in preference order.
        tokens: How many tokens the evidence contributed. Below :data:`_MIN_TOKENS` the
            profile is too thin to compare and every similarity reads zero.
    """

    vector: Mapping[str, float]
    skills: frozenset[str]
    titles: tuple[str, ...]
    tokens: int

    @classmethod
    def build(
        cls,
        evidence: Iterable[str],
        *,
        titles: Sequence[str] = (),
        skills: Iterable[str] = (),
        idf: Mapping[str, float] | None = None,
    ) -> ApplicantProfile:
        """Flatten an applicant's evidence into a comparable profile.

        Args:
            evidence: The applicant's own text — knowledge fact bodies, which is where this
                system keeps the truth about a person (golden rule #6). Passing résumé prose
                works identically; the facts are simply the version that cannot drift.
            titles: Target titles, in preference order.
            skills: Canonical skill names already known for this applicant. Anything the
                evidence mentions is added, so a caller may pass nothing.
            idf: Corpus weights from :func:`build_idf`, or ``None``.

        Returns:
            The profile.
        """
        from app.knowledge.extractors import extract_skills

        body = "\n".join(part for part in evidence if part)
        tokens = tokenize(body)
        known = {name for name in skills if name}
        known.update(extract_skills(body))

        return cls(
            vector=weight(tokens, idf),
            skills=frozenset(known),
            titles=tuple(title for title in titles if title),
            tokens=len(tokens),
        )

    @property
    def is_comparable(self) -> bool:
        """Whether there is enough evidence here to produce an honest similarity."""
        return self.tokens >= _MIN_TOKENS


@dataclass(frozen=True, slots=True)
class MatchWeights:
    """How the three signals blend into the combined score.

    Résumé similarity carries the smallest share, and that is a measurement rather than a
    preference. Scored against 400 real postings with a 26,000-term corpus, an applicant's
    whole indexed career overlaps any single posting by roughly 3-12% in weighted cosine —
    the two documents are simply not the same kind of text. It orders postings correctly
    inside that band, which makes it a good tiebreaker and a poor driver. Weighting it like
    the other two would mean the signal with the least dynamic range moved the headline
    number the most.

    Attributes:
        title: Weight on title relevance.
        skills: Weight on skills overlap.
        resume: Weight on résumé similarity.
    """

    title: float = 0.40
    skills: float = 0.40
    resume: float = 0.20

    def normalized(self) -> MatchWeights:
        """Return these weights scaled to sum to one.

        Returns:
            The scaled weights, or :data:`DEFAULT_WEIGHTS` when they sum to zero — a blend
            of nothing is not a score, and returning zero for every posting would silently
            disable matching rather than announce that it was misconfigured.
        """
        total = self.title + self.skills + self.resume
        if total <= 0.0:
            return DEFAULT_WEIGHTS
        return MatchWeights(
            title=self.title / total,
            skills=self.skills / total,
            resume=self.resume / total,
        )


#: The shipped blend. Title and skills lead because they are the two signals a human reads
#: first, and because full-text similarity is the one most easily inflated by a long posting
#: that happens to share boilerplate with a long résumé.
DEFAULT_WEIGHTS: Final[MatchWeights] = MatchWeights()

#: How many skills a posting must name before its overlap ratio carries full weight. Below
#: this the signal is weighted proportionally: one skill out of one is a 100% that rests on
#: a single observation, and treating it as equal to ten out of ten is how a posting that
#: mentions Python once outranks one that lists an entire stack.
SKILLS_FULL_WEIGHT_AT: Final[int] = 4

#: Names of the three signals, as reported in :attr:`MatchBreakdown.signals`.
SIGNAL_TITLE: Final[str] = "title"
SIGNAL_SKILLS: Final[str] = "skills"
SIGNAL_RESUME: Final[str] = "resume"


@dataclass(frozen=True, slots=True)
class MatchBreakdown:
    """Why a posting matched, in the shape the UI renders.

    Attributes:
        title_relevance: ``0-1``.
        skills_overlap: ``0-1``.
        resume_similarity: ``0-1``.
        combined: The weighted blend, ``0-1``.
        matched_skills: Skills the posting asks for and the applicant has, sorted.
        missing_skills: Skills the posting asks for and the applicant does not, sorted.
            This is the half a user can act on, which is why it is carried rather than
            derived from a count.
        comparable: Whether both sides had enough text to compare. ``False`` means the
            numbers are zeros because there was nothing to measure, not because the posting
            is a poor fit — a distinction the UI has to be able to draw.
        signals: Which of the three actually went into :attr:`combined`, sorted. A signal
            with no evidence is dropped and its weight redistributed, so a reader has to be
            able to see that a displayed ``0%`` did not drag the total down.
    """

    title_relevance: float
    skills_overlap: float
    resume_similarity: float
    combined: float
    matched_skills: tuple[str, ...]
    missing_skills: tuple[str, ...]
    comparable: bool
    signals: tuple[str, ...] = ()

    def as_percent(self) -> dict[str, int]:
        """Return the four scores as whole percentages, for display.

        Returns:
            ``{"combined", "title_relevance", "skills_overlap", "resume_similarity"}``.
        """
        return {
            "combined": round(self.combined * 100),
            "title_relevance": round(self.title_relevance * 100),
            "skills_overlap": round(self.skills_overlap * 100),
            "resume_similarity": round(self.resume_similarity * 100),
        }

    def to_dict(self) -> dict[str, object]:
        """Return the JSON form persisted on ``JobScore.breakdown``.

        Floats are rounded to four places: the extra digits are noise from a sum of weighted
        ratios, and rounding here is what makes a stored breakdown comparable to a recomputed
        one byte for byte.

        Returns:
            A JSON-ready mapping.
        """
        return {
            "title_relevance": round(self.title_relevance, 4),
            "skills_overlap": round(self.skills_overlap, 4),
            "resume_similarity": round(self.resume_similarity, 4),
            "combined": round(self.combined, 4),
            "matched_skills": list(self.matched_skills),
            "missing_skills": list(self.missing_skills),
            "comparable": self.comparable,
            "signals": list(self.signals),
            "percent": self.as_percent(),
        }


#: The breakdown returned when there is nothing to compare.
_INCOMPARABLE: Final[MatchBreakdown] = MatchBreakdown(
    title_relevance=0.0,
    skills_overlap=0.0,
    resume_similarity=0.0,
    combined=0.0,
    matched_skills=(),
    missing_skills=(),
    comparable=False,
)


def match(
    profile: ApplicantProfile,
    *,
    title: str | None,
    description: str | None,
    requirements: str | None = None,
    weights: MatchWeights = DEFAULT_WEIGHTS,
    idf: Mapping[str, float] | None = None,
) -> MatchBreakdown:
    """Score one posting against one applicant.

    Args:
        profile: The applicant, from :meth:`ApplicantProfile.build`.
        title: The posting's title.
        description: The posting's body.
        requirements: Extra requirement text, when the provider separates it from the body.
        weights: How the three signals blend.
        idf: The same corpus weights the profile was built with. Passing different ones is a
            caller bug: two vectors weighted differently are not comparable, and the cosine
            between them means nothing.

    Returns:
        The breakdown. ``comparable=False`` with zeros throughout when either side is too
        thin to measure — which is different from a confident zero and is reported as such.
    """
    from app.knowledge.extractors import extract_skills

    body = "\n".join(part for part in (title, description, requirements) if part)
    posting_tokens = tokenize(body)

    if not profile.is_comparable or len(posting_tokens) < _MIN_TOKENS:
        logger.debug(
            "similarity.incomparable",
            profile_tokens=profile.tokens,
            posting_tokens=len(posting_tokens),
            minimum=_MIN_TOKENS,
        )
        return _INCOMPARABLE

    wanted = set(extract_skills(body))
    matched = wanted & profile.skills
    missing = wanted - profile.skills
    skills = len(matched) / len(wanted) if wanted else 0.0

    resolved = weights.normalized()
    title_score = title_relevance(title, profile.titles)
    resume_score = cosine(profile.vector, weight(posting_tokens, idf))

    # **Only signals with evidence are averaged.** A signal that could not be measured is
    # dropped and its weight redistributed, rather than blended in as a zero it never earned.
    # Both cases are real and both were observed: a posting written in prose names no
    # vocabulary skill, and an applicant whose stated targets are all seniority words
    # ("intern", "co-op") has named no role for a title to be relevant *to*. Scoring either
    # as zero pushes a whole class of postings down for something neither party did wrong.
    #
    # Résumé similarity is always measurable past the guard above, so the blend can never be
    # empty.
    signals: list[tuple[float, float, str]] = [(resolved.resume, resume_score, SIGNAL_RESUME)]
    if usable_targets(profile.titles) and title:
        signals.append((resolved.title, title_score, SIGNAL_TITLE))
    if wanted:
        # Weighted by how much evidence the ratio rests on. A posting naming one recognised
        # skill that this applicant happens to have is 1/1 — a perfect 100% that says almost
        # nothing — and on real data that put "Marketing Operations Systems" and "Technical
        # Content Writer" level with "Backend Engineer" at the top of an engineer's feed.
        # The displayed ratio stays honest; what shrinks is how much it is allowed to move
        # the headline number.
        confidence = min(len(wanted) / SKILLS_FULL_WEIGHT_AT, 1.0)
        signals.append((resolved.skills * confidence, skills, SIGNAL_SKILLS))

    total_weight = sum(component for component, _value, _name in signals)
    combined = (
        sum(component * value for component, value, _name in signals) / total_weight
        if total_weight > 0
        else 0.0
    )

    return MatchBreakdown(
        title_relevance=title_score,
        skills_overlap=skills,
        resume_similarity=resume_score,
        combined=max(0.0, min(1.0, combined)),
        matched_skills=tuple(sorted(matched)),
        missing_skills=tuple(sorted(missing)),
        comparable=True,
        signals=tuple(sorted(name for _component, _value, name in signals)),
    )
