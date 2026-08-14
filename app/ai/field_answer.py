"""Form answering — turning one discovered input into one answer, or into a question.

Every application form is the same twenty questions in a different order, plus two or three
that are genuinely about this role. This module answers the twenty deterministically from the
user's profile, sends only the two or three to a language model, and refuses the rest.

``docs/CONTRACTS.md`` §10 defines :class:`AnswerPlan` and :class:`FieldAnswerer`; §12 makes
this the resolver behind :class:`app.browser.autofill.AutoFiller`. Golden rule #2 — *never
guess* — is the whole design:

**Confidence is the product, not the answer.** ``settings.min_answer_confidence`` (0.75 by
default) is the line between "typed into the form" and "shown to a human". A field this module
cannot resolve returns confidence ``0.0``, the autofiller routes the application to review, and
a person answers it in ten seconds. That is the cheap failure. A confident wrong answer is
submitted under the user's name and cannot be taken back.

**Resolution order**, per §10 — with §10b's screen ahead of all of it:

0. **The untrusted-text screen.** A form's labels, helper text and option values are served by
   the employer's ATS and are attacker-controlled in exactly the way a job description is.
   :func:`~app.ai.untrusted.sanitize_external_text` scores them first, and a field that reaches
   :attr:`~app.ai.untrusted.InjectionRisk.HIGH` is refused outright at :data:`SOURCE_BLOCKED`.
   This module is the most exposed of §10b's four call sites, because unlike the résumé path
   there is no fact-id validator downstream: whatever it returns is typed into a form.
1. **The explicit answers dictionary** — anything the user or a previous manual review already
   settled. Confidence ``1.0``; nothing overrides it.
2. **:data:`KNOWN_FIELDS`** — a curated map of normalised label fragments to resolvers over
   :class:`~app.jobs.base.UserProfileDTO`. Confidence
   :data:`KNOWN_CONFIDENCE`. This is the path that fills most of most forms, with no model call
   and no network.
3. **:data:`EEO_FIELDS`** — gender, race/ethnicity, disability and veteran status. **These
   never reach the language model, under any phrasing.** They are answered from the profile if
   the user stated a value, otherwise with the form's own "decline to self-identify" option,
   otherwise not at all.
4. **The model**, for genuine free text only — the "why do you want to work here?" box. Its
   self-reported confidence is capped at :data:`MAX_LLM_CONFIDENCE`, because a model's opinion
   of its own reliability is not evidence.
5. **Give up**, at confidence ``0.0``.

**The model path carries the user's memory.** A free-text question is exactly where "last time
you told me my notice period is four weeks" should decide the answer, so
:mod:`app.ai.memory_prompt` retrieves the relevant memories, screens every one of them for
personal data, and injects what survives into the **system** prompt. A memory that carried an
SSN or a date of birth is excluded whole rather than redacted, and only its category is logged.
When the resulting answer clears ``settings.min_answer_confidence`` — that is, when the field
was filled rather than handed to a human — the memories behind it are reinforced.

**Options are binding.** For a ``select`` or ``radio`` field the submitted value must be one of
``field.options``, byte for byte: a browser silently ignores an option that does not exist, the
form fails validation on submit, and the failure looks like a selector bug rather than a
content bug. :meth:`FieldAnswerer.coerce_to_options` fuzzy-matches a resolved value against the
offered ones and drops the plan to confidence ``0.0`` when nothing matches.

Label matching is deliberately blunt. Forms write "First Name *", "First name (required)",
"first_name" and "Legal first name" for the same input, so labels are lowercased, stripped of
punctuation, decoration and the ``(required)`` / ``(optional)`` suffixes, whitespace-collapsed,
and then matched by **substring containment against the longest alias first** — which is what
keeps "first name" from being answered by the alias "name".
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Final

import structlog

from app.ai.memory_prompt import MEMORY_PROMPT_K, MemoryBlock, build_memory_block, reinforce_used
from app.ai.prompts import load_prompt
from app.ai.resume_engine import numbers_in
from app.ai.untrusted import InjectionRisk, contact_allowlist, sanitize_external_text
from app.models.enums import FieldKind, WorkAuthStatus

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    import uuid

    from app.ai.llm import ModelPlugin
    from app.jobs.base import FormField, UserProfileDTO
    from app.knowledge.memory import MemoryStore
    from app.knowledge.retrieval import KnowledgeRetriever

__all__ = [
    "DECLINE_MARKERS",
    "EEO_FIELDS",
    "EXPLICIT_CONFIDENCE",
    "FIELD_ANSWER_SCHEMA",
    "KNOWN_CONFIDENCE",
    "KNOWN_FIELDS",
    "MAX_LLM_CONFIDENCE",
    "MEMORY_PURPOSE",
    "MIN_OPTION_SIMILARITY",
    "NO_ANSWER",
    "SOURCE_BLOCKED",
    "SOURCE_DECLINED",
    "SOURCE_EEO",
    "SOURCE_EXPLICIT",
    "SOURCE_LLM",
    "SOURCE_NONE",
    "SOURCE_PROFILE",
    "AnswerPlan",
    "FieldAnswerer",
    "normalize_label",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

# -- confidences --------------------------------------------------------------------------

#: Confidence for a value the user (or a previous manual review) supplied explicitly. There is
#: nothing more authoritative than the user having already typed the answer.
EXPLICIT_CONFIDENCE: Final[float] = 1.0

#: Confidence for a value read straight out of the profile through :data:`KNOWN_FIELDS`. Just
#: below certainty, because the *label match* is a heuristic even though the *value* is not.
KNOWN_CONFIDENCE: Final[float] = 0.95

#: Ceiling on a model's self-reported confidence. A model asked how sure it is answers
#: fluently, not calibratedly, so its number is treated as an upper bound on a bounded scale
#: rather than as a measurement. Deliberately above ``settings.min_answer_confidence`` so a
#: genuinely well-grounded essay answer can still be submitted.
MAX_LLM_CONFIDENCE: Final[float] = 0.9

#: The confidence of "I do not know", which routes the application to a human.
NO_ANSWER: Final[float] = 0.0

# -- source labels ---------------------------------------------------------------------------
#
# Recorded on :attr:`AnswerPlan.source` and surfaced in the desktop app's review queue, so a
# person resolving a field can see *why* the system answered as it did.

SOURCE_EXPLICIT: Final[str] = "explicit"
SOURCE_PROFILE: Final[str] = "profile"
SOURCE_EEO: Final[str] = "eeo_profile"
SOURCE_DECLINED: Final[str] = "eeo_declined"

#: Sources that identify a plan as answering a demographic question. Option coercion treats
#: these specially: a value that matches no offered option falls back to the form's decline
#: option rather than to a human, because declining is always a valid answer here and the
#: applicant already answered this during onboarding.
_EEO_SOURCES: Final[frozenset[str]] = frozenset({SOURCE_EEO, SOURCE_DECLINED})
SOURCE_LLM: Final[str] = "llm"
SOURCE_NONE: Final[str] = "unanswered"

#: Source recorded when the field's own text was refused by the §10b screen. Distinct from
#: :data:`SOURCE_NONE` because "we could not answer this" and "we refused to read this" send a
#: human to two different places, and because
#: :meth:`app.browser.autofill.AutoFiller.fill` promotes it to
#: :attr:`~app.models.enums.ReviewReason.POLICY_BLOCK`.
SOURCE_BLOCKED: Final[str] = "policy_block"

# -- option matching ---------------------------------------------------------------------------

#: Similarity a resolved value must reach against an offered option before it is submitted as
#: that option. High, because the failure mode is silent: a browser ignores a value that is not
#: in the ``<select>``, the form fails validation, and the log says nothing useful. When in
#: doubt the field goes to a human.
MIN_OPTION_SIMILARITY: Final[float] = 0.82

#: Field kinds whose value must be one of ``field.options``.
CONSTRAINED_KINDS: Final[frozenset[FieldKind]] = frozenset(
    {FieldKind.SELECT, FieldKind.RADIO, FieldKind.MULTISELECT, FieldKind.CHECKBOX}
)

#: Kinds a model may answer when the label reads as a question. Plain text plus the
#: constrained controls — a yes/no radio asking about the applicant's experience is a
#: question about evidence, and the answer is still forced back onto the offered options by
#: :meth:`FieldAnswerer.coerce_to_options`. Demographic questions never reach here.
_MODEL_ANSWERABLE_KINDS: Final[frozenset[FieldKind]] = frozenset(
    {FieldKind.TEXT, FieldKind.SELECT, FieldKind.RADIO, FieldKind.MULTISELECT}
)

#: Affirmative and negative answers, and the option texts that mean them. Yes/no questions are
#: the most common constrained fields on a form and rarely spell the words the same way twice.
_AFFIRMATIVE: Final[tuple[str, ...]] = ("yes", "y", "true", "i am", "i do", "affirmative")
_NEGATIVE: Final[tuple[str, ...]] = ("no", "n", "false", "i am not", "i do not", "negative")

#: The canonical answers a yes/no resolver returns, before option coercion.
ANSWER_YES: Final[str] = "Yes"
ANSWER_NO: Final[str] = "No"

# -- EEO --------------------------------------------------------------------------------------

#: Option texts that mean "the applicant chose not to say". Matched as substrings of a
#: normalised option, longest first, so "I don't wish to answer" and "Decline to self-identify"
#: both resolve. This list is the reason an EEO question can be answered honestly without the
#: system ever inferring anything.
DECLINE_MARKERS: Final[tuple[str, ...]] = (
    "decline to self identify",
    "decline to selfidentify",
    "i do not wish to answer",
    "i dont wish to answer",
    "do not wish to answer",
    "dont wish to answer",
    # "want" as well as "wish": CALSTART's Lever form offers "I do not want to answer",
    # which the wish-only list missed, so a question with a perfectly good decline option
    # went to a human instead.
    "i do not want to answer",
    "i dont want to answer",
    "do not want to answer",
    "dont want to answer",
    "i decline to answer",
    "decline to answer",
    "choose not to disclose",
    "prefer not to disclose",
    "prefer not to answer",
    "prefer not to say",
    "not disclosed",
    "decline",
)

#: The value the profile stores when the user actively chose not to self-identify. Only this
#: string, never ``None``, may be written into a form (``docs/CONTRACTS.md`` §9).
DECLINE_VALUE: Final[str] = "decline_to_self_identify"

# -- LLM --------------------------------------------------------------------------------------

#: Words below which a ``text`` field is treated as a data entry box rather than a question,
#: and therefore never sent to a model. A textarea is always treated as free text.
LLM_MIN_QUESTION_WORDS: Final[int] = 4

#: Facts retrieved to ground one free-text answer, when a retriever was supplied.
LLM_KNOWLEDGE_FACTS: Final[int] = 12

#: Label this module's memory injections carry in the logs (:mod:`app.ai.memory_prompt`).
MEMORY_PURPOSE: Final[str] = "field_answer"

#: Characters of each retrieved fact rendered into the prompt.
MAX_FACT_CHARS: Final[int] = 300

#: Temperature for answering. Zero, so the same form produces the same answers and the call is
#: cacheable in :class:`app.ai.llm.GuardedModelPlugin`.
ANSWER_TEMPERATURE: Final[float] = 0.0

#: Completion budget for one free-text answer.
ANSWER_MAX_TOKENS: Final[int] = 768

#: Fallback character ceiling for a free-text answer whose field advertises no ``max_length``.
DEFAULT_ANSWER_CHARS: Final[int] = 1500

# -- label normalisation -------------------------------------------------------------------------

#: Parenthetical decorations stripped from a label before matching.
_LABEL_DECORATIONS: Final[re.Pattern[str]] = re.compile(
    r"\((?:required|optional|mandatory|if applicable)\)", re.IGNORECASE
)

#: Apostrophes, removed outright rather than replaced with a space because they sit *inside*
#: a word: "I don't wish to answer" must normalise to ``"i dont wish to answer"`` and not to
#: ``"i don t wish to answer"``, or the decline-option matcher misses the single most common
#: way an EEO form words its opt-out. The three escapes are the typographic apostrophes web
#: forms actually emit (U+2018, U+2019, U+02BC).
_LABEL_APOSTROPHES: Final[re.Pattern[str]] = re.compile("['‘’ʼ]")

#: Everything that is not a letter, digit or space becomes a space. Underscores are listed
#: explicitly because ``\w`` includes them and ``first_name`` must become ``first name``.
_LABEL_NOISE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9\s]|_")

#: Runs of whitespace, collapsed to a single space.
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Trailing words that mark a label's obligation rather than its subject.
_TRAILING_NOISE: Final[tuple[str, ...]] = ("required", "optional", "mandatory")

#: JSON Schema for a free-text answer. Mirrors the schema block in
#: ``app/ai/prompts/field_answer.system.md``; the two must be edited together.
FIELD_ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["answer", "confidence", "reasoning"],
    "additionalProperties": False,
    "properties": {
        "answer": {
            "type": "string",
            "description": "The answer as it should be typed into the form, or empty to decline.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "How well the supplied material supports the answer.",
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence on what the answer was based on, or why it declined.",
        },
    },
}


# ======================================================================================
# Label normalisation
# ======================================================================================


def normalize_label(label: str) -> str:
    """Reduce a form label to the key :data:`KNOWN_FIELDS` is matched against.

    Forms write the same input a dozen ways — ``"First Name *"``, ``"First name (required)"``,
    ``"first_name"``, ``"Legal First Name:"`` — so matching has to happen after aggressive
    normalisation or the alias list becomes unmaintainable.

    The transformation, in order: case-fold; remove ``(required)`` / ``(optional)`` style
    decorations; delete apostrophes; replace every other non-alphanumeric character
    (including ``_`` and ``*``) with a space; collapse whitespace; drop a trailing obligation
    word.

    Examples:
        ``"First Name *"`` → ``"first name"``; ``"E-mail Address"`` → ``"e mail address"``;
        ``"I don't wish to answer"`` → ``"i dont wish to answer"``;
        ``"Are you legally authorized to work in the U.S.?"`` →
        ``"are you legally authorized to work in the u s"``.

    Args:
        label: The label exactly as the browser found it.

    Returns:
        The normalised key, possibly empty.
    """
    lowered = (label or "").casefold()
    stripped = _LABEL_APOSTROPHES.sub("", _LABEL_DECORATIONS.sub(" ", lowered))
    spaced = _LABEL_NOISE.sub(" ", stripped)
    collapsed = _WHITESPACE.sub(" ", spaced).strip()
    for noise in _TRAILING_NOISE:
        if collapsed.endswith(f" {noise}"):
            collapsed = collapsed[: -len(noise) - 1].strip()
    return collapsed


# ======================================================================================
# Profile resolvers
# ======================================================================================

#: Reads one answer out of a profile. Returns ``None`` when the profile does not state it —
#: which is the honest answer and the one that routes the field to a human.
Resolver = Callable[["UserProfileDTO"], "str | None"]


def _text(value: Any) -> str | None:
    """Return *value* as a trimmed string, or ``None`` when it carries no content."""
    if value is None:
        return None
    rendered = value if isinstance(value, str) else str(value)
    trimmed = rendered.strip()
    return trimmed or None


def _name_part(user: UserProfileDTO, index: int) -> str | None:
    """Return one part of the applicant's name.

    Args:
        user: The applicant.
        index: ``0`` for the given name, ``-1`` for the family name.

    Returns:
        The requested part, or ``None`` when the profile has no name — or, for the family
        name, when the profile holds only a single token and splitting it would invent one.
    """
    parts = (user.full_name or "").split()
    if not parts:
        return None
    if index == -1 and len(parts) < 2:
        return None
    return parts[index]


def _address_part(user: UserProfileDTO, *keys: str) -> str | None:
    """Return the first present component of the applicant's postal address.

    Args:
        user: The applicant.
        *keys: Candidate keys, in preference order — forms and importers disagree on whether
            a postcode is ``postal_code``, ``zip`` or ``zipcode``.

    Returns:
        The first non-empty value, or ``None``.
    """
    address = user.address if isinstance(user.address, Mapping) else {}
    for key in keys:
        found = _text(address.get(key))
        if found:
            return found
    return None


def _authorized_to_work(user: UserProfileDTO) -> str | None:
    """Answer "are you legally authorised to work?" from the stated status.

    Args:
        user: The applicant.

    Returns:
        ``"Yes"`` for a citizen, permanent resident or visa holder, ``"No"`` for someone who
        needs sponsorship, and ``None`` when the status is
        :attr:`~app.models.enums.WorkAuthStatus.UNKNOWN` — which is never guessed, because
        answering this one wrong is a false statement on an application.
    """
    status = user.work_authorization
    if status is WorkAuthStatus.UNKNOWN:
        return None
    return ANSWER_NO if status is WorkAuthStatus.NEEDS_SPONSORSHIP else ANSWER_YES


def _requires_sponsorship(user: UserProfileDTO) -> str | None:
    """Answer "will you require sponsorship?" from the stated status.

    Args:
        user: The applicant.

    Returns:
        ``"Yes"`` or ``"No"`` derived from
        :attr:`~app.jobs.base.UserProfileDTO.work_authorization`; ``"Yes"`` when the profile
        asserts :attr:`~app.jobs.base.UserProfileDTO.requires_sponsorship` outright; and
        ``None`` when the status is unknown. The ``False`` default of that boolean is *not*
        treated as a "No": an unset field and a stated "no" are different things, and only one
        of them may be submitted.
    """
    status = user.work_authorization
    if status is not WorkAuthStatus.UNKNOWN:
        return ANSWER_YES if status is WorkAuthStatus.NEEDS_SPONSORSHIP else ANSWER_NO
    return ANSWER_YES if user.requires_sponsorship else None


def _salary_expectation(user: UserProfileDTO) -> str | None:
    """Render the applicant's stated compensation band.

    Args:
        user: The applicant.

    Returns:
        ``"120000"``, ``"120000-150000"`` or ``None``. Rendered without separators or a
        currency symbol because most salary inputs are numeric and reject both; the band's
        currency travels in :attr:`~app.jobs.base.UserProfileDTO.salary_currency` for callers
        that need it.
    """
    low, high = user.salary_min, user.salary_max
    if low and high and high != low:
        return f"{int(low)}-{int(high)}"
    stated = low or high
    return str(int(stated)) if stated else None


def _notice_period(user: UserProfileDTO) -> str | None:
    """Render the applicant's notice period in weeks.

    Args:
        user: The applicant.

    Returns:
        ``"4 weeks"``, ``"1 week"``, ``"None"`` for a zero notice period, or ``None`` when the
        profile does not state one.
    """
    weeks = user.notice_period_weeks
    if weeks is None:
        return None
    if weeks <= 0:
        return "None"
    return f"{int(weeks)} week" + ("s" if weeks != 1 else "")


def _yes_no(value: bool) -> str:
    """Render a boolean as the word a form expects."""
    return ANSWER_YES if value else ANSWER_NO


def _extra(user: UserProfileDTO, *keys: str) -> str | None:
    """Return the first present value from the profile's free-form ``extra`` map.

    ``extra`` is where onboarding and previous manual reviews put answers the schema has no
    column for — years of experience, referral source, how the applicant heard about the
    company.

    Args:
        user: The applicant.
        *keys: Candidate keys, in preference order.

    Returns:
        The first non-empty value, or ``None``.
    """
    extra = user.extra if isinstance(user.extra, Mapping) else {}
    for key in keys:
        found = _text(extra.get(key))
        if found:
            return found
    return None


#: **The deterministic answer map.** Normalised label fragment → resolver over
#: :class:`~app.jobs.base.UserProfileDTO`.
#:
#: Matching is substring containment against the **longest alias first**, which is what makes
#: an unordered mapping safe: ``"first name"`` is tested before ``"name"``, so "Legal First
#: Name" cannot be answered with the applicant's full name. Every key is already in the form
#: :func:`normalize_label` produces — lowercase, alphanumerics and single spaces only — so
#: ``"e mail"`` matches "E-mail" and ``"linkedin"`` matches "LinkedIn URL".
#:
#: This map is the reason most of most forms fill with no model call, no network round trip
#: and no chance of a fabricated answer.
KNOWN_FIELDS: Final[dict[str, Resolver]] = {
    # -- identity -------------------------------------------------------------------------
    "first name": lambda user: _name_part(user, 0),
    "given name": lambda user: _name_part(user, 0),
    "forename": lambda user: _name_part(user, 0),
    "preferred name": lambda user: _name_part(user, 0),
    "last name": lambda user: _name_part(user, -1),
    "family name": lambda user: _name_part(user, -1),
    "surname": lambda user: _name_part(user, -1),
    "full name": lambda user: _text(user.full_name),
    "legal name": lambda user: _text(user.full_name),
    "your name": lambda user: _text(user.full_name),
    "candidate name": lambda user: _text(user.full_name),
    "applicant name": lambda user: _text(user.full_name),
    "name": lambda user: _text(user.full_name),
    "pronouns": lambda user: _text(user.pronouns),
    # -- contact --------------------------------------------------------------------------
    "email address": lambda user: _text(user.email),
    "e mail address": lambda user: _text(user.email),
    "email": lambda user: _text(user.email),
    "e mail": lambda user: _text(user.email),
    "mobile number": lambda user: _text(user.phone),
    "phone number": lambda user: _text(user.phone),
    "telephone": lambda user: _text(user.phone),
    "mobile": lambda user: _text(user.phone),
    "phone": lambda user: _text(user.phone),
    # -- address --------------------------------------------------------------------------
    "street address": lambda user: _address_part(user, "line1", "street", "address1", "address"),
    "address line 1": lambda user: _address_part(user, "line1", "street", "address1", "address"),
    "address line 2": lambda user: _address_part(user, "line2", "address2"),
    "mailing address": lambda user: _address_part(user, "line1", "street", "address1", "address"),
    "postal code": lambda user: _address_part(user, "postal_code", "zip", "zipcode", "postcode"),
    "zip code": lambda user: _address_part(user, "postal_code", "zip", "zipcode", "postcode"),
    "postcode": lambda user: _address_part(user, "postal_code", "zip", "zipcode", "postcode"),
    "zip": lambda user: _address_part(user, "postal_code", "zip", "zipcode", "postcode"),
    "city": lambda user: _address_part(user, "city", "town", "locality"),
    "state or province": lambda user: _address_part(user, "state", "province", "region"),
    "state province": lambda user: _address_part(user, "state", "province", "region"),
    "province": lambda user: _address_part(user, "state", "province", "region"),
    "state": lambda user: _address_part(user, "state", "province", "region"),
    "country": lambda user: _address_part(user, "country"),
    "current location": lambda user: _text(user.location),
    "location": lambda user: _text(user.location),
    "address": lambda user: _address_part(user, "line1", "street", "address1", "address"),
    # -- links ----------------------------------------------------------------------------
    "linkedin profile": lambda user: user.link("linkedin"),
    "linkedin url": lambda user: user.link("linkedin"),
    "linkedin": lambda user: user.link("linkedin"),
    "github profile": lambda user: user.link("github"),
    "github url": lambda user: user.link("github"),
    "github": lambda user: user.link("github"),
    "portfolio url": lambda user: user.link("portfolio") or user.link("website"),
    "portfolio": lambda user: user.link("portfolio") or user.link("website"),
    "personal website": lambda user: user.link("website") or user.link("portfolio"),
    "personal site": lambda user: user.link("website") or user.link("portfolio"),
    "website": lambda user: user.link("website") or user.link("portfolio"),
    # -- work authorisation ---------------------------------------------------------------
    "are you legally authorized to work": _authorized_to_work,
    "are you legally authorised to work": _authorized_to_work,
    "legally authorized to work": _authorized_to_work,
    "legally authorised to work": _authorized_to_work,
    "authorized to work": _authorized_to_work,
    "authorised to work": _authorized_to_work,
    "right to work": _authorized_to_work,
    "work authorization": _authorized_to_work,
    "work authorisation": _authorized_to_work,
    "work eligibility": _authorized_to_work,
    "will you now or in the future require sponsorship": _requires_sponsorship,
    "will you require sponsorship": _requires_sponsorship,
    "do you require sponsorship": _requires_sponsorship,
    "require visa sponsorship": _requires_sponsorship,
    "need sponsorship": _requires_sponsorship,
    "visa sponsorship": _requires_sponsorship,
    "sponsorship": _requires_sponsorship,
    "citizenship": lambda user: _text(user.citizenship),
    "security clearance": lambda user: _text(user.clearance),
    "clearance": lambda user: _text(user.clearance),
    # -- availability and terms -----------------------------------------------------------
    "earliest start date": lambda user: _text(user.start_date_availability),
    "available start date": lambda user: _text(user.start_date_availability),
    "when can you start": lambda user: _text(user.start_date_availability),
    "start date": lambda user: _text(user.start_date_availability),
    "availability": lambda user: _text(user.start_date_availability),
    "notice period": _notice_period,
    "salary expectation": _salary_expectation,
    "expected salary": _salary_expectation,
    "desired salary": _salary_expectation,
    "salary requirement": _salary_expectation,
    "compensation expectation": _salary_expectation,
    "expected compensation": _salary_expectation,
    "desired compensation": _salary_expectation,
    "years of experience": lambda user: _extra(
        user, "years_of_experience", "years_experience", "experience_years"
    ),
    "years experience": lambda user: _extra(
        user, "years_of_experience", "years_experience", "experience_years"
    ),
    # -- preferences ----------------------------------------------------------------------
    "are you willing to relocate": lambda user: _yes_no(user.willing_to_relocate),
    "willing to relocate": lambda user: _yes_no(user.willing_to_relocate),
    "open to relocation": lambda user: _yes_no(user.willing_to_relocate),
    "relocate": lambda user: _yes_no(user.willing_to_relocate),
    "how did you hear about us": lambda user: _extra(
        user, "referral_source", "how_did_you_hear", "source"
    ),
    "how did you hear about this": lambda user: _extra(
        user, "referral_source", "how_did_you_hear", "source"
    ),
    "where did you hear about": lambda user: _extra(
        user, "referral_source", "how_did_you_hear", "source"
    ),
    "referral source": lambda user: _extra(user, "referral_source", "how_did_you_hear", "source"),
    "referred by": lambda user: _extra(user, "referred_by", "referral_name"),
}

#: **The questions this system never answers on its own.** Normalised label fragment →
#: the :class:`~app.jobs.base.UserProfileDTO` attribute holding the user's own stated value.
#:
#: These are resolved from the profile when the user set something, answered with the form's
#: "decline to self-identify" option when they did not, and otherwise left for a human. They
#: are **never** sent to a language model and never inferred from anything — not a name, not a
#: school, not a pronoun (``docs/CONTRACTS.md`` §4, golden rule #2).
EEO_FIELDS: Final[dict[str, str]] = {
    "gender identity": "gender",
    "gender": "gender",
    "sex": "gender",
    "race or ethnicity": "race_ethnicity",
    "race ethnicity": "race_ethnicity",
    "ethnicity": "race_ethnicity",
    "ethnic background": "race_ethnicity",
    "racial": "race_ethnicity",
    "race": "race_ethnicity",
    "hispanic or latino": "race_ethnicity",
    "disability status": "disability_status",
    "disabled": "disability_status",
    "disability": "disability_status",
    "protected veteran": "veteran_status",
    "veteran status": "veteran_status",
    "veteran": "veteran_status",
    "military service": "veteran_status",
}

#: Aliases sorted longest-first, computed once. Substring containment means a shorter alias
#: can shadow a longer one ("name" inside "first name"), and the only robust fix is to try the
#: most specific alias first.
_KNOWN_ALIASES: Final[tuple[str, ...]] = tuple(sorted(KNOWN_FIELDS, key=len, reverse=True))
_EEO_ALIASES: Final[tuple[str, ...]] = tuple(sorted(EEO_FIELDS, key=len, reverse=True))

#: Decline markers sorted longest-first, for the same reason ("decline" inside "decline to
#: self identify").
_DECLINE_MARKERS_SORTED: Final[tuple[str, ...]] = tuple(
    sorted(DECLINE_MARKERS, key=len, reverse=True)
)


# ======================================================================================
# The plan
# ======================================================================================


@dataclass(slots=True)
class AnswerPlan:
    """One field's answer, plus everything needed to decide whether to submit it (§10).

    Attributes:
        field: The form input this answers. Kept whole rather than reduced to a selector so
            the browser layer can act on ``kind``, ``options`` and ``max_length`` without a
            second lookup, and so a review-queue entry can show the human the real question.
        value: The value to type, select or click. ``""`` means "no answer" and always travels
            with confidence ``0.0``.
        confidence: How much this answer should be trusted, in ``[0, 1]``. Compared against
            ``settings.min_answer_confidence`` by
            :meth:`app.browser.autofill.AutoFiller.fill`; below it the application goes to a
            human instead (golden rule #2).
        source: Which resolution path produced this — one of the ``SOURCE_*`` constants. Shown
            in the review queue so a person can see the system's reasoning at a glance.
        reasoning: Optional human-readable explanation, populated on the model path.
    """

    field: FormField
    value: str = ""
    confidence: float = NO_ANSWER
    source: str = SOURCE_NONE
    reasoning: str = ""

    @property
    def answered(self) -> bool:
        """Whether this plan carries a value at all."""
        return bool(self.value.strip())

    def is_confident(self, threshold: float) -> bool:
        """Return whether this answer clears a confidence threshold.

        Args:
            threshold: Normally ``settings.min_answer_confidence``.

        Returns:
            ``True`` only when there is a value *and* the confidence reaches *threshold*. An
            empty value never clears any threshold, whatever confidence it claims.
        """
        return self.answered and self.confidence >= threshold

    def declined(self) -> AnswerPlan:
        """Return a copy of this plan with no answer at all.

        Returns:
            The same field, an empty value and confidence ``0.0`` — the shape that sends a
            field to a human.
        """
        return AnswerPlan(
            field=self.field,
            value="",
            confidence=NO_ANSWER,
            source=SOURCE_NONE,
            reasoning=self.reasoning,
        )


# ======================================================================================
# The answerer
# ======================================================================================


class FieldAnswerer:
    """Answers discovered form fields for one applicant (``docs/CONTRACTS.md`` §10).

    Bound to one profile and one answers dictionary; every field is passed in. Safe to reuse
    across the whole of one application.

    Usage::

        answerer = FieldAnswerer(user_dto, application.answers, llm=get_llm("fast"))
        plans = [await answerer.answer(field) for field in discovered_fields]
        review = [plan.field for plan in plans
                  if not plan.is_confident(settings.min_answer_confidence)]
    """

    def __init__(
        self,
        user: UserProfileDTO,
        answers: dict[str, Any],
        llm: ModelPlugin | None = None,
        knowledge: KnowledgeRetriever | None = None,
    ) -> None:
        """Bind the answerer to a profile and the answers already settled.

        Args:
            user: The applicant's answer sheet.
            answers: Values the user or a previous manual review already supplied, keyed by
                field selector, by raw label, or by normalised label — all three are looked
                up, because the key that gets stored depends on which surface recorded it.
            llm: Model client for genuine free-text questions. ``None`` disables the model
                path entirely, which is a supported configuration: everything else still
                works and essays simply go to a human.
            knowledge: Retriever used to ground a free-text answer in the applicant's own
                facts, **and** the source of the memories injected into the model prompt.
                ``None`` means the model sees only the profile and the question.
        """
        self.user = user
        self.answers = dict(answers or {})
        self.llm = llm
        self.knowledge = knowledge
        self._normalized_answers = {
            normalize_label(str(key)): value
            for key, value in self.answers.items()
            if isinstance(key, str)
        }
        #: The applicant's own contact details, exempted from the memory PII screen. Their
        #: email address and phone number belong on their own résumé, and treating them as a
        #: leak would exclude nearly every memory worth injecting.
        self._memory_allow: tuple[str, ...] = contact_allowlist(user.email, user.phone)
        #: Every memory this answerer has put in front of the model, in first-injection order.
        #: Exposed so a caller can audit one form's answers without re-running retrieval.
        self.injected_memory_ids: list[uuid.UUID] = []

    # ----------------------------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------------------------

    async def answer(self, field: FormField) -> AnswerPlan:
        """Resolve one field, or refuse it.

        The order is fixed by ``docs/CONTRACTS.md`` §10: the explicit answers dictionary, then
        :data:`KNOWN_FIELDS`, then EEO handling, then the model for genuine free text, then
        nothing. **An EEO field short-circuits before the model under every phrasing** — that
        is the one branch of this method that exists for legal and ethical reasons rather than
        for accuracy.

        Whatever path produced the value, a ``select``/``radio``/``multiselect``/``checkbox``
        field then passes through :meth:`coerce_to_options`, because a value that is not in the
        control fails the form silently.

        Args:
            field: The discovered input.

        Returns:
            The plan. Confidence ``0.0`` means "ask a human", which is a correct outcome and
            not an error. A field whose own text was refused by the §10b screen comes back at
            confidence ``0.0`` with source :data:`SOURCE_BLOCKED` — never as an exception,
            because one poisoned field must not abandon the other twenty on the form.
        """
        blocked = self._screen(field)
        if blocked is not None:
            return blocked

        label = normalize_label(field.label)

        explicit = self._explicit(field, label)
        if explicit is not None:
            return self.coerce_to_options(explicit)

        eeo_attribute = self._eeo_attribute(label)
        if eeo_attribute is None:
            known = self._known(field, label)
            if known is not None:
                return self.coerce_to_options(known)
        else:
            # EEO: resolved from the profile or from the form's own decline option, and
            # returned unconditionally. Nothing here ever reaches the model.
            return self.coerce_to_options(self._eeo(field, eeo_attribute))

        if self._is_free_text(field, label):
            memories = await self._memories_for(field)
            generated = await self._llm_answer(field, memories)
            plan = self.coerce_to_options(generated) if generated is not None else None
            # The outcome is known here and nowhere earlier: the model may have declined, the
            # answer may have invented a number, and option coercion may have dropped it. Only
            # a plan that survives all three is going to be typed rather than handed to a
            # human, and only that plan credits the memories that shaped it.
            await self._settle_memories(memories, plan)
            if plan is not None:
                return plan

        logger.info(
            "field_answer.unanswered",
            label=field.label,
            kind=field.kind.value,
            required=field.required,
        )
        return AnswerPlan(field=field, value="", confidence=NO_ANSWER, source=SOURCE_NONE)

    def answer_known(self, field: FormField) -> AnswerPlan | None:
        """Resolve one field deterministically, with no model call and no coroutine.

        The synchronous subset of :meth:`answer`: the explicit dictionary, then
        :data:`KNOWN_FIELDS`, then EEO handling. Exposed because the browser layer fills most
        of a form before it ever needs a model, and because it makes the deterministic path
        testable without an event loop.

        Args:
            field: The discovered input.

        Returns:
            The plan, already coerced to the field's options, or ``None`` when only the model
            path could answer this field.
        """
        label = normalize_label(field.label)
        explicit = self._explicit(field, label)
        if explicit is not None:
            return self.coerce_to_options(explicit)
        eeo_attribute = self._eeo_attribute(label)
        if eeo_attribute is not None:
            return self.coerce_to_options(self._eeo(field, eeo_attribute))
        known = self._known(field, label)
        return self.coerce_to_options(known) if known is not None else None

    # ----------------------------------------------------------------------------------
    # The untrusted-text screen
    # ----------------------------------------------------------------------------------

    def _screen(self, field: FormField) -> AnswerPlan | None:
        """Refuse a field whose own text is a prompt injection (``docs/CONTRACTS.md`` §10b).

        A form's labels, helper text and option values are served by the employer's ATS and
        are attacker-controlled in exactly the way a job description is. This is the most
        exposed of the four §10b call sites, because unlike the résumé path there is **no
        fact-id validator downstream**: whatever this class returns is typed into a form and
        submitted under the user's name.

        The refusal is a plan, not an exception. A form has twenty fields and one of them
        being poisoned is a reason to hand that form to a human, not a reason to abandon the
        other nineteen mid-fill.

        Args:
            field: The discovered input.

        Returns:
            A refusing plan at confidence :data:`NO_ANSWER` with source
            :data:`SOURCE_BLOCKED`, or ``None`` when the field is safe to resolve.
        """
        for part, value in self._untrusted_parts(field):
            _safe, verdict = sanitize_external_text(value, source=f"form_field:{part}")
            if verdict.risk is not InjectionRisk.HIGH:
                continue
            logger.warning(
                "field_answer.policy_block",
                label=field.label,
                selector=field.selector,
                part=part,
                score=verdict.score,
                signals=verdict.signals,
            )
            return AnswerPlan(
                field=field,
                value="",
                confidence=NO_ANSWER,
                source=SOURCE_BLOCKED,
                reasoning=(
                    f"the form's {part} scored {verdict.score:.2f} for prompt injection "
                    f"({', '.join(verdict.signals)}); it was not read"
                ),
            )
        return None

    @staticmethod
    def _untrusted_parts(field: FormField) -> list[tuple[str, str]]:
        """Return the field's externally-supplied strings, each with a name for the log.

        Args:
            field: The discovered input.

        Returns:
            ``(part_name, value)`` pairs for the label, the helper text and every option.
        """
        parts: list[tuple[str, str]] = [("label", field.label or "")]
        if field.hint:
            parts.append(("hint", field.hint))
        parts.extend((f"option[{index}]", option) for index, option in enumerate(field.options))
        return [(name, value) for name, value in parts if value.strip()]

    @staticmethod
    def _safe_for_prompt(value: str, *, part: str) -> str:
        """Return one field string in the form that may be rendered into a prompt.

        Only ever called after :meth:`_screen` has cleared the field, so the risk here is at
        most :attr:`~app.ai.untrusted.InjectionRisk.MEDIUM` and the result is the text with
        the offending spans removed.

        Args:
            value: The raw label, hint or option.
            part: Which one, for the log line.

        Returns:
            The screened text.
        """
        safe, _verdict = sanitize_external_text(value, source=f"form_field:{part}")
        return safe

    # ----------------------------------------------------------------------------------
    # Resolution paths
    # ----------------------------------------------------------------------------------

    def _explicit(self, field: FormField, label: str) -> AnswerPlan | None:
        """Return the answer the user already supplied for this field, if any.

        Three keys are tried, because three different surfaces write into this dictionary: the
        browser layer keys by selector, the review UI keys by the label the human saw, and
        onboarding keys by a normalised label.

        Args:
            field: The discovered input.
            label: Its normalised label.

        Returns:
            A plan at confidence :data:`EXPLICIT_CONFIDENCE`, or ``None``.
        """
        for key in (field.selector, field.label, label):
            if not key:
                continue
            if key in self.answers:
                value = _text(self.answers[key])
                if value:
                    return AnswerPlan(
                        field=field,
                        value=value,
                        confidence=EXPLICIT_CONFIDENCE,
                        source=SOURCE_EXPLICIT,
                    )
        if label and label in self._normalized_answers:
            value = _text(self._normalized_answers[label])
            if value:
                return AnswerPlan(
                    field=field,
                    value=value,
                    confidence=EXPLICIT_CONFIDENCE,
                    source=SOURCE_EXPLICIT,
                )
        return None

    def _known(self, field: FormField, label: str) -> AnswerPlan | None:
        """Resolve this field from the profile through :data:`KNOWN_FIELDS`.

        Args:
            field: The discovered input.
            label: Its normalised label.

        Returns:
            A plan at confidence :data:`KNOWN_CONFIDENCE`, or ``None`` when no alias matched
            or the matching resolver found nothing in the profile. The distinction matters:
            "we do not recognise this question" and "we recognise it and the user never
            answered it" both go to a human, and both are correct.
        """
        if not label:
            return None
        for alias in _KNOWN_ALIASES:
            if alias not in label:
                continue
            value = _text(KNOWN_FIELDS[alias](self.user))
            if value is None:
                logger.debug("field_answer.profile_empty", label=field.label, alias=alias)
                return None
            return AnswerPlan(
                field=field,
                value=value,
                confidence=KNOWN_CONFIDENCE,
                source=SOURCE_PROFILE,
                reasoning=f"Matched the profile field for {alias!r}.",
            )
        return None

    @staticmethod
    def _eeo_attribute(label: str) -> str | None:
        """Return which EEO attribute *label* asks about, if any.

        Args:
            label: A normalised label.

        Returns:
            The :class:`~app.jobs.base.UserProfileDTO` attribute name, or ``None``.
        """
        if not label:
            return None
        for alias in _EEO_ALIASES:
            if alias in label:
                return EEO_FIELDS[alias]
        return None

    def _eeo(self, field: FormField, attribute: str) -> AnswerPlan:
        """Answer a demographic question without ever inferring anything.

        Three outcomes, in order:

        1. The user stated a value and it matches one of the offered options — submit it.
        2. The form offers a "decline to self-identify" option — choose it. This is the
           honest answer for a user who did not state a value, and it is always available on a
           compliant EEO form.
        3. Neither — return confidence ``0.0`` and let a human decide.

        Args:
            field: The discovered input.
            attribute: The profile attribute this question asks about.

        Returns:
            The plan. Never a guess, under any circumstances.
        """
        stated = _text(getattr(self.user, attribute, None))
        if stated and stated != DECLINE_VALUE:
            logger.debug("field_answer.eeo_from_profile", label=field.label, attribute=attribute)
            return AnswerPlan(
                field=field,
                value=stated,
                confidence=KNOWN_CONFIDENCE,
                source=SOURCE_EEO,
                reasoning="The applicant stated this value during onboarding.",
            )

        decline = self._decline_option(field.options)
        if decline is not None:
            logger.info(
                "field_answer.eeo_declined",
                label=field.label,
                attribute=attribute,
                option=decline,
            )
            return AnswerPlan(
                field=field,
                value=decline,
                confidence=KNOWN_CONFIDENCE,
                source=SOURCE_DECLINED,
                reasoning="Demographic questions are never inferred; declining to self-identify.",
            )

        logger.info(
            "field_answer.eeo_unanswerable",
            label=field.label,
            attribute=attribute,
            options=len(field.options),
        )
        return AnswerPlan(
            field=field,
            value="",
            confidence=NO_ANSWER,
            source=SOURCE_NONE,
            reasoning=(
                "Demographic questions are never inferred, and no decline option was offered."
            ),
        )

    @staticmethod
    def _decline_option(options: Sequence[str]) -> str | None:
        """Return the "prefer not to say" option among *options*, if there is one.

        Args:
            options: The values the control offers.

        Returns:
            The offered option verbatim — so it can be submitted byte-for-byte — or ``None``.
        """
        for option in options or ():
            normalized = normalize_label(option)
            if any(marker in normalized for marker in _DECLINE_MARKERS_SORTED):
                return option
        return None

    # ----------------------------------------------------------------------------------
    # The model path
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _is_free_text(field: FormField, label: str) -> bool:
        """Return whether this field is a genuine question rather than a data entry box.

        A textarea always is. A ``text`` field is one when its label reads as a question —
        long enough, or ending in a question mark.

        **A constrained field can be a question too**, and excluding all of them cost real
        answers. The original rule was "a model cannot improve on a profile lookup and can
        only introduce a fabrication", which was true when a profile lookup was the only
        other source. It is not any more: the answerer is handed a
        :class:`~app.knowledge.retrieval.KnowledgeRetriever`, so "Do you have experience
        working with front-end languages such as HTML, CSS, and JavaScript?" is a question
        about *evidence in the knowledge graph* — indexed from the applicant's own repositories
        — rather than an invitation to guess. Observed on a live Lever form, seven such
        questions went to a human while the facts that answer them sat indexed and unread.

        Three things keep golden rule #2 intact on that path, and none of them are new:
        demographic questions return before this is ever called and can never reach the model;
        :meth:`coerce_to_options` forces the answer to be one of the offered options verbatim
        or drops it; and the model's confidence is capped at :data:`MAX_LLM_CONFIDENCE` and
        still has to clear ``settings.min_answer_confidence`` to be typed rather than asked.

        Controls that are not questions at all — dates, numbers, files, URLs — stay
        deterministic, because there is nothing for a model to reason about.

        Args:
            field: The discovered input.
            label: Its normalised label.

        Returns:
            Whether the model path applies.
        """
        if field.kind is FieldKind.TEXTAREA:
            return True
        if field.kind not in _MODEL_ANSWERABLE_KINDS:
            return False
        return field.label.strip().endswith("?") or len(label.split()) >= LLM_MIN_QUESTION_WORDS

    async def _llm_answer(self, field: FormField, memories: MemoryBlock) -> AnswerPlan | None:
        """Ask the model to write a free-text answer, grounded in the applicant's facts.

        The model's self-reported confidence is capped at :data:`MAX_LLM_CONFIDENCE`, and an
        empty answer is honoured as a refusal rather than overridden — the prompt explicitly
        tells it that declining is a first-class outcome, so a decline is information.

        Args:
            field: The discovered input.
            memories: The screened memory block, injected into the **system** prompt. It is
                the applicant's own prior corrections, and the prompt tells the model to prefer
                them over its own guesses while never reading them as facts about the employer.

        Returns:
            The plan, or ``None`` when there is no model, the call failed, the model declined,
            or the answer stated a number nothing in the supporting material supports. Every
            failure is a degradation to "ask a human", never an exception.
        """
        if self.llm is None:
            return None

        try:
            grounding = await self._grounding(field)
            payload = await self.llm.complete_json(
                system=load_prompt("field_answer.system", memories=memories.for_prompt()),
                prompt=self._prompt(field, grounding),
                schema=FIELD_ANSWER_SCHEMA,
                temperature=ANSWER_TEMPERATURE,
                max_tokens=ANSWER_MAX_TOKENS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "field_answer.llm_failed",
                label=field.label,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

        answer = _text(payload.get("answer"))
        if not answer:
            logger.info("field_answer.llm_declined", label=field.label)
            return None

        # The same rule the résumé and the cover letter obey: a number the supporting
        # material does not contain was invented, and "I have 7 years of Kubernetes
        # experience" is exactly the sentence this system must never submit. Unlike a résumé
        # bullet there is no original wording to revert to, so the field goes to a human.
        #
        # The memory block counts as supporting material, and only here. Every memory in it is
        # something *the user themselves typed* — a correction recorded when a human resolved
        # this exact question before — so "four weeks" echoed back from "last time you told me
        # my notice period is four weeks" is quotation, not invention. Excluding it would make
        # the feedback loop useless: the system would re-ask the one question the user has
        # already answered, every single time. Nothing else changes; the memory block still
        # cannot introduce an employer, a date or a credential, because the prompt forbids it
        # and the confidence gate still decides whether anything is submitted at all.
        supported = numbers_in(
            " ".join(
                [
                    field.label,
                    field.hint or "",
                    memories.text,
                    *grounding,
                    *self._profile_lines(),
                ]
            )
        )
        invented = numbers_in(answer) - supported
        if invented:
            logger.warning(
                "field_answer.unsupported_metric",
                label=field.label,
                numbers=sorted(invented),
            )
            return None

        limit = field.max_length or DEFAULT_ANSWER_CHARS
        if len(answer) > limit:
            logger.info(
                "field_answer.llm_truncated",
                label=field.label,
                characters=len(answer),
                limit=limit,
            )
            answer = answer[:limit].rstrip()

        raw_confidence = payload.get("confidence")
        confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool)
            else NO_ANSWER
        )
        confidence = max(NO_ANSWER, min(MAX_LLM_CONFIDENCE, confidence))

        reasoning = payload.get("reasoning")
        return AnswerPlan(
            field=field,
            value=answer,
            confidence=confidence,
            source=SOURCE_LLM,
            reasoning=reasoning if isinstance(reasoning, str) else "",
        )

    async def _grounding(self, field: FormField) -> list[str]:
        """Retrieve the applicant's own facts relevant to this question.

        Args:
            field: The discovered input, whose label is the retrieval query.

        Returns:
            Rendered fact lines, or ``[]`` when no retriever was supplied, the profile has no
            ``user_id``, or retrieval failed — in which case the model answers from the profile
            alone, which is a weaker but still safe position.
        """
        user_id = getattr(self.user, "user_id", None)
        if self.knowledge is None or user_id is None:
            return []
        query = f"{field.label} {field.hint or ''}".strip()
        if not query:
            return []
        try:
            result = await self.knowledge.retrieve(
                user_id, query, k_facts=LLM_KNOWLEDGE_FACTS, k_chunks=0
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("field_answer.retrieval_failed", label=field.label, error=str(exc))
            return []
        return [
            _WHITESPACE.sub(" ", (fact.text or "")).strip()[:MAX_FACT_CHARS]
            for fact in result.facts
            if (fact.text or "").strip()
        ]

    # ----------------------------------------------------------------------------------
    # Memory
    # ----------------------------------------------------------------------------------

    def _memory_store(self) -> MemoryStore | None:
        """Return the memory store behind the retriever, if there is one.

        Args:
            None.

        Returns:
            The store, or ``None`` when no retriever was supplied, when the retriever does not
            expose one (a test double, a future retriever), or when the profile carries no
            ``user_id``. Every one of those is a supported configuration in which the model
            simply answers without the user's memory.
        """
        if self.knowledge is None or getattr(self.user, "user_id", None) is None:
            return None
        return getattr(self.knowledge, "memory", None)

    async def _memories_for(self, field: FormField) -> MemoryBlock:
        """Retrieve, screen and render the memories relevant to one question.

        The retrieval query is the question itself, which is what makes "last time you told me
        my notice period is four weeks" surface on a notice-period field and stay out of the
        way everywhere else. Screening happens inside
        :func:`app.ai.memory_prompt.build_memory_block`; a memory carrying personal data is
        excluded whole, and only the category is logged.

        Args:
            field: The discovered input, already cleared by :meth:`_screen`.

        Returns:
            The block, empty when there is no store, no ``user_id``, no memory, or nothing that
            survived the screen.
        """
        store = self._memory_store()
        user_id = getattr(self.user, "user_id", None)
        if store is None or user_id is None:
            return MemoryBlock()

        query = f"{field.label} {field.hint or ''}".strip()
        block = await build_memory_block(
            store,
            user_id,
            query,
            allow=self._memory_allow,
            purpose=MEMORY_PURPOSE,
            k=MEMORY_PROMPT_K,
        )
        for memory_id in block.memory_ids:
            if memory_id not in self.injected_memory_ids:
                self.injected_memory_ids.append(memory_id)
        return block

    async def _settle_memories(self, memories: MemoryBlock, plan: AnswerPlan | None) -> int:
        """Credit the injected memories when the field was answered rather than escalated.

        The supervised half of the loop, and the reason it lives here rather than beside the
        injection: at injection time nobody knows whether the answer will be submitted. A field
        whose plan clears ``settings.min_answer_confidence`` is typed into the form; anything
        below it goes to a human (golden rule #2). So "did not come back to review" is exactly
        "cleared the threshold", and it is known only once :meth:`coerce_to_options` has had its
        say.

        There is no penalty on the other branch. A field escalates for a hundred reasons — a
        model outage, an unanswerable question, an option that matched nothing — and docking a
        memory for all of them would eventually silence the corrections this product runs on.

        Args:
            memories: The block that was injected.
            plan: The final plan, or ``None`` when the model produced nothing usable.

        Returns:
            How many memories were reinforced.
        """
        if not memories.memory_ids:
            return 0

        from app.config.settings import get_settings

        threshold = float(get_settings().min_answer_confidence)
        if plan is None or not plan.is_confident(threshold):
            logger.debug(
                "memory.not_reinforced",
                purpose=MEMORY_PURPOSE,
                memories=len(memories.memory_ids),
                confidence=plan.confidence if plan is not None else None,
                threshold=threshold,
            )
            return 0

        store = self._memory_store()
        if store is None:  # pragma: no cover - the block came from that store
            return 0
        return await reinforce_used(store, memories.memory_ids, purpose=MEMORY_PURPOSE)

    def _prompt(self, field: FormField, grounding: Sequence[str]) -> str:
        """Render the user message for one free-text question.

        Args:
            field: The discovered input.
            grounding: Rendered fact lines from the applicant's knowledge graph.

        Returns:
            The prompt: the question, its constraints, the profile facts that are safe to
            state, and an explicit reminder that declining is allowed. Every string that came
            from the form has been through the §10b screen first — the field itself was
            cleared by :meth:`_screen`, so what survives here is at most a mid-risk string
            with its offending spans already removed.
        """
        limit = field.max_length or DEFAULT_ANSWER_CHARS
        lines = [
            "Answer this one question on a job application form.",
            "",
            "## The question",
            "",
            f"- Label: {self._safe_for_prompt(field.label, part='label')}",
            f"- Input type: {field.kind.value}",
            f"- Required: {'yes' if field.required else 'no'}",
            f"- Character limit: {limit}",
        ]
        if field.hint:
            lines.append(f"- Helper text: {self._safe_for_prompt(field.hint, part='hint')}")
        if field.options:
            lines.extend(
                [
                    "",
                    "## Allowed answers",
                    "",
                    "The answer must be exactly one of these, copied verbatim:",
                    "",
                    *(
                        f"- {self._safe_for_prompt(option, part=f'option[{index}]')}"
                        for index, option in enumerate(field.options)
                    ),
                ]
            )

        lines.extend(["", "## What is known about the applicant", ""])
        profile_lines = self._profile_lines()
        lines.extend(profile_lines or ["(nothing beyond contact details)"])

        if grounding:
            lines.extend(
                [
                    "",
                    "### Verified facts from the applicant's own history",
                    "",
                    *(f"- {fact}" for fact in grounding),
                ]
            )

        lines.extend(
            [
                "",
                (
                    "Return the JSON object described in your instructions. If the "
                    "material above does not contain what the question asks for, return an "
                    "empty answer with confidence 0.0 — a human will answer it, and that "
                    "is the correct outcome."
                ),
            ]
        )
        return "\n".join(lines)

    def _profile_lines(self) -> list[str]:
        """Render the non-sensitive profile facts a free-text answer may draw on.

        Demographic attributes are absent by construction: they are not read here, so they
        cannot be sent, whatever the question asks.

        Returns:
            One bullet per stated attribute.
        """
        user = self.user
        candidates: list[tuple[str, str | None]] = [
            ("Name", _text(user.full_name)),
            ("Location", _text(user.location)),
            ("Target roles", ", ".join(user.desired_roles) or None),
            ("Target industries", ", ".join(user.desired_industries) or None),
            (
                "Work authorisation",
                user.work_authorization.value
                if user.work_authorization is not WorkAuthStatus.UNKNOWN
                else None,
            ),
            ("Availability", _text(user.start_date_availability)),
            ("Notice period", _notice_period(user)),
            ("Willing to relocate", _yes_no(user.willing_to_relocate)),
        ]
        return [f"- {name}: {value}" for name, value in candidates if value]

    # ----------------------------------------------------------------------------------
    # Option coercion
    # ----------------------------------------------------------------------------------

    def coerce_to_options(self, plan: AnswerPlan) -> AnswerPlan:
        """Force a constrained field's value to be one of the options it offers.

        A ``<select>`` silently ignores a value that is not among its ``<option>`` elements.
        The form then fails validation on submit and the failure looks like a broken selector
        rather than a wrong answer, which is the most expensive kind of bug this system can
        have. So the value is matched against the offered options — exactly, then by
        normalised equality, then by containment, then by yes/no meaning, and finally by
        :class:`difflib.SequenceMatcher` ratio against :data:`MIN_OPTION_SIMILARITY` — and if
        nothing matches, the plan is dropped to confidence ``0.0`` and a human is asked.

        Args:
            plan: The resolved plan.

        Returns:
            The plan with its value replaced by the matching option verbatim, or a declined
            copy. Unconstrained fields and fields with no options pass through untouched.
        """
        field = plan.field
        if field.kind not in CONSTRAINED_KINDS or not field.options:
            return plan
        if not plan.answered:
            return plan

        match = self._match_option(plan.value, field.options)
        if match is None:
            logger.warning(
                "field_answer.option_mismatch",
                label=field.label,
                value=plan.value,
                options=list(field.options)[:12],
                source=plan.source,
            )
            # A demographic question always has an honest answer available, and it is the
            # form's own decline option — never a human's time. `_eeo_plan` already knows
            # that, but its work was being undone here: a stated value that does not match
            # the offered wording is a *vocabulary* mismatch, not an unanswerable question.
            #
            # Observed on a live Lever form: the profile said "I AM NOT A VETERAN" and the
            # control offered "I am not a protected veteran". Too dissimilar to match, so
            # the field was escalated — asking the applicant to hand-answer a question they
            # had already answered during onboarding, on a form where declining is
            # explicitly voluntary and explicitly offered.
            if plan.source in _EEO_SOURCES:
                decline = self._decline_option(field.options)
                if decline is not None:
                    logger.info(
                        "field_answer.eeo_declined_after_mismatch",
                        label=field.label,
                        stated=plan.value,
                        option=decline,
                    )
                    return AnswerPlan(
                        field=field,
                        value=decline,
                        confidence=KNOWN_CONFIDENCE,
                        source=SOURCE_DECLINED,
                        reasoning=(
                            "The stated value does not match any option this form offers, "
                            "and demographic questions are never inferred; declining."
                        ),
                    )
            return plan.declined()

        if match != plan.value:
            logger.debug(
                "field_answer.option_matched",
                label=field.label,
                value=plan.value,
                option=match,
            )
        return AnswerPlan(
            field=field,
            value=match,
            confidence=plan.confidence,
            source=plan.source,
            reasoning=plan.reasoning,
        )

    @staticmethod
    def _match_option(value: str, options: Sequence[str]) -> str | None:
        """Return the offered option *value* means, or ``None``.

        Args:
            value: The resolved value.
            options: The values the control offers.

        Returns:
            The matching option **verbatim**, so it can be submitted byte-for-byte.
        """
        candidate = value.strip()
        for option in options:
            if option == candidate:
                return option

        normalized = normalize_label(candidate)
        if not normalized:
            return None
        pairs = [(normalize_label(option), option) for option in options]

        for option_key, option in pairs:
            if option_key == normalized:
                return option

        # Yes/no questions before containment: "Yes" is a substring of "Yes, I am authorized"
        # *and* of "No, but yes to relocation", so meaning has to win over spelling.
        if normalized in _AFFIRMATIVE or normalized in _NEGATIVE:
            wanted = _AFFIRMATIVE if normalized in _AFFIRMATIVE else _NEGATIVE
            unwanted = _NEGATIVE if normalized in _AFFIRMATIVE else _AFFIRMATIVE
            for option_key, option in pairs:
                head = option_key.split(" ", 1)[0]
                if head in wanted and head not in unwanted:
                    return option

        for option_key, option in pairs:
            if option_key and (option_key in normalized or normalized in option_key):
                return option

        best: tuple[float, str] | None = None
        for option_key, option in pairs:
            if not option_key:
                continue
            ratio = SequenceMatcher(None, normalized, option_key).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, option)
        if best is not None and best[0] >= MIN_OPTION_SIMILARITY:
            return best[1]
        return None
