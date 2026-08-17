"""Job scoring — the number that decides whether ApplicantOS applies to a posting.

``docs/CONTRACTS.md`` §10 splits scoring into two halves that must never be confused, and
this module keeps them structurally apart:

**The rule engine is the score.** :meth:`Scorer.score_rules` is pure, synchronous and
perfectly deterministic. It reads a posting, matches a data-driven rule pack
(:func:`load_rules`, seeded from ``app/config/scoring_rules.yaml``) plus the user's hard
preference gates, and returns a :class:`ScoreResult` whose arithmetic is fully itemised in
:attr:`ScoreResult.components`. No clock, no network, no randomness, no database: the same
posting scored today, tomorrow, and on a colleague's machine produces byte-identical
output. That is not a nicety — the dashboard shows these numbers next to each other and a
score that drifts makes every comparison meaningless.

**The model only comments.** :meth:`Scorer.score` runs the rule engine first and then, when
a language model is available, asks it for a bounded ``±10`` adjustment and one paragraph of
prose. The adjustment is clamped, cached by content hash, and — this is the load-bearing
part — **a matched hard negative pins the verdict to its rule-based value**. Sponsorship
conflicts, blocked companies and blocked industries are policy decisions the user already
made; no amount of model enthusiasm may talk the pipeline into applying anyway. That guard
is written out explicitly in :meth:`Scorer.score` rather than left to emerge from the
arithmetic. Any model failure at all — missing key, rate limit, malformed JSON, budget
exhaustion — logs ``scoring.llm_unavailable`` and returns the rule-based result unchanged.

**Matching is word-boundary aware, and that is harder than it looks.** Job descriptions are
full of technology names that break naive substring search: ``"go"`` must not match inside
``"Google"``, a bare ``"C"`` must not match inside ``"CI/CD"`` or ``"C++"``, ``"R"`` must not
match inside ``"React"`` — while ``"c++"``, ``"c#"``, ``".net"`` and ``"node.js"`` must all
match literally despite being full of regular-expression metacharacters and non-word
characters. :func:`term_pattern` builds a matcher that treats ``+`` and ``#`` as part of a
technology name and escapes everything, which is what makes all six of those cases come out
right at once.

The worked example this pack is tuned around — a remote, new-graduate embedded robotics
firmware role written in C++ that also wants 8+ years of experience and cannot sponsor —
totals exactly ``70`` and yields ``"apply"`` at the default ``min_score`` of 70::

    +40 Embedded systems role  +30 Robotics domain  +25 Firmware engineering
    +15 C++ required  +10 New graduate / entry level  +10 Fully remote
    -20 Requires 8+ years of experience  -40 Visa sponsorship explicitly unavailable
    = 70 -> apply
"""

from __future__ import annotations

import dataclasses
import re
import threading
import unicodedata
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from app.cache import NAMESPACES, get_cache, hash_payload, make_key
from app.config.settings import DEFAULT_SCORING_RULES_PATH

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from collections.abc import Iterable, Mapping, Sequence

    from app.ai.llm import ModelPlugin
    from app.ai.similarity import ApplicantProfile, MatchBreakdown, MatchWeights
    from app.models.user import UserPreferences

__all__ = [
    "COMPONENT_LLM_ADJUSTMENT",
    "COMPONENT_MATCH",
    "DEFAULT_RULES",
    "GATE_BLOCKED_COMPANY",
    "GATE_BLOCKED_INDUSTRY",
    "GATE_DEFENSE_EMPLOYER",
    "GATE_NOT_REMOTE",
    "GATE_SALARY_BELOW_MINIMUM",
    "GATE_SPONSORSHIP_CONFLICT",
    "GATE_STARTUP_TOO_SMALL",
    "HARD_GATE_KEYS",
    "HARD_GATE_PENALTY",
    "MATCH_COMPONENT_POINTS",
    "MAX_LLM_ADJUSTMENT",
    "MAX_NORMALIZED_SCORE",
    "MIN_NORMALIZED_SCORE",
    "REVIEW_BAND",
    "RULE_FIELDS",
    "SPONSORSHIP_REQUIRED_NEGATIONS",
    "SPONSORSHIP_REQUIRED_TERMS",
    "SPONSORSHIP_UNAVAILABLE_TERMS",
    "VERDICT_APPLY",
    "VERDICT_REVIEW",
    "VERDICT_SKIP",
    "ScoreComponent",
    "ScoreResult",
    "ScoreRule",
    "Scorer",
    "ScoringConfigError",
    "Verdict",
    "default_rules",
    "explain",
    "iter_rule_keys",
    "load_rules",
    "matches_term",
    "normalize_company_name",
    "normalize_text",
    "reset_rule_cache",
    "score_posting",
    "term_pattern",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary and tuning constants
# ======================================================================================

#: Routing decision produced by the engine. Mirrors
#: :data:`app.schemas.scoring.ScoreVerdict` exactly; declared here as a plain
#: :class:`~typing.Literal` so this module never has to import the pydantic schema layer.
Verdict = Literal["apply", "review", "skip"]

#: The posting scores at or above the user's floor — the pipeline may proceed to apply.
#: Annotated bare ``Final`` on purpose: that narrows the constant to ``Literal["apply"]``,
#: so a type checker can prove :meth:`Scorer.verdict_for` really does return a
#: :data:`Verdict` rather than an arbitrary string.
VERDICT_APPLY: Final = "apply"

#: The posting is close enough to the floor to be worth a human glance.
VERDICT_REVIEW: Final = "review"

#: The posting is not worth pursuing.
VERDICT_SKIP: Final = "skip"

#: How far below ``prefs.min_score`` a posting may fall and still be routed to human review
#: rather than discarded. Named because it is a product decision, not a magic number: 15
#: points is roughly one strong positive rule, so "missed by one signal" reaches a human
#: while "missed by a mile" does not.
REVIEW_BAND: Final[int] = 15

#: Lower bound of the normalised band. ``ScoreResult.total`` is unclamped — rules may sum to
#: anything, and a policy veto drives it deeply negative — while ``normalized`` is the
#: 0-100 figure stored on ``job_scores.normalized`` and rendered by the desktop app.
MIN_NORMALIZED_SCORE: Final[int] = 0

#: Upper bound of the normalised band.
MAX_NORMALIZED_SCORE: Final[int] = 100

#: Largest adjustment the language model may make in either direction
#: (``docs/CONTRACTS.md`` §10). Anything it returns is clamped to this before use.
MAX_LLM_ADJUSTMENT: Final[int] = 10

#: Points deducted by a matched preference gate. Deliberately far larger than any rule in
#: the pack: a gate encodes a decision the user already made ("never this company", "never
#: without remote"), so no combination of positive rules may out-vote it.
HARD_GATE_PENALTY: Final[int] = -1000

#: Haystacks a rule may match against. ``text`` (and its alias ``all``) is the joined
#: title + description + company + location and is the default choice for most signals;
#: the narrower fields exist so a title-shaped signal cannot be triggered by body text.
RULE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "text",
        "all",
        "title",
        "description",
        "company",
        "location",
        "work_arrangement",
        "employment_type",
    }
)

#: Default haystack for a rule that does not name one, per ``docs/CONTRACTS.md`` §10.
DEFAULT_RULE_FIELD: Final[str] = "description"

#: Field names that both mean "the whole posting".
FIELD_TEXT: Final[str] = "text"
FIELD_ALL: Final[str] = "all"

#: Separator joining the individual fields into the ``text`` haystack. A non-whitespace
#: token on purpose: term patterns join their words with ``\s+``, so this guarantees a
#: multi-word phrase can never be formed accidentally by the end of one field abutting the
#: start of the next (a title ending in "embedded" beside a company named "Systems Inc"
#: must not conjure "embedded systems").
FIELD_JOINER: Final[str] = " | "

# --------------------------------------------------------------------------------------
# Preference gate keys — stable identifiers, referenced by the desktop score panel.
# --------------------------------------------------------------------------------------

#: The employer is on ``UserPreferences.blocked_companies``.
GATE_BLOCKED_COMPANY: Final[str] = "pref_blocked_company"

#: The employer's industry is on ``UserPreferences.blocked_industries``.
GATE_BLOCKED_INDUSTRY: Final[str] = "pref_blocked_industry"

#: The posting's sponsorship posture contradicts ``UserPreferences.require_no_sponsorship``.
GATE_SPONSORSHIP_CONFLICT: Final[str] = "pref_sponsorship_conflict"

#: The advertised compensation is below ``UserPreferences.min_salary``.
GATE_SALARY_BELOW_MINIMUM: Final[str] = "pref_salary_below_minimum"

#: The employer is a defence contractor and ``UserPreferences.exclude_defense`` is set.
GATE_DEFENSE_EMPLOYER: Final[str] = "pref_defense_employer"

#: Headcount is below ``UserPreferences.skip_startups_under``.
GATE_STARTUP_TOO_SMALL: Final[str] = "pref_startup_too_small"

#: The posting is not fully remote and ``UserPreferences.remote_only`` is set.
GATE_NOT_REMOTE: Final[str] = "pref_not_remote"

#: The three gates ``docs/CONTRACTS.md`` §10 names as hard negatives the language model may
#: never overturn. Every gate in this module sets ``hard_negative`` — a gate *is* a policy
#: veto — so this set is the contract's explicit floor rather than the whole list, and
#: :meth:`Scorer.score` asserts against it rather than trusting the flag alone.
HARD_GATE_KEYS: Final[frozenset[str]] = frozenset(
    {GATE_SPONSORSHIP_CONFLICT, GATE_BLOCKED_COMPANY, GATE_BLOCKED_INDUSTRY}
)

#: Synthetic component key carrying the language model's adjustment, so that
#: ``sum(points of matched components) == total`` still holds after the model has spoken.
COMPONENT_LLM_ADJUSTMENT: Final[str] = "llm_adjustment"

#: Component key for the applicant-to-posting match (``docs/SCORING.md``).
COMPONENT_MATCH: Final[str] = "match"

#: Points a perfect match contributes. Chosen to be meaningful beside the rule pack without
#: being able to carry a posting on its own: the pack's own positive rules total well above
#: this, so a posting that matches the applicant perfectly but satisfies no preference still
#: lands short of the apply threshold rather than being waved through on similarity alone.
MATCH_COMPONENT_POINTS: Final[int] = 30

#: Label rendered for that component.
LLM_ADJUSTMENT_LABEL: Final[str] = "Model adjustment"

# --------------------------------------------------------------------------------------
# Sponsorship vocabulary (used by the preference gate, not by the packaged rules)
# --------------------------------------------------------------------------------------

#: Phrases that mean *the employer will not sponsor a work visa*. A user who needs
#: sponsorship (``require_no_sponsorship=False``) is vetoed out of these postings.
SPONSORSHIP_UNAVAILABLE_TERMS: Final[tuple[str, ...]] = (
    "will not sponsor",
    "cannot sponsor",
    "unable to sponsor",
    "do not sponsor",
    "does not sponsor",
    "not able to sponsor",
    "no sponsorship",
    "no visa sponsorship",
    "sponsorship is not available",
    "sponsorship not available",
    "not provide sponsorship",
    "without sponsorship now or in the future",
)

#: Phrases that mean *this role requires the employer to sponsor a visa*. A user who
#: requires that no sponsorship be involved (``require_no_sponsorship=True``, the default)
#: is vetoed out of these postings.
SPONSORSHIP_REQUIRED_TERMS: Final[tuple[str, ...]] = (
    "sponsorship required",
    "sponsorship is required",
    "visa sponsorship required",
    "requires visa sponsorship",
    "will require visa sponsorship",
    "work visa required",
    "work permit required",
)

#: Negations that flip the meaning of a :data:`SPONSORSHIP_REQUIRED_TERMS` hit. "No
#: sponsorship required" contains "sponsorship required" and means the exact opposite, so
#: it is checked first and suppresses the gate.
SPONSORSHIP_REQUIRED_NEGATIONS: Final[tuple[str, ...]] = (
    "no sponsorship required",
    "no visa sponsorship required",
    "sponsorship is not required",
    "not require sponsorship",
    "does not require sponsorship",
    "must not require sponsorship",
    "without sponsorship",
)

# --------------------------------------------------------------------------------------
# Company-name normalisation (mirrors app.models.company.Company.normalize)
# --------------------------------------------------------------------------------------

#: Legal-form tokens stripped from the end of a company name before comparison. Kept in
#: step with :data:`app.models.company.COMPANY_LEGAL_SUFFIXES`; duplicated rather than
#: imported so that scoring — which must stay pure and importable without the ORM — never
#: pulls in SQLAlchemy.
COMPANY_LEGAL_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "ag",
        "co",
        "corp",
        "gmbh",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "plc",
        "pte",
        "pvt",
        "sa",
    }
)

#: Everything that is not an ASCII letter or digit becomes a separator during company
#: normalisation, so ``"Acme, Inc."`` and ``"Acme - Inc"`` collapse identically.
_COMPANY_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Unicode normal form applied before ASCII folding, so ``"Nestlé"`` becomes ``"Nestle"``.
_UNICODE_NORMAL_FORM: Final[Literal["NFKD"]] = "NFKD"

# --------------------------------------------------------------------------------------
# Word-boundary machinery
# --------------------------------------------------------------------------------------

#: Characters that continue an ordinary word. A term ending in one of these may not be
#: followed by another, which is what stops ``"go"`` from matching inside ``"Google"``.
_WORD_CHARS: Final[str] = "0-9A-Za-z_"

#: Characters that continue a *technology name* without being word characters. Treating
#: ``+`` and ``#`` as continuations is what stops a bare ``"C"`` from matching inside
#: ``"C++"`` or ``"C#"`` while still letting ``"c++"`` match ``"C++17"``.
_SYMBOL_CHARS: Final[str] = r"+#"

#: Lookbehind forbidding a word or technology character immediately before a match.
_LEFT_WORD_GUARD: Final[str] = f"(?<![{_WORD_CHARS}{_SYMBOL_CHARS}])"

#: Lookahead forbidding a word or technology character immediately after a match.
_RIGHT_WORD_GUARD: Final[str] = f"(?![{_WORD_CHARS}{_SYMBOL_CHARS}])"

#: Lookbehind used for a term that *starts* with ``+`` or ``#``: only another symbol may
#: not precede it, since ``#`` legitimately follows a letter in ``"C#"``.
_LEFT_SYMBOL_GUARD: Final[str] = f"(?<![{_SYMBOL_CHARS}])"

#: Lookahead used for a term that *ends* with ``+`` or ``#``, so ``"c+"`` cannot match
#: ``"c++"`` while ``"c++"`` still matches ``"C++17"``.
_RIGHT_SYMBOL_GUARD: Final[str] = f"(?![{_SYMBOL_CHARS}])"

#: How many compiled term matchers to keep. The default pack holds a few hundred terms;
#: the ceiling exists only to bound a pathological user-supplied pack.
_TERM_CACHE_SIZE: Final[int] = 8192

#: How many compiled rule regexes to keep.
_REGEX_CACHE_SIZE: Final[int] = 1024

#: How many normalised company names to memoise.
_COMPANY_CACHE_SIZE: Final[int] = 2048

# --------------------------------------------------------------------------------------
# Language-model pass
# --------------------------------------------------------------------------------------

#: Model tier used for the adjustment. Short, structured, high volume — the cheap model.
LLM_TIER: Final = "fast"

#: Sampling temperature for the adjustment. Zero, always: a sampled adjustment would make
#: the visible score jitter between runs, and it is the only temperature at which
#: :class:`app.ai.llm.GuardedModelPlugin` will cache the completion.
LLM_TEMPERATURE: Final[float] = 0.0

#: Completion ceiling for the adjustment call. One integer and one paragraph.
LLM_MAX_TOKENS: Final[int] = 600

#: How much of the description is shown to the model. Enough for the model to judge fit,
#: bounded so a 40-page posting cannot blow the prompt budget.
MAX_PROMPT_DESCRIPTION_CHARS: Final[int] = 6000

#: How much of a model-written rationale is retained. ``job_scores.rationale`` is TEXT, but
#: the score panel shows one paragraph and an essay would simply be truncated on screen.
MAX_RATIONALE_CHARS: Final[int] = 900

#: TTL for a cached model adjustment. ``None`` defers to ``settings.cache_default_ttl``.
LLM_SCORE_CACHE_TTL_SECONDS: Final[int | None] = None

#: Cache key discriminator, so the scoring namespace can hold other kinds of entry later.
LLM_CACHE_KIND: Final[str] = "llm_adjustment"

#: JSON Schema the adjustment call must satisfy.
LLM_ADJUSTMENT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["adjustment", "rationale"],
    "properties": {
        "adjustment": {
            "type": "integer",
            "minimum": -MAX_LLM_ADJUSTMENT,
            "maximum": MAX_LLM_ADJUSTMENT,
            "description": (
                "Signed correction to the rule-based total, from -10 to +10. Use 0 when "
                "the rules already capture the fit."
            ),
        },
        "rationale": {
            "type": "string",
            "description": (
                "One paragraph, addressed to the candidate, explaining the fit and the "
                "adjustment. Reference only facts present in the posting."
            ),
        },
    },
}

#: System prompt for the adjustment call.
LLM_SYSTEM_PROMPT: Final[str] = (
    "You are the review step of an automated job-application pipeline.\n\n"
    "A deterministic rule engine has already scored this posting against the candidate's "
    "stated preferences. That total is authoritative. Your job is to make a small "
    "correction for what keyword rules cannot see — seniority actually implied by the "
    "responsibilities, how central the candidate's domain is to the role, hidden "
    "dealbreakers buried in the body text — and to explain the result in one paragraph.\n\n"
    "Rules you must obey:\n"
    "- The adjustment is an integer between -10 and +10. Nothing larger is available to "
    "you; asking for more will simply be clamped.\n"
    "- Return 0 whenever the rule breakdown already reflects the posting. Most postings "
    "deserve 0.\n"
    "- Judge only what the posting says. Never infer an employer's values, funding, "
    "culture, or intentions from its name.\n"
    "- Components marked HARD NEGATIVE are policy decisions the candidate has already "
    "made. You may not argue against them, and your adjustment cannot overturn them.\n"
    "- The rationale is advisory prose for a human. It is never the source of the number."
)


# ======================================================================================
# Errors
# ======================================================================================


class ScoringConfigError(ValueError):
    """A scoring rule pack is malformed.

    Raised by :func:`load_rules` and by :class:`ScoreRule` construction. The message always
    names the offending rule — by ``key`` when there is one, by position when the key itself
    is what is wrong — because a rule pack is user-editable through
    ``PUT /settings/scoring-rules`` and "invalid YAML" is not a fixable error report.
    """


# ======================================================================================
# Text normalisation and term matching
# ======================================================================================


def normalize_text(value: Any) -> str:
    """Reduce any value to the lowercase, single-spaced form the matcher runs against.

    Enum members contribute their value (so ``WorkArrangement.REMOTE`` becomes ``"remote"``),
    ``None`` becomes the empty string, and every run of whitespace — including the newlines
    and non-breaking spaces that arrive in scraped descriptions — collapses to one space, so
    a multi-word term matches across a line break.

    Args:
        value: Anything a posting field might hold.

    Returns:
        The normalised haystack text.
    """
    if value is None:
        return ""
    raw = value.value if isinstance(value, Enum) else value
    return " ".join(str(raw).lower().split())


def _left_guard(first: str) -> str:
    """Return the lookbehind that anchors the start of a term.

    Args:
        first: The term's first character.

    Returns:
        A lookbehind forbidding a word or technology character before an ordinary term;
        a symbol-only lookbehind for a term starting with ``+`` or ``#``; and nothing at
        all for a term starting with other punctuation (``".net"``, ``", mi"``), which must
        stay matchable directly after a letter.
    """
    if first.isalnum() or first == "_":
        return _LEFT_WORD_GUARD
    if first in _SYMBOL_CHARS:
        return _LEFT_SYMBOL_GUARD
    return ""


def _right_guard(last: str) -> str:
    """Return the lookahead that anchors the end of a term.

    Args:
        last: The term's last character.

    Returns:
        A lookahead forbidding a word or technology character after an ordinary term —
        which is what stops ``"go"`` matching ``"Google"``, ``"R"`` matching ``"React"`` and
        ``"C"`` matching ``"C++"`` — a symbol-only lookahead after a term ending in ``+`` or
        ``#`` so that ``"c++"`` still matches ``"C++17"``, and nothing after other
        punctuation.
    """
    if last.isalnum() or last == "_":
        return _RIGHT_WORD_GUARD
    if last in _SYMBOL_CHARS:
        return _RIGHT_SYMBOL_GUARD
    return ""


def term_pattern(term: str) -> str:
    """Build the boundary-aware regular expression source for one literal term.

    Every character of *term* is escaped, so ``"c++"``, ``"c#"``, ``".net"`` and
    ``"node.js"`` are matched literally rather than interpreted. Internal whitespace becomes
    ``\\s+`` so a phrase survives a line break, and both ends are guarded according to what
    kind of character they are — see :func:`_left_guard` and :func:`_right_guard`.

    Args:
        term: The literal term, as written in the rule pack.

    Returns:
        The pattern source, or ``""`` when *term* is blank.

    Example:
        >>> re.search(term_pattern("go"), "we use google cloud", re.IGNORECASE) is None
        True
        >>> re.search(term_pattern("c++"), "modern c++17", re.IGNORECASE) is not None
        True
    """
    stripped = term.strip()
    if not stripped:
        return ""
    body = r"\s+".join(re.escape(word) for word in stripped.split())
    return f"{_left_guard(stripped[0])}{body}{_right_guard(stripped[-1])}"


@lru_cache(maxsize=_TERM_CACHE_SIZE)
def _term_matcher(term: str) -> re.Pattern[str] | None:
    """Return the compiled matcher for *term*, memoised across postings.

    Args:
        term: The literal term, as written in the rule pack.

    Returns:
        The compiled pattern, or ``None`` when *term* is blank.
    """
    source = term_pattern(term)
    return re.compile(source, re.IGNORECASE) if source else None


@lru_cache(maxsize=_REGEX_CACHE_SIZE)
def _rule_regex(pattern: str) -> re.Pattern[str]:
    """Return the compiled form of a rule's ``regex``, memoised.

    Args:
        pattern: The pattern source from the rule pack.

    Returns:
        The compiled, case-insensitive pattern.

    Raises:
        re.error: If *pattern* is not a valid regular expression.
    """
    return re.compile(pattern, re.IGNORECASE)


def matches_term(haystack: str, term: str) -> bool:
    """Return whether *term* occurs in *haystack* on word boundaries.

    Args:
        haystack: Normalised text (see :func:`normalize_text`).
        term: The literal term to look for.

    Returns:
        ``True`` when the term is present as a standalone token or phrase.
    """
    matcher = _term_matcher(term)
    return bool(matcher and matcher.search(haystack))


def _first_matching_term(haystack: str, terms: Sequence[str]) -> str | None:
    """Return the first term of *terms* present in *haystack*.

    Declaration order is honoured so that the recorded evidence is stable: two runs over the
    same posting always quote the same term.

    Args:
        haystack: Normalised text.
        terms: Literal terms, in pack order.

    Returns:
        The matching term, or ``None``.
    """
    for term in terms:
        if matches_term(haystack, term):
            return term
    return None


def _first_missing_term(haystack: str, terms: Sequence[str]) -> str | None:
    """Return the first term of *terms* absent from *haystack*.

    Args:
        haystack: Normalised text.
        terms: Literal terms, in pack order.

    Returns:
        The missing term, or ``None`` when every term is present.
    """
    for term in terms:
        if not matches_term(haystack, term):
            return term
    return None


@lru_cache(maxsize=_COMPANY_CACHE_SIZE)
def normalize_company_name(name: str) -> str:
    """Collapse a company name to the key block-list comparisons run against.

    Unicode is decomposed and folded to ASCII, the name is lowercased, ``&`` expands to
    ``" and "``, every non-alphanumeric run becomes a single space, and trailing legal-form
    tokens are stripped — so ``"Acme, Inc."``, ``"ACME Incorporated"`` and ``"acme"`` all
    reduce to ``"acme"``. The final token is never stripped, so a company genuinely named
    ``"Limited"`` does not normalise away to nothing.

    Args:
        name: A company name from a posting feed or from the user's block list.

    Returns:
        The comparison key, or ``""`` when *name* has no alphanumeric content.
    """
    if not name:
        return ""
    folded = unicodedata.normalize(_UNICODE_NORMAL_FORM, name)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    expanded = ascii_only.lower().replace("&", " and ")
    tokens = _COMPANY_SEPARATOR_RE.sub(" ", expanded).split()
    while len(tokens) > 1 and tokens[-1] in COMPANY_LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


# ======================================================================================
# The rule
# ======================================================================================


@dataclasses.dataclass(slots=True)
class ScoreRule:
    """One data-driven scoring rule, per ``docs/CONTRACTS.md`` §10.

    A rule fires when **all** of its populated conditions hold, which is the plain
    conjunction documented at the top of ``app/config/scoring_rules.yaml``::

        (any_of is empty OR at least one entry matches)
        AND (all_of is empty OR every entry matches)
        AND (none_of is empty OR no entry matches)
        AND (regex is null OR the pattern matches)

    Every term is matched case-insensitively and on word boundaries — ``"go"`` does not fire
    on ``"Google"`` — with ``+`` and ``#`` treated as part of a technology name so that
    ``"C"`` does not fire on ``"C++"``. See :func:`term_pattern`.

    Attributes:
        key: Stable identifier, unique within a pack. Appears in the stored breakdown, so
            renaming one orphans historical scores.
        label: Human-facing name rendered in the score panel and by :func:`explain`.
        points: Signed contribution added to the total when the rule fires.
        field: Which haystack to match against; one of :data:`RULE_FIELDS`.
        any_of: Fires when at least one of these terms is present.
        all_of: Requires every one of these terms.
        none_of: Suppresses the rule when any of these terms is present.
        regex: Optional additional case-insensitive pattern that must also match.
    """

    key: str
    label: str
    points: int
    field: str = DEFAULT_RULE_FIELD
    any_of: list[str] = dataclasses.field(default_factory=list)
    all_of: list[str] = dataclasses.field(default_factory=list)
    none_of: list[str] = dataclasses.field(default_factory=list)
    regex: str | None = None

    def __post_init__(self) -> None:
        """Normalise and validate the rule at construction time.

        Validation happens here rather than at match time so that a malformed pack fails
        when it is loaded or saved — with the offending key in the message — instead of
        silently scoring every posting wrongly.

        Raises:
            ScoringConfigError: If the key is blank, the points are not an integer, the
                field is not one of :data:`RULE_FIELDS`, a term list holds a non-string or a
                blank string, the regex does not compile, or the rule states no condition at
                all (which would fire on every posting ever discovered).
        """
        self.key = str(self.key).strip()
        if not self.key:
            raise ScoringConfigError("scoring rule is missing a 'key'")

        self.label = str(self.label).strip() or self.key
        self.points = _coerce_points(self.points, self.key)

        self.field = str(self.field).strip().lower() or DEFAULT_RULE_FIELD
        if self.field not in RULE_FIELDS:
            raise ScoringConfigError(
                f"scoring rule {self.key!r} has unknown field {self.field!r}; "
                f"expected one of {', '.join(sorted(RULE_FIELDS))}"
            )

        self.any_of = _coerce_terms(self.any_of, self.key, "any_of")
        self.all_of = _coerce_terms(self.all_of, self.key, "all_of")
        self.none_of = _coerce_terms(self.none_of, self.key, "none_of")

        if self.regex is not None:
            if not isinstance(self.regex, str) or not self.regex.strip():
                raise ScoringConfigError(
                    f"scoring rule {self.key!r} has a 'regex' that is not a pattern string"
                )
            self.regex = self.regex.strip()
            try:
                _rule_regex(self.regex)
            except re.error as exc:
                raise ScoringConfigError(
                    f"scoring rule {self.key!r} has an invalid regex: {exc}"
                ) from exc

        if not (self.any_of or self.all_of or self.none_of or self.regex):
            raise ScoringConfigError(
                f"scoring rule {self.key!r} states no condition; a rule with no "
                "any_of/all_of/none_of/regex would fire on every posting"
            )

    def copy(self) -> ScoreRule:
        """Return an independent copy, sharing no mutable state with this rule.

        Returns:
            A new :class:`ScoreRule` whose term lists are fresh objects, so a caller who
            edits a loaded pack cannot corrupt the cached defaults.
        """
        return ScoreRule(
            key=self.key,
            label=self.label,
            points=self.points,
            field=self.field,
            any_of=list(self.any_of),
            all_of=list(self.all_of),
            none_of=list(self.none_of),
            regex=self.regex,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the rule as plain JSON-native data, in pack order.

        Returns:
            A mapping with every contract field present, suitable for serialising back to
            YAML or returning from ``GET /settings/scoring-rules``.
        """
        return {
            "key": self.key,
            "label": self.label,
            "points": self.points,
            "field": self.field,
            "any_of": list(self.any_of),
            "all_of": list(self.all_of),
            "none_of": list(self.none_of),
            "regex": self.regex,
        }


def _coerce_points(value: Any, key: str) -> int:
    """Return *value* as the signed integer a rule contributes.

    Args:
        value: The raw ``points`` entry.
        key: The owning rule's key, for the error message.

    Returns:
        The integer point value. An integral float (``40.0``) is accepted and narrowed,
        because YAML makes that spelling easy to write by accident.

    Raises:
        ScoringConfigError: If *value* is a bool, a non-number, or a fractional number.
            Fractional points are rejected rather than rounded: silently turning 12.5 into
            12 would make a pack score differently than it reads.
    """
    if isinstance(value, bool):
        raise ScoringConfigError(f"scoring rule {key!r} has non-numeric 'points': {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ScoringConfigError(
        f"scoring rule {key!r} has 'points' that is not a whole number: {value!r}"
    )


def _coerce_terms(value: Any, key: str, name: str) -> list[str]:
    """Return a rule's term list, validated and normalised.

    Args:
        value: The raw list from the pack; ``None`` is accepted as "empty".
        key: The owning rule's key, for the error message.
        name: Which list this is (``any_of`` / ``all_of`` / ``none_of``).

    Returns:
        The terms, stripped of surrounding whitespace, in declaration order.

    Raises:
        ScoringConfigError: If *value* is not a list, or holds a non-string or blank entry.
            A bare string is rejected too — ``any_of: "c++"`` iterates as characters, which
            would quietly match every ``c``, ``+`` in the posting.
    """
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ScoringConfigError(
            f"scoring rule {key!r} has a {name!r} that is not a list: {value!r}"
        )
    terms: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            raise ScoringConfigError(
                f"scoring rule {key!r} has a non-string term in {name!r} at index {index}: "
                f"{entry!r}"
            )
        stripped = entry.strip()
        if not stripped:
            raise ScoringConfigError(
                f"scoring rule {key!r} has a blank term in {name!r} at index {index}"
            )
        terms.append(stripped)
    return terms


# ======================================================================================
# The result
# ======================================================================================


@dataclasses.dataclass(slots=True)
class ScoreComponent:
    """One line of a score's arithmetic — what was checked, and what it contributed.

    Non-matching components are kept, with ``points`` reported as the rule's own value and
    ``matched=False``, so the desktop score panel can show what was *examined* rather than
    only what fired. Only matched components contribute to
    :attr:`ScoreResult.total`; :meth:`contribution` is the authority on that.

    Attributes:
        rule: The key of the rule or preference gate this line came from.
        label: Human-facing name.
        points: The signed contribution the rule carries.
        matched: Whether the condition held.
        evidence: Short, quotable explanation — the term that matched, the term that was
            missing, or the term that blocked the rule.
        hard_negative: Whether this is a policy veto the language model may never overturn.
    """

    rule: str
    label: str
    points: int
    matched: bool
    evidence: str = ""
    hard_negative: bool = False

    def contribution(self) -> int:
        """Return what this component actually adds to the total.

        Returns:
            :attr:`points` when the component matched, otherwise ``0``.
        """
        return self.points if self.matched else 0

    def to_dict(self) -> dict[str, Any]:
        """Return the component in the shape :class:`app.schemas.scoring.ScoreComponentRead`
        expects.

        Returns:
            A JSON-native mapping. ``key`` is the schema's name for :attr:`rule`, and
            ``detail`` is its name for :attr:`evidence`; ``points`` is reported as the
            *contribution*, so summing the field reproduces the total exactly.
        """
        return {
            "key": self.rule,
            "label": self.label,
            "points": self.contribution(),
            "weight": 1.0,
            "matched": self.matched,
            "hard_negative": self.hard_negative,
            "detail": self.evidence or None,
        }


@dataclasses.dataclass(slots=True)
class ScoreResult:
    """A complete scoring decision, with its arithmetic and its reasoning attached.

    Attributes:
        total: Unclamped sum of every matched component. A policy veto drives this deeply
            negative on purpose; it is the figure :attr:`verdict` is derived from.
        normalized: :attr:`total` clipped to ``0``-``100`` for storage and display.
        components: Every rule and gate that was evaluated, in evaluation order.
        verdict: ``"apply"``, ``"review"`` or ``"skip"``.
        rationale: Optional model-written prose. Never the source of the number.
        model_used: The model that wrote :attr:`rationale`; ``None`` for a pure rule score.
        match: How the posting compared to the applicant themselves — title relevance,
            skills overlap and résumé similarity. ``None`` when no
            :class:`~app.ai.similarity.ApplicantProfile` was supplied, which is the case for
            every caller that only wants the rule pack's verdict.
    """

    total: int
    normalized: int
    components: list[ScoreComponent]
    verdict: Verdict
    rationale: str | None = None
    model_used: str | None = None
    match: MatchBreakdown | None = None

    @property
    def matched_components(self) -> list[ScoreComponent]:
        """The components that actually fired, in evaluation order."""
        return [component for component in self.components if component.matched]

    @property
    def hard_negatives(self) -> list[ScoreComponent]:
        """The matched policy vetoes — the components no model adjustment may overturn."""
        return [
            component
            for component in self.components
            if component.matched and component.hard_negative
        ]

    @property
    def has_hard_negative(self) -> bool:
        """Whether any policy veto fired."""
        return any(component.matched and component.hard_negative for component in self.components)

    def component(self, key: str) -> ScoreComponent | None:
        """Return the component produced by rule *key*.

        Args:
            key: A rule or gate key.

        Returns:
            The component, or ``None`` when that rule was not evaluated.
        """
        for candidate in self.components:
            if candidate.rule == key:
                return candidate
        return None

    def explain(self) -> str:
        """Return the human-readable breakdown; see :func:`explain`."""
        return explain(self)

    def to_breakdown(self) -> dict[str, Any]:
        """Return the JSON stored in ``job_scores.breakdown``.

        Returns:
            The full component list plus the constants needed to re-render the decision
            months later — the review band in force and the count of vetoes — so the panel
            never has to re-run the engine to explain an old score.
        """
        breakdown: dict[str, Any] = {
            "components": [component.to_dict() for component in self.components],
            "total": self.total,
            "normalized": self.normalized,
            "verdict": self.verdict,
            "review_band": REVIEW_BAND,
            "hard_negatives": [component.rule for component in self.hard_negatives],
        }
        # Omitted rather than written as null when there was no profile to compare against.
        # A `match` key holding zeros would be indistinguishable from a genuine no-match, and
        # the UI's "Why this job matched" panel would confidently render four zeroes for a
        # posting nothing had actually measured.
        if self.match is not None:
            breakdown["match"] = self.match.to_dict()
        return breakdown


def explain(result: ScoreResult) -> str:
    """Render a score as the one-line breakdown the dashboard shows.

    Args:
        result: The result to render.

    Returns:
        Each matched component as a signed value and its label, then the total and the
        verdict — for example::

            +40 Embedded systems role  +30 Robotics domain  +25 Firmware engineering
            -40 Visa sponsorship explicitly unavailable  = 70 -> apply

        A posting that matched nothing renders ``"no rules matched  = 0 -> skip"``.
    """
    parts = [
        f"{component.contribution():+d} {component.label}"
        for component in result.matched_components
    ]
    head = "  ".join(parts) if parts else "no rules matched"
    return f"{head}  = {result.total} -> {result.verdict}"


# ======================================================================================
# Rule pack loading
# ======================================================================================

#: Top-level key holding the rule list in a pack document.
RULES_DOCUMENT_KEY: Final[str] = "rules"

#: Memoised default pack, loaded from :data:`DEFAULT_SCORING_RULES_PATH` on first use.
_default_rules: list[ScoreRule] | None = None

#: Guards :data:`_default_rules`. The pack is loaded once per process, and Celery runs tasks
#: on a thread pool, so two workers can reach the loader at the same instant.
_default_rules_lock: Final[threading.Lock] = threading.Lock()


def load_rules(path: str | Path | None = None) -> list[ScoreRule]:
    """Load and validate a scoring rule pack from YAML.

    Args:
        path: The pack to read. Defaults to the packaged
            :data:`~app.config.settings.DEFAULT_SCORING_RULES_PATH`
            (``app/config/scoring_rules.yaml``).

    Returns:
        The rules in file order. Order is preserved because it is the order the score
        breakdown is rendered in.

    Raises:
        ScoringConfigError: If the file is missing, is not valid YAML, is not a mapping with
            a ``rules`` list, contains a duplicate key, or holds a rule that fails
            validation. Every message names the offending rule — by key where one exists,
            by position otherwise — because this pack is user-editable.
    """
    import yaml  # Lazy: the app must import without optional dependencies present.

    resolved = Path(path) if path is not None else DEFAULT_SCORING_RULES_PATH
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScoringConfigError(f"cannot read scoring rules from {resolved}: {exc}") from exc

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ScoringConfigError(f"scoring rules in {resolved} are not valid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise ScoringConfigError(
            f"scoring rules in {resolved} must be a mapping with a {RULES_DOCUMENT_KEY!r} key"
        )
    entries = document.get(RULES_DOCUMENT_KEY)
    if not isinstance(entries, list):
        raise ScoringConfigError(
            f"scoring rules in {resolved} must contain a {RULES_DOCUMENT_KEY!r} list"
        )

    rules: list[ScoreRule] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ScoringConfigError(
                f"scoring rule at index {index} in {resolved} is not a mapping: {entry!r}"
            )
        rule = _rule_from_mapping(entry, index=index, source=resolved)
        previous = seen.get(rule.key)
        if previous is not None:
            raise ScoringConfigError(
                f"duplicate scoring rule key {rule.key!r} in {resolved} "
                f"(indexes {previous} and {index})"
            )
        seen[rule.key] = index
        rules.append(rule)

    if not rules:
        raise ScoringConfigError(f"scoring rules in {resolved} contain no rules")

    logger.debug("scoring.rules_loaded", path=str(resolved), rules=len(rules))
    return rules


def _rule_from_mapping(entry: Mapping[str, Any], *, index: int, source: Path) -> ScoreRule:
    """Build one :class:`ScoreRule` from a pack entry.

    Args:
        entry: The mapping read from YAML.
        index: Its position in the pack, used when the entry has no usable key.
        source: The pack's path, quoted in error messages.

    Returns:
        The validated rule.

    Raises:
        ScoringConfigError: If the entry declares unknown keys or fails
            :meth:`ScoreRule.__post_init__` validation.
    """
    raw_key = entry.get("key")
    label_for_errors = raw_key if isinstance(raw_key, str) and raw_key.strip() else f"#{index}"

    allowed = {field.name for field in dataclasses.fields(ScoreRule)}
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ScoringConfigError(
            f"scoring rule {label_for_errors!r} in {source} has unknown key(s): "
            f"{', '.join(unknown)}"
        )
    if "points" not in entry:
        raise ScoringConfigError(
            f"scoring rule {label_for_errors!r} in {source} is missing 'points'"
        )

    # Bound through explicitly-``Any`` locals because that is what they are: unvalidated YAML,
    # including the ``None`` a written-but-empty key produces. :meth:`ScoreRule.__post_init__`
    # is the validator — it turns each of these into the ``list[str]`` the attribute declares,
    # or raises — so the constructor genuinely accepts more than the attribute type says.
    any_of: Any = entry.get("any_of")
    all_of: Any = entry.get("all_of")
    none_of: Any = entry.get("none_of")

    try:
        return ScoreRule(
            key=raw_key if isinstance(raw_key, str) else "",
            label=entry.get("label", "") if isinstance(entry.get("label", ""), str) else "",
            points=entry["points"],
            field=entry.get("field") or DEFAULT_RULE_FIELD,
            any_of=any_of,
            all_of=all_of,
            none_of=none_of,
            regex=entry.get("regex"),
        )
    except ScoringConfigError as exc:
        raise ScoringConfigError(f"{exc} (rule {label_for_errors!r} in {source})") from exc


def default_rules() -> list[ScoreRule]:
    """Return the packaged default rule pack, loaded once and memoised.

    Returns:
        A fresh list of independent copies, so a caller who edits the returned rules — the
        settings screen does exactly that — cannot corrupt the process-wide cache.

    Raises:
        ScoringConfigError: If the packaged pack is missing or malformed.
    """
    global _default_rules
    with _default_rules_lock:
        if _default_rules is None:
            _default_rules = load_rules(DEFAULT_SCORING_RULES_PATH)
        return [rule.copy() for rule in _default_rules]


def reset_rule_cache() -> None:
    """Discard the memoised default pack so the next read re-loads it from disk.

    For tests, and for the settings screen after it writes a new pack.
    """
    global _default_rules
    with _default_rules_lock:
        _default_rules = None


def __getattr__(name: str) -> Any:
    """Resolve :data:`DEFAULT_RULES` lazily, per :pep:`562`.

    ``DEFAULT_RULES`` is part of the frozen contract surface (``docs/CONTRACTS.md`` §10) and
    must be importable as a module attribute — but materialising it at import time would
    read a file and import PyYAML during ``import app.ai.scoring``, which this codebase
    forbids. Resolving it here gives the contract's spelling with none of the cost.

    Args:
        name: The attribute being looked up.

    Returns:
        The default rule pack for ``DEFAULT_RULES``.

    Raises:
        AttributeError: For every other name.
    """
    if name == "DEFAULT_RULES":
        return default_rules()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:  # pragma: no cover - the runtime value comes from __getattr__ above
    #: The packaged default rule pack (``app/config/scoring_rules.yaml``).
    DEFAULT_RULES: list[ScoreRule]


# ======================================================================================
# Posting adaptation
# ======================================================================================


def _read_attribute(obj: Any, name: str, default: Any = None) -> Any:
    """Read one field from a posting-like object without ever touching the database.

    Postings reach the scorer in four shapes: the :class:`~app.models.posting.JobPosting`
    ORM row, the DB-free ``JobPostingDTO``, a provider's ``RawPosting``, and a plain
    mapping in tests. This reads all four.

    For a SQLAlchemy instance only *already loaded* state is consulted, because emitting a
    lazy SELECT would make :meth:`Scorer.score_rules` impure — and inside an async session
    it would raise ``MissingGreenlet`` outright.

    Args:
        obj: The posting, company, or mapping to read.
        name: The attribute or key.
        default: Returned when the field is absent or unreadable.

    Returns:
        The field's value, or *default*.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
        return default if value is None else value
    if hasattr(obj, "_sa_instance_state"):
        state = getattr(obj, "__dict__", {})
        value = state.get(name, default)
        return default if value is None else value
    try:
        value = getattr(obj, name, default)
    except Exception as exc:
        logger.debug("scoring.attribute_unreadable", attribute=name, error=str(exc))
        return default
    return default if value is None else value


def _as_int(value: Any) -> int | None:
    """Return *value* as an integer when it plausibly is one.

    Args:
        value: A salary, headcount, or similar numeric field from a feed.

    Returns:
        The integer, or ``None`` when the value is missing or not numeric. Booleans are
        rejected: ``True`` is not a headcount.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = value.strip().replace(",", "").replace("_", "")
        try:
            return int(float(digits))
        except ValueError:
            return None
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class _PostingView:
    """The scorer's flat, immutable read of a posting.

    Built once per :meth:`Scorer.score_rules` call so every rule matches against the same
    normalised text, and so nothing downstream ever touches the posting object again.
    """

    title: str
    description: str
    company: str
    location: str
    work_arrangement: str
    employment_type: str
    text: str
    company_key: str
    industry: str
    is_defense: bool
    is_startup: bool
    employee_count: int | None
    salary_min: int | None
    salary_max: int | None

    def haystack(self, field: str) -> str:
        """Return the normalised text a rule's ``field`` refers to.

        Args:
            field: One of :data:`RULE_FIELDS`.

        Returns:
            The corresponding haystack. ``all`` is an alias of ``text``; an unrecognised
            field cannot occur because :class:`ScoreRule` validates it at construction, but
            it degrades to the joined text rather than raising mid-score.
        """
        if field in (FIELD_TEXT, FIELD_ALL):
            return self.text
        if field == "title":
            return self.title
        if field == "description":
            return self.description
        if field == "company":
            return self.company
        if field == "location":
            return self.location
        if field == "work_arrangement":
            return self.work_arrangement
        if field == "employment_type":
            return self.employment_type
        return self.text

    def content_fingerprint(self) -> str:
        """Return a stable digest of everything scoring depends on.

        Returns:
            A SHA-256 of the normalised posting content. Used as the cache key for the
            language-model pass, so a posting whose text has not changed is never re-scored
            by the model — and one whose text *has* changed always is.
        """
        return hash_payload(
            [
                self.title,
                self.description,
                self.company,
                self.location,
                self.work_arrangement,
                self.employment_type,
                self.salary_min,
                self.salary_max,
            ]
        )


def _company_of(posting: Any) -> Any:
    """Return the company object or name carried by *posting*.

    Args:
        posting: The posting being scored.

    Returns:
        A ``Company``-like object, a bare name string, or ``None``. ``company_name`` (the
        provider DTO spelling) wins over ``company`` (the ORM relationship) only when the
        latter is absent, so an eager-loaded row keeps its richer metadata.
    """
    company = _read_attribute(posting, "company")
    if company is not None:
        return company
    return _read_attribute(posting, "company_name")


def _build_view(posting: Any) -> _PostingView:
    """Flatten *posting* into the immutable view the rules run against.

    Args:
        posting: A :class:`~app.models.posting.JobPosting`, a posting DTO, a provider
            ``RawPosting``, or a mapping with the same field names.

    Returns:
        The normalised view. Missing fields become empty strings rather than raising: a
        posting with no description is a thin posting, not an error.
    """
    company_obj = _company_of(posting)
    has_company_record = company_obj is not None and not isinstance(company_obj, str)
    company_name = (
        _read_attribute(company_obj, "name", "") if has_company_record else (company_obj or "")
    )
    # Employer metadata lives on the Company row when there is one; a posting that carries
    # a bare company *name* (a provider RawPosting, a mapping in a test) may still carry
    # the same fields inline, so that is where they are read from instead.
    metadata_source: Any = company_obj if has_company_record else posting

    title = normalize_text(_read_attribute(posting, "title", ""))
    description = normalize_text(_read_attribute(posting, "description", ""))
    company = normalize_text(company_name)
    location = normalize_text(_read_attribute(posting, "location", ""))

    return _PostingView(
        title=title,
        description=description,
        company=company,
        location=location,
        work_arrangement=normalize_text(_read_attribute(posting, "work_arrangement", "")),
        employment_type=normalize_text(_read_attribute(posting, "employment_type", "")),
        text=FIELD_JOINER.join(part for part in (title, description, company, location) if part),
        company_key=normalize_company_name(company),
        industry=normalize_text(_read_attribute(metadata_source, "industry", "")),
        is_defense=bool(_read_attribute(metadata_source, "is_defense", False)),
        is_startup=bool(_read_attribute(metadata_source, "is_startup", False)),
        employee_count=_as_int(_read_attribute(metadata_source, "employee_count")),
        salary_min=_as_int(_read_attribute(posting, "salary_min")),
        salary_max=_as_int(_read_attribute(posting, "salary_max")),
    )


# ======================================================================================
# The scorer
# ======================================================================================

#: Preference-gate labels, kept beside their keys so the score panel and :func:`explain`
#: agree with the API schema without a second lookup table.
_GATE_LABELS: Final[dict[str, str]] = {
    GATE_BLOCKED_COMPANY: "Blocked company",
    GATE_BLOCKED_INDUSTRY: "Blocked industry",
    GATE_SPONSORSHIP_CONFLICT: "Sponsorship requirement conflict",
    GATE_SALARY_BELOW_MINIMUM: "Salary below minimum",
    GATE_DEFENSE_EMPLOYER: "Defense employer excluded",
    GATE_STARTUP_TOO_SMALL: "Company below size floor",
    GATE_NOT_REMOTE: "Not fully remote",
}

#: The ``work_arrangement`` value meaning fully remote. Mirrors
#: :attr:`app.models.enums.WorkArrangement.REMOTE`; spelled as a string so scoring never
#: imports the ORM enum module.
REMOTE_ARRANGEMENT: Final[str] = "remote"

#: Arrangement values meaning "the posting did not say". Neither may trip the remote-only
#: gate: ``unknown`` is a first-class value in ``WorkArrangement`` precisely because
#: inferring an answer from its absence would discard good postings (golden rule #2).
UNSTATED_ARRANGEMENTS: Final[frozenset[str]] = frozenset({"", "unknown"})


class Scorer:
    """Scores one user's postings against one rule pack.

    Construct once per (user, pack) and reuse: the pack hash and the preference hash are
    computed at construction and reused for every cache key.

    Args:
        rules: The rule pack. ``None`` loads the packaged defaults.
        prefs: The user's :class:`~app.models.user.UserPreferences`.
        llm: An explicit model client for the optional adjustment pass. ``None`` resolves
            :func:`app.ai.llm.get_llm` lazily on first use, which is also what makes the
            zero-API-key path work: it returns the deterministic offline model.
        match_weights: How title relevance, skills overlap and résumé similarity blend into
            the match score. ``None`` uses :data:`app.ai.similarity.DEFAULT_WEIGHTS`.
        idf: Corpus document frequencies from :func:`app.ai.similarity.build_idf`, shared by
            every comparison this scorer makes. ``None`` uses corpus-free sublinear-TF
            weighting, which is what a fresh install has and is reproducible either way.
    """

    def __init__(
        self,
        rules: Sequence[ScoreRule] | None = None,
        prefs: UserPreferences | None = None,
        llm: ModelPlugin | None = None,
        *,
        match_weights: MatchWeights | None = None,
        idf: Mapping[str, float] | None = None,
    ) -> None:
        """Bind a pack, a set of preferences and a match configuration to this scorer."""
        from app.ai.similarity import DEFAULT_WEIGHTS

        self.rules: list[ScoreRule] = list(rules) if rules is not None else default_rules()
        self.prefs: UserPreferences = prefs if prefs is not None else _default_preferences()
        self.match_weights: MatchWeights = (
            match_weights if match_weights is not None else DEFAULT_WEIGHTS
        )
        self._llm = llm
        # The corpus weights every profile and posting in this batch is vectorised with.
        # They must be the *same* weights on both sides: two vectors weighted differently
        # are not comparable, and the cosine between them is a number with no meaning.
        self._idf = idf
        self._rules_hash = hash_payload([rule.to_dict() for rule in self.rules])
        self._prefs_hash = hash_payload(self.prefs)

    # ----------------------------------------------------------------------------------
    # Identity
    # ----------------------------------------------------------------------------------

    @property
    def rules_hash(self) -> str:
        """Content hash of the rule pack, part of every cache key."""
        return self._rules_hash

    @property
    def prefs_hash(self) -> str:
        """Content hash of the user's preferences, part of every cache key."""
        return self._prefs_hash

    @property
    def min_score(self) -> int:
        """The user's apply threshold, read from preferences."""
        return int(getattr(self.prefs, "min_score", 0) or 0)

    def verdict_for(self, total: int) -> Verdict:
        """Return the routing decision for an unclamped *total*.

        Args:
            total: The summed score.

        Returns:
            ``"apply"`` at or above ``prefs.min_score``; ``"review"`` within
            :data:`REVIEW_BAND` points below it; ``"skip"`` otherwise.
        """
        threshold = self.min_score
        if total >= threshold:
            return VERDICT_APPLY
        if total >= threshold - REVIEW_BAND:
            return VERDICT_REVIEW
        return VERDICT_SKIP

    @staticmethod
    def normalize_total(total: int) -> int:
        """Clip an unclamped total into the stored 0-100 band.

        Args:
            total: The summed score.

        Returns:
            *total* clipped to ``[MIN_NORMALIZED_SCORE, MAX_NORMALIZED_SCORE]``.
        """
        return max(MIN_NORMALIZED_SCORE, min(MAX_NORMALIZED_SCORE, total))

    # ----------------------------------------------------------------------------------
    # The deterministic engine
    # ----------------------------------------------------------------------------------

    def score_rules(self, posting: Any, profile: ApplicantProfile | None = None) -> ScoreResult:
        """Score *posting* against the pack, the preference gates and — optionally — a person.

        **This function is pure, synchronous and deterministic.** It reads no clock, no
        network, no database and no random source; it consults only already-loaded ORM
        state; and it returns identical output for identical input, forever. That is a hard
        requirement: the dashboard ranks postings against each other, and a score that
        drifts between runs makes every comparison it shows a lie. Supplying *profile* does
        not weaken that — :mod:`app.ai.similarity` holds the same contract, which is why the
        match is computed there rather than by a model.

        Args:
            posting: A :class:`~app.models.posting.JobPosting`, a posting DTO, a provider
                ``RawPosting``, or a mapping with the same field names.
            profile: The applicant to compare the posting against. ``None`` — the default —
                scores the rule pack alone and produces byte-identical output to before this
                parameter existed.

        Returns:
            The itemised result. Every rule in the pack and every preference gate appears in
            :attr:`ScoreResult.components`, matched or not, so the score panel can show what
            was examined; only matched components contribute to the total.
        """
        return self._score_view(_build_view(posting), profile)

    def _score_view(
        self,
        view: _PostingView,
        profile: ApplicantProfile | None = None,
    ) -> ScoreResult:
        """Run the pack, the gates and the match over an already-flattened posting.

        Split out of :meth:`score_rules` so :meth:`score` can build the view once and use it
        for both the deterministic pass and the model prompt.

        Args:
            view: The flattened posting.
            profile: The applicant, or ``None`` for a rule-only score.

        Returns:
            The itemised rule-based result.
        """
        components = [self._evaluate_rule(rule, view) for rule in self.rules]
        components.extend(self._preference_gates(view))

        breakdown = self._match(view, profile)
        if breakdown is not None:
            components.append(self._match_component(breakdown))

        total = sum(component.contribution() for component in components)
        result = ScoreResult(
            total=total,
            normalized=self.normalize_total(total),
            components=components,
            verdict=self.verdict_for(total),
            match=breakdown,
        )
        logger.debug(
            "scoring.rules_scored",
            total=result.total,
            normalized=result.normalized,
            verdict=result.verdict,
            matched=len(result.matched_components),
            hard_negatives=len(result.hard_negatives),
            match_combined=breakdown.combined if breakdown is not None else None,
        )
        return result

    # ----------------------------------------------------------------------------------
    # Matching the applicant, not just the rules
    # ----------------------------------------------------------------------------------

    def _match(self, view: _PostingView, profile: ApplicantProfile | None) -> MatchBreakdown | None:
        """Compare one posting against the applicant, when there is one to compare against.

        Args:
            view: The flattened posting.
            profile: The applicant, or ``None``.

        Returns:
            The breakdown, or ``None`` when no profile was supplied.
        """
        if profile is None:
            return None

        from app.ai.similarity import match

        return match(
            profile,
            title=view.title,
            description=view.description,
            weights=self.match_weights,
            idf=self._idf,
        )

    def _match_component(self, breakdown: MatchBreakdown) -> ScoreComponent:
        """Turn a match breakdown into one line of the score's arithmetic.

        The combined ratio is scaled by :data:`MATCH_COMPONENT_POINTS` and rounded, so the
        rule pack and the match are denominated in the same units and the panel can show
        them side by side. It is a *component*, not a multiplier on the total: a posting the
        pack vetoes stays vetoed however well it matches, which is what keeps a hard
        negative hard.

        Args:
            breakdown: The computed match.

        Returns:
            The component. Unmatched — contributing nothing — when there was not enough text
            on one side to compare, because a zero earned by absent evidence and a zero
            earned by a genuine mismatch must not read the same.
        """
        percent = breakdown.as_percent()
        if not breakdown.comparable:
            return ScoreComponent(
                rule=COMPONENT_MATCH,
                label="Match to your background",
                points=0,
                matched=False,
                evidence="not enough text on one side to compare",
            )
        return ScoreComponent(
            rule=COMPONENT_MATCH,
            label="Match to your background",
            points=round(breakdown.combined * MATCH_COMPONENT_POINTS),
            matched=True,
            evidence=(
                f"title {percent['title_relevance']}%, "
                f"skills {percent['skills_overlap']}%, "
                f"résumé {percent['resume_similarity']}%"
            ),
        )

    def _evaluate_rule(self, rule: ScoreRule, view: _PostingView) -> ScoreComponent:
        """Evaluate one rule against one posting.

        Conditions are checked in the order the rule pack documents — ``any_of``,
        ``all_of``, ``none_of``, ``regex`` — and the first failure both stops the check and
        becomes the recorded evidence, so a non-matching component explains itself.

        Args:
            rule: The rule to evaluate.
            view: The flattened posting.

        Returns:
            The component, matched or not.
        """
        haystack = view.haystack(rule.field)
        findings: list[str] = []

        if rule.any_of:
            hit = _first_matching_term(haystack, rule.any_of)
            if hit is None:
                return self._component(rule, matched=False, evidence="no listed term present")
            findings.append(f'matched "{hit}"')

        if rule.all_of:
            missing = _first_missing_term(haystack, rule.all_of)
            if missing is not None:
                return self._component(rule, matched=False, evidence=f'missing "{missing}"')
            findings.append("matched all required terms")

        if rule.none_of:
            blocker = _first_matching_term(haystack, rule.none_of)
            if blocker is not None:
                return self._component(rule, matched=False, evidence=f'excluded by "{blocker}"')

        if rule.regex is not None:
            match = _rule_regex(rule.regex).search(haystack)
            if match is None:
                return self._component(rule, matched=False, evidence="pattern did not match")
            findings.append(f'pattern matched "{match.group(0)}"')

        return self._component(rule, matched=True, evidence="; ".join(findings))

    @staticmethod
    def _component(rule: ScoreRule, *, matched: bool, evidence: str) -> ScoreComponent:
        """Build the component for an evaluated rule.

        Args:
            rule: The rule that was evaluated.
            matched: Whether it fired.
            evidence: The quotable explanation.

        Returns:
            The component. Pack rules are never hard negatives — a veto comes from the
            user's preferences, not from a keyword — so ``hard_negative`` stays ``False``
            here and is set only by :meth:`_preference_gates`.
        """
        return ScoreComponent(
            rule=rule.key,
            label=rule.label,
            points=rule.points,
            matched=matched,
            evidence=evidence,
        )

    # ----------------------------------------------------------------------------------
    # Preference gates
    # ----------------------------------------------------------------------------------

    def _preference_gates(self, view: _PostingView) -> list[ScoreComponent]:
        """Evaluate every hard preference gate against the posting.

        A gate encodes a decision the user has already made, so each carries
        :data:`HARD_GATE_PENALTY` — far more than any rule can offset — and is flagged
        ``hard_negative``, which is what pins the verdict in :meth:`score`.

        Gates only ever fire on evidence. A posting with no advertised salary never trips
        the salary floor, and a company with no known headcount never trips the size floor:
        golden rule #2 is "never guess", and an unknown is not a violation.

        Args:
            view: The flattened posting.

        Returns:
            One component per gate, in a fixed order, matched or not.
        """
        return [
            self._gate_blocked_company(view),
            self._gate_blocked_industry(view),
            self._gate_sponsorship(view),
            self._gate_salary(view),
            self._gate_defense(view),
            self._gate_startup_size(view),
            self._gate_remote(view),
        ]

    @staticmethod
    def _gate(key: str, *, matched: bool, evidence: str) -> ScoreComponent:
        """Build a preference-gate component.

        Args:
            key: One of the ``GATE_*`` constants.
            matched: Whether the gate fired.
            evidence: The quotable explanation.

        Returns:
            The component, always flagged as a hard negative.
        """
        return ScoreComponent(
            rule=key,
            label=_GATE_LABELS[key],
            points=HARD_GATE_PENALTY,
            matched=matched,
            evidence=evidence,
            hard_negative=True,
        )

    def _gate_blocked_company(self, view: _PostingView) -> ScoreComponent:
        """Veto a posting from a company on the user's block list.

        Both sides are normalised (``"Acme, Inc."`` and ``"ACME Incorporated"`` collapse to
        the same key), and a block-list entry also matches as a whole word inside a longer
        name, so blocking ``"Meta"`` catches ``"Meta Platforms"`` without catching
        ``"Metabase"``.

        Args:
            view: The flattened posting.

        Returns:
            The gate component.
        """
        blocked = _string_list(self.prefs, "blocked_companies")
        if not blocked or not view.company_key:
            return self._gate(GATE_BLOCKED_COMPANY, matched=False, evidence="")
        for entry in blocked:
            key = normalize_company_name(entry)
            if not key:
                continue
            if key == view.company_key or matches_term(view.company_key, key):
                return self._gate(
                    GATE_BLOCKED_COMPANY,
                    matched=True,
                    evidence=f'"{view.company}" is on the blocked-company list as "{entry}"',
                )
        return self._gate(GATE_BLOCKED_COMPANY, matched=False, evidence="")

    def _gate_blocked_industry(self, view: _PostingView) -> ScoreComponent:
        """Veto a posting whose employer's industry is on the user's block list.

        Matching is against the enriched ``companies.industry`` label only, never against
        the description: a posting that merely *mentions* an industry is not in it.

        Args:
            view: The flattened posting.

        Returns:
            The gate component.
        """
        blocked = _string_list(self.prefs, "blocked_industries")
        if not blocked or not view.industry:
            return self._gate(GATE_BLOCKED_INDUSTRY, matched=False, evidence="")
        for entry in blocked:
            if matches_term(view.industry, normalize_text(entry)):
                return self._gate(
                    GATE_BLOCKED_INDUSTRY,
                    matched=True,
                    evidence=f'industry "{view.industry}" is blocked as "{entry}"',
                )
        return self._gate(GATE_BLOCKED_INDUSTRY, matched=False, evidence="")

    def _gate_sponsorship(self, view: _PostingView) -> ScoreComponent:
        """Veto a posting whose visa posture contradicts the user's.

        Two symmetrical cases, and which one applies depends entirely on
        ``prefs.require_no_sponsorship``:

        * The user requires that no sponsorship be involved (the default). A posting that
          *demands* sponsorship — "visa sponsorship required" — is a mismatch. The negation
          list is checked first, because "no sponsorship required" contains "sponsorship
          required" and means precisely the opposite.
        * The user needs sponsorship (``require_no_sponsorship=False``). A posting that
          refuses to sponsor is a mismatch, and this is the case that matters in practice.

        Note that a posting merely *stating* it will not sponsor is not a veto for a
        candidate who needs no sponsorship — the packaged ``sponsorship_unavailable`` rule
        applies its ordinary penalty there, and that is exactly what the product's worked
        example relies on.

        Args:
            view: The flattened posting.

        Returns:
            The gate component.
        """
        requires_none = bool(getattr(self.prefs, "require_no_sponsorship", True))
        if requires_none:
            negation = _first_matching_term(view.text, SPONSORSHIP_REQUIRED_NEGATIONS)
            if negation is None:
                demanded = _first_matching_term(view.text, SPONSORSHIP_REQUIRED_TERMS)
                if demanded is not None:
                    return self._gate(
                        GATE_SPONSORSHIP_CONFLICT,
                        matched=True,
                        evidence=(
                            f'posting states "{demanded}" but the candidate requires a role '
                            "needing no sponsorship"
                        ),
                    )
            return self._gate(GATE_SPONSORSHIP_CONFLICT, matched=False, evidence="")

        refused = _first_matching_term(view.text, SPONSORSHIP_UNAVAILABLE_TERMS)
        if refused is not None:
            return self._gate(
                GATE_SPONSORSHIP_CONFLICT,
                matched=True,
                evidence=(f'posting states "{refused}" but the candidate needs visa sponsorship'),
            )
        return self._gate(GATE_SPONSORSHIP_CONFLICT, matched=False, evidence="")

    def _gate_salary(self, view: _PostingView) -> ScoreComponent:
        """Veto a posting advertising less than ``prefs.min_salary``.

        The top of the advertised range is what is compared: a posting offering
        ``90,000-140,000`` clears a ``120,000`` floor, because the range is negotiable and
        the ceiling is what the employer has said it will pay.

        Args:
            view: The flattened posting.

        Returns:
            The gate component. Never fires on an unadvertised salary.
        """
        floor = _as_int(getattr(self.prefs, "min_salary", None))
        advertised = view.salary_max if view.salary_max is not None else view.salary_min
        if floor is None or advertised is None:
            return self._gate(GATE_SALARY_BELOW_MINIMUM, matched=False, evidence="")
        if advertised < floor:
            return self._gate(
                GATE_SALARY_BELOW_MINIMUM,
                matched=True,
                evidence=f"advertised up to {advertised} against a floor of {floor}",
            )
        return self._gate(GATE_SALARY_BELOW_MINIMUM, matched=False, evidence="")

    def _gate_defense(self, view: _PostingView) -> ScoreComponent:
        """Veto a defence contractor when ``prefs.exclude_defense`` is set.

        Driven by the enriched ``companies.is_defense`` flag rather than by keywords; the
        packaged ``defense_contractor`` rule covers the textual signal separately, and at an
        ordinary penalty rather than a veto.

        Args:
            view: The flattened posting.

        Returns:
            The gate component.
        """
        if not bool(getattr(self.prefs, "exclude_defense", False)) or not view.is_defense:
            return self._gate(GATE_DEFENSE_EMPLOYER, matched=False, evidence="")
        return self._gate(
            GATE_DEFENSE_EMPLOYER,
            matched=True,
            evidence=f'"{view.company}" is flagged as a defense employer',
        )

    def _gate_startup_size(self, view: _PostingView) -> ScoreComponent:
        """Veto an employer below ``prefs.skip_startups_under`` headcount.

        Args:
            view: The flattened posting.

        Returns:
            The gate component. Never fires on an unknown headcount.
        """
        floor = _as_int(getattr(self.prefs, "skip_startups_under", None))
        if floor is None or view.employee_count is None or view.employee_count >= floor:
            return self._gate(GATE_STARTUP_TOO_SMALL, matched=False, evidence="")
        stage = "startup" if view.is_startup else "employer"
        return self._gate(
            GATE_STARTUP_TOO_SMALL,
            matched=True,
            evidence=f"{stage} has {view.employee_count} employees, below the floor of {floor}",
        )

    def _gate_remote(self, view: _PostingView) -> ScoreComponent:
        """Veto a non-remote posting when ``prefs.remote_only`` is set.

        An ``unknown`` or absent arrangement does not fire: the provider simply did not say,
        and guessing would silently discard good postings.

        Args:
            view: The flattened posting.

        Returns:
            The gate component.
        """
        if not bool(getattr(self.prefs, "remote_only", False)):
            return self._gate(GATE_NOT_REMOTE, matched=False, evidence="")
        arrangement = view.work_arrangement
        if arrangement in UNSTATED_ARRANGEMENTS or arrangement == REMOTE_ARRANGEMENT:
            return self._gate(GATE_NOT_REMOTE, matched=False, evidence="")
        return self._gate(
            GATE_NOT_REMOTE,
            matched=True,
            evidence=f'work arrangement is "{arrangement}" but only remote roles are wanted',
        )

    # ----------------------------------------------------------------------------------
    # The optional language-model pass
    # ----------------------------------------------------------------------------------

    async def score(
        self,
        posting: Any,
        *,
        use_llm: bool = True,
        profile: ApplicantProfile | None = None,
    ) -> ScoreResult:
        """Score *posting*, optionally letting a model nudge the total by ``±10``.

        The rule engine runs first and its result is the floor of what this returns: on any
        model failure whatsoever — no API key, rate limit, timeout, exhausted token budget,
        unparseable reply — the rule-based result is returned unchanged and
        ``scoring.llm_unavailable`` is logged. This method never raises because of the model.

        Args:
            posting: The posting to score.
            use_llm: Set ``False`` to skip the model entirely and return the rule score.
            profile: The applicant to compare the posting against, passed through to
                :meth:`score_rules`. The model pass never sees it and never adjusts it — the
                match is deterministic by construction and stays that way.

        Returns:
            The result, with the model's adjustment recorded as its own component (so the
            components still sum to :attr:`ScoreResult.total`) and its prose in
            :attr:`ScoreResult.rationale`.
        """
        view = _build_view(posting)
        base = self._score_view(view, profile)
        if not use_llm:
            return base

        try:
            payload = await self._llm_adjustment(view, base)
        except Exception as exc:
            logger.warning(
                "scoring.llm_unavailable",
                error=str(exc),
                error_type=type(exc).__name__,
                total=base.total,
                verdict=base.verdict,
            )
            return base
        if payload is None:
            return base

        adjustment = _clamp_adjustment(payload.get("adjustment"))
        rationale = _clean_rationale(payload.get("rationale"))
        model_used = str(payload.get("model") or "") or None

        components = [
            *base.components,
            ScoreComponent(
                rule=COMPONENT_LLM_ADJUSTMENT,
                label=LLM_ADJUSTMENT_LABEL,
                points=adjustment,
                matched=adjustment != 0,
                evidence=f"model {model_used or 'unknown'} adjusted by {adjustment:+d}",
            ),
        ]
        total = base.total + adjustment
        model_verdict = self.verdict_for(total)

        # HARD GUARD (docs/CONTRACTS.md §10). A matched hard negative — sponsorship
        # conflict, blocked company, blocked industry — is a policy decision the user
        # already made. The model may still move the number and write prose, but the
        # verdict is pinned to the rule-based decision so it can never argue the pipeline
        # into applying to a posting the user has vetoed. This is written out here, rather
        # than left to emerge from the size of HARD_GATE_PENALTY, because an emergent
        # safeguard is one pack retune away from silently disappearing.
        locked = base.has_hard_negative
        verdict: Verdict = base.verdict if locked else model_verdict
        if locked and model_verdict != base.verdict:
            logger.warning(
                "scoring.llm_verdict_locked",
                requested_verdict=model_verdict,
                verdict=base.verdict,
                adjustment=adjustment,
                hard_negatives=[component.rule for component in base.hard_negatives],
            )

        return ScoreResult(
            total=total,
            normalized=self.normalize_total(total),
            components=components,
            verdict=verdict,
            rationale=rationale,
            model_used=model_used,
            # Carried through unchanged. The model may move the total; it may not touch how
            # the posting compared to the applicant, because that number's whole value is
            # that it is reproducible.
            match=base.match,
        )

    async def _llm_adjustment(self, view: _PostingView, base: ScoreResult) -> dict[str, Any] | None:
        """Ask the model for an adjustment and a rationale, through the cache.

        The cache key is the content hash of the posting, the preference hash and the rule
        pack hash (``docs/CONTRACTS.md`` §7), so re-scoring an unchanged posting for an
        unchanged user against an unchanged pack costs nothing, while any edit to any of the
        three invalidates precisely and only the affected entries.

        Args:
            view: The flattened posting.
            base: The rule-based result, quoted to the model as authoritative.

        Returns:
            The model's ``{"adjustment", "rationale", "model"}`` payload, or ``None`` when
            the reply carried nothing usable.

        Raises:
            Exception: Anything the model or cache raises. :meth:`score` is the handler.
        """
        llm = self._llm
        if llm is None:
            from app.ai.llm import (
                get_llm,  # Lazy: avoids an import cycle through app.ai.
            )

            llm = get_llm(LLM_TIER)
            self._llm = llm

        model_name = str(getattr(llm, "model", "") or type(llm).__name__)
        key = make_key(
            NAMESPACES.SCORE,
            LLM_CACHE_KIND,
            view.content_fingerprint(),
            self._prefs_hash,
            self._rules_hash,
            model_name,
        )

        async def factory() -> dict[str, Any]:
            reply = await llm.complete_json(
                system=LLM_SYSTEM_PROMPT,
                prompt=self._build_prompt(view, base),
                schema=LLM_ADJUSTMENT_SCHEMA,
                max_tokens=LLM_MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
            )
            return {
                "adjustment": _clamp_adjustment(reply.get("adjustment")),
                "rationale": _clean_rationale(reply.get("rationale")),
                "model": model_name,
            }

        payload = await get_cache().get_or_set(key, factory, ttl=LLM_SCORE_CACHE_TTL_SECONDS)
        return payload if isinstance(payload, dict) else None

    def _build_prompt(self, view: _PostingView, base: ScoreResult) -> str:
        """Render the user message for the adjustment call.

        Args:
            view: The flattened posting.
            base: The rule-based result.

        Returns:
            The prompt: the candidate's stated policy, the posting, and the full rule
            breakdown with hard negatives called out, so the model corrects the rules rather
            than re-deriving them.
        """
        breakdown = "\n".join(
            f"  {component.contribution():+d} {component.label}"
            f"{' [HARD NEGATIVE]' if component.hard_negative else ''}"
            f"{' - ' + component.evidence if component.evidence else ''}"
            for component in base.matched_components
        )
        description = view.description[:MAX_PROMPT_DESCRIPTION_CHARS]
        truncated = " (truncated)" if len(view.description) > MAX_PROMPT_DESCRIPTION_CHARS else ""
        keywords = ", ".join(_string_list(self.prefs, "preferred_keywords")) or "none stated"
        locations = ", ".join(_string_list(self.prefs, "preferred_locations")) or "none stated"

        return (
            "## Candidate policy\n"
            f"- Apply threshold: {self.min_score}\n"
            f"- Preferred keywords: {keywords}\n"
            f"- Preferred locations: {locations}\n"
            f"- Remote only: {bool(getattr(self.prefs, 'remote_only', False))}\n"
            f"- Requires a role needing no visa sponsorship: "
            f"{bool(getattr(self.prefs, 'require_no_sponsorship', True))}\n\n"
            "## Posting\n"
            f"- Title: {view.title or 'unknown'}\n"
            f"- Company: {view.company or 'unknown'}\n"
            f"- Location: {view.location or 'unknown'}\n"
            f"- Arrangement: {view.work_arrangement or 'unknown'}\n"
            f"- Employment type: {view.employment_type or 'unknown'}\n\n"
            f"### Description{truncated}\n{description or '(no description supplied)'}\n\n"
            "## Rule engine result (authoritative)\n"
            f"{breakdown or '  (no rule matched)'}\n"
            f"  = {base.total} -> {base.verdict}\n\n"
            "Return the adjustment and the rationale."
        )


# ======================================================================================
# Small helpers
# ======================================================================================


def _default_preferences() -> UserPreferences:
    """Return contract-default preferences.

    Imported lazily so this module stays usable — and importable — without the ORM layer.

    Returns:
        A fresh :class:`~app.models.user.UserPreferences`, every field at its safe default.
    """
    from app.models.user import UserPreferences as Preferences

    return Preferences()


def _string_list(prefs: Any, name: str) -> list[str]:
    """Read a list-of-strings preference defensively.

    Args:
        prefs: The preferences object.
        name: The field name.

    Returns:
        The non-blank string entries, in order. A field holding something other than a list
        of strings degrades to an empty list rather than raising: a corrupt preference blob
        must not stop the pipeline scoring (see :attr:`app.models.user.User.prefs`).
    """
    value = getattr(prefs, name, None)
    if not isinstance(value, (list, tuple)):
        return []
    return [entry.strip() for entry in value if isinstance(entry, str) and entry.strip()]


def _clamp_adjustment(value: Any) -> int:
    """Coerce and clamp a model-supplied adjustment.

    Args:
        value: Whatever the model returned — an int, a float, a numeric string, or nonsense.

    Returns:
        An integer in ``[-MAX_LLM_ADJUSTMENT, +MAX_LLM_ADJUSTMENT]``. Anything unreadable
        becomes ``0``: an adjustment that cannot be understood is no adjustment.
    """
    if isinstance(value, bool) or value is None:
        return 0
    try:
        adjustment = round(float(value))
    except (TypeError, ValueError):
        return 0
    return max(-MAX_LLM_ADJUSTMENT, min(MAX_LLM_ADJUSTMENT, adjustment))


def _clean_rationale(value: Any) -> str | None:
    """Normalise a model-supplied rationale.

    Args:
        value: Whatever the model returned.

    Returns:
        A single-paragraph string capped at :data:`MAX_RATIONALE_CHARS`, or ``None`` when
        nothing usable was supplied.
    """
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) <= MAX_RATIONALE_CHARS:
        return text
    return text[: MAX_RATIONALE_CHARS - 1].rstrip() + "…"


def score_posting(
    posting: Any,
    prefs: UserPreferences | None = None,
    rules: Sequence[ScoreRule] | None = None,
) -> ScoreResult:
    """Score one posting with a throwaway scorer — the convenience path for scripts.

    Prefer constructing a :class:`Scorer` when scoring more than one posting: it hashes the
    pack and the preferences once.

    Args:
        posting: The posting to score.
        prefs: The user's preferences; defaults apply when omitted.
        rules: The rule pack; the packaged defaults apply when omitted.

    Returns:
        The deterministic rule-based result.
    """
    return Scorer(rules, prefs).score_rules(posting)


def iter_rule_keys(rules: Iterable[ScoreRule]) -> list[str]:
    """Return the keys of *rules*, in order.

    Args:
        rules: Any iterable of rules.

    Returns:
        The key list — handy for asserting that a stored breakdown still lines up with the
        pack that produced it.
    """
    return [rule.key for rule in rules]
