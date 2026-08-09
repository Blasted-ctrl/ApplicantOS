"""Cover letters — the second document, written only from the first one and the posting.

``docs/CONTRACTS.md`` §10 gives this module two entry points and they answer two different
questions. :meth:`CoverLetterWriter.should_write` decides *whether* a letter is worth writing
at all, because most applications do not want one and a needless letter costs tokens and adds
a surface for a mistake. :meth:`CoverLetterWriter.write` produces the letter when the answer
is yes.

**The grounding rule is narrower than the résumé's.** A tailored résumé has already been
through :class:`~app.ai.resume_engine.ResumeEngine`'s validator, so every bullet on it traces
to a verified :class:`~app.models.knowledge.KnowledgeFact`. That makes the résumé — and
nothing else — the safe source of statements about the applicant, and the posting the safe
source of statements about the employer. A letter that praises a funding round, a mission
statement or a product the posting never mentioned is the classic way a generated letter
embarrasses the person who sent it, so this module treats it as a defect and not as flair.

Three validations run on every generated letter:

* **Numbers.** Any number in the letter must appear in the résumé or the posting; a sentence
  carrying an unsupported one is deleted (``cover_letter.unsupported_metric``).
* **Placeholders.** ``[Hiring Manager]``, ``{{company}}``, ``<Role>`` and friends are replaced
  with the real value when one is known and removed when it is not
  (``cover_letter.placeholder_replaced``). A letter that reaches an employer containing a
  template bracket is worse than no letter.
* **Length.** Trailing paragraphs are dropped, then trailing sentences, until the letter is
  inside ``max_words``.

If the model fails — no key, a rate limit, an unparseable reply — or if validation leaves too
little standing, :meth:`CoverLetterWriter.write` returns a deterministic letter assembled from
the résumé's own strongest lines. Like the résumé engine's fallback it is a real document, not
a placeholder, and it cannot say anything the résumé does not already say.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from app.ai.prompts import load_prompt
from app.ai.resume_engine import numbers_in
from app.ai.untrusted import sanitize_or_raise
from app.cache import NAMESPACES, hash_payload, make_key
from app.documents.models import Contact, CoverLetterDocument, ResumeDocument

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.ai.llm import ModelPlugin
    from app.cache import Cache
    from app.jobs.base import JobPostingDTO, UserProfileDTO
    from app.models.user import UserPreferences

__all__ = [
    "COVER_LETTER_SCHEMA",
    "DEFAULT_MAX_WORDS",
    "DEFAULT_RECIPIENT",
    "DEFAULT_TONE",
    "HIGH_SCORE_THRESHOLD",
    "MAX_PARAGRAPHS",
    "MIN_PARAGRAPHS",
    "PLACEHOLDER_PATTERN",
    "CoverLetterRequest",
    "CoverLetterResult",
    "CoverLetterWriter",
    "Requirement",
    "cover_letter_requirement",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: What a posting says about wanting a cover letter, as
#: :func:`cover_letter_requirement` reports it.
Requirement = Literal["required", "optional", "absent"]

#: Words a letter may run to before it is trimmed. Four short paragraphs, which is what a
#: recruiter reads; past this the letter is skimmed and the last paragraph is wasted.
DEFAULT_MAX_WORDS: Final[int] = 320

#: Fewest paragraphs a generated letter may survive validation with. Below this the letter has
#: lost its argument and the deterministic template is the better document.
MIN_PARAGRAPHS: Final[int] = 2

#: Most paragraphs printed. Extra paragraphs are dropped from the end, which is where a model
#: puts its weakest material.
MAX_PARAGRAPHS: Final[int] = 4

#: Salutation used when the posting names nobody. Honest and standard; never a guessed name,
#: and never "To Whom It May Concern".
DEFAULT_RECIPIENT: Final[str] = "Hiring Manager"

#: Tone recorded on ``cover_letters.tone`` when a request does not name one.
DEFAULT_TONE: Final[str] = "professional"

#: Score at or above which ``cover_letter_policy="when_high_score"`` writes a letter, unless
#: the user's own ``min_score`` floor is higher. A letter is discretionary effort, so it is
#: spent on the postings the scorer actually rated well rather than on everything that cleared
#: the bar.
HIGH_SCORE_THRESHOLD: Final[int] = 85

#: Temperature for letter generation. Zero, so the same application produces the same letter
#: and the call is cacheable in :class:`app.ai.llm.GuardedModelPlugin`.
LETTER_TEMPERATURE: Final[float] = 0.0

#: Completion budget for one letter.
LETTER_MAX_TOKENS: Final[int] = 1536

#: Characters of the posting body sent to the model. A letter needs the requirements section,
#: not the benefits appendix.
MAX_POSTING_CHARS: Final[int] = 4000

#: Headroom the §10b screening cap allows above :data:`MAX_POSTING_CHARS` for the title and
#: company lines, which are prepended to the body before screening.
MAX_SCREENED_HEADER_CHARS: Final[int] = 400

#: Characters of the résumé rendered into the prompt.
MAX_RESUME_CHARS: Final[int] = 6000

#: Longest recipient name accepted from the model. Anything longer is a sentence, not a name.
MAX_RECIPIENT_CHARS: Final[int] = 80

#: Discriminator on the cache key, so letters cannot collide with other ``llm``-namespace
#: entries.
CACHE_DISCRIMINATOR: Final[str] = "cover_letter"

#: TTL for a cached letter. ``None`` defers to ``settings.cache_default_ttl``.
LETTER_CACHE_TTL_SECONDS: Final[int | None] = None

#: Characters either side of a "cover letter" mention that :func:`cover_letter_requirement`
#: reads to decide whether it is being demanded or merely offered. Wide enough to catch
#: "a cover letter is not required for this role", narrow enough that the next bullet point
#: does not bleed in.
REQUIREMENT_WINDOW_CHARS: Final[int] = 90

#: The phrase whose neighbourhood is inspected.
_COVER_LETTER_PHRASE: Final[str] = "cover letter"

#: Markers that make a nearby "cover letter" mention *optional*. Checked first, because
#: "not required" and "optional but encouraged" both contain a required-marker substring.
_OPTIONAL_MARKERS: Final[tuple[str, ...]] = (
    "optional",
    "not required",
    "no cover letter",
    "isn't required",
    "is not required",
    "not necessary",
    "if you wish",
    "if you would like",
    "if you'd like",
    "encouraged but",
    "welcome but",
)

#: Markers that make a nearby "cover letter" mention *required*.
_REQUIRED_MARKERS: Final[tuple[str, ...]] = (
    "required",
    "must include",
    "must submit",
    "must attach",
    "please include",
    "please attach",
    "please submit",
    "please provide",
    "be sure to include",
    "applications without",
    "we ask that you include",
    "you will be asked to",
)

#: A template placeholder in any of the shapes models leave behind: ``[Company]``,
#: ``{{company}}``, ``{company}``, ``<Hiring Manager>``. Deliberately greedy about the
#: bracket styles and conservative about the content — no newlines, bounded length — so a
#: legitimate parenthetical or a stray angle bracket in prose is not eaten.
PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\{\{\s*([^{}\n]{1,60}?)\s*\}\}"
    r"|\{\s*([^{}\n]{1,60}?)\s*\}"
    r"|\[\s*([^\[\]\n]{1,60}?)\s*\]"
    r"|<\s*([A-Za-z][^<>\n]{0,59}?)\s*>"
)

#: Placeholder names, normalised, mapped to the request field that fills them.
_PLACEHOLDER_FIELDS: Final[dict[str, str]] = {
    "company": "company",
    "company name": "company",
    "employer": "company",
    "organization": "company",
    "organisation": "company",
    "role": "role",
    "position": "role",
    "job title": "role",
    "title": "role",
    "hiring manager": "recipient",
    "recipient": "recipient",
    "manager": "recipient",
    "manager name": "recipient",
    "hiring manager name": "recipient",
    "name": "sender",
    "your name": "sender",
    "candidate name": "sender",
    "applicant name": "sender",
    "full name": "sender",
    "date": "date",
    "today": "date",
    "todays date": "date",
    "email": "email",
    "your email": "email",
    "phone": "phone",
    "your phone": "phone",
    "location": "location",
    "city": "location",
}

#: Non-alphanumeric characters collapsed when normalising a placeholder name.
_PLACEHOLDER_NOISE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Sentence boundary, used to delete exactly the sentence carrying an unsupported number.
_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")

#: Runs of whitespace, collapsed to a single space.
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Three or more consecutive newlines, collapsed to a paragraph break.
_PARAGRAPH_BREAK: Final[re.Pattern[str]] = re.compile(r"\n{2,}")

#: JSON Schema for the letter reply. Mirrors the schema block in
#: ``app/ai/prompts/cover_letter.system.md``; the two must be edited together.
COVER_LETTER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["recipient", "body", "reasoning"],
    "additionalProperties": False,
    "properties": {
        "recipient": {
            "type": "string",
            "description": (
                "Hiring manager's name if the posting states one, else 'Hiring Manager'."
            ),
        },
        "body": {
            "type": "string",
            "description": "The letter body, paragraphs separated by a blank line.",
        },
        "reasoning": {
            "type": "string",
            "description": "Which résumé evidence was used, and why.",
        },
    },
}


# ======================================================================================
# Requirement detection
# ======================================================================================


def _nearest_marker(window: str, markers: Sequence[str], anchor: int) -> int | None:
    """Return the distance from *anchor* to the closest of *markers* inside *window*.

    Args:
        window: The text to search, already case-folded.
        markers: Marker phrases to look for.
        anchor: Index within *window* that the distance is measured from.

    Returns:
        The smallest absolute distance, or ``None`` when no marker occurs.
    """
    best: int | None = None
    for marker in markers:
        position = window.find(marker)
        while position >= 0:
            distance = abs(position - anchor)
            if best is None or distance < best:
                best = distance
            position = window.find(marker, position + 1)
    return best


def cover_letter_requirement(text: str) -> Requirement:
    """Report what *text* says about wanting a cover letter.

    Postings are inconsistent about this — "Cover letter (optional)", "A cover letter is
    required", "Applications without a cover letter will not be considered", "no cover letter
    necessary" — so every mention of the phrase is inspected in a
    :data:`REQUIREMENT_WINDOW_CHARS` window rather than the document being pattern-matched as
    a whole.

    Within one window both kinds of marker often occur, and neither "optional wins" nor
    "required wins" is right: "encouraged but not required" is optional while "a cover letter
    is optional, but you must include one for the senior track" is required. **Proximity
    decides.** The marker closest to the mention is the one describing it, which also resolves
    the substring problem for free — in "not required", the optional marker starts before the
    required one it contains, so it is always the nearer match.

    Args:
        text: The posting text, typically title plus description.

    Returns:
        ``"required"`` when any mention demands one, ``"optional"`` when every mention
        explicitly offers one without demanding it, and ``"absent"`` when the posting does not
        raise the subject. A mention with no marker at all resolves to ``"required"``: writing
        an unwanted letter costs a few tokens, while omitting a demanded one costs the
        application.
    """
    lowered = (text or "").casefold()
    if _COVER_LETTER_PHRASE not in lowered:
        return "absent"

    verdict: Requirement = "absent"
    cursor = 0
    while True:
        index = lowered.find(_COVER_LETTER_PHRASE, cursor)
        if index < 0:
            break
        cursor = index + len(_COVER_LETTER_PHRASE)
        start = max(0, index - REQUIREMENT_WINDOW_CHARS)
        window = lowered[start : cursor + REQUIREMENT_WINDOW_CHARS]
        anchor = index - start

        optional = _nearest_marker(window, _OPTIONAL_MARKERS, anchor)
        required = _nearest_marker(window, _REQUIRED_MARKERS, anchor)
        if optional is not None and (required is None or optional <= required):
            verdict = "optional" if verdict == "absent" else verdict
            continue
        return "required"
    return verdict


# ======================================================================================
# Contract dataclasses
# ======================================================================================


@dataclass(slots=True)
class CoverLetterRequest:
    """One "write the letter for this application" request.

    Attributes:
        user: The applicant, supplying the letter's contact header.
        posting: The job being applied to. **The only safe source of claims about the
            employer.**
        resume: The tailored résumé that will be attached. **The only safe source of claims
            about the applicant** — every bullet on it has already passed
            :class:`~app.ai.resume_engine.ResumeEngine`'s fact validator.
        prefs: The user's policy, part of the cache key.
        tone: Recorded on ``cover_letters.tone`` and passed to the model as guidance.
        max_words: Word ceiling for the finished letter.
        score: The posting's normalised score, when one has been computed. Only read by
            :meth:`CoverLetterWriter.should_write` under the ``when_high_score`` policy.
        recipient: A hiring manager's name the caller already knows. Never guessed here; when
            absent the letter is addressed to :data:`DEFAULT_RECIPIENT`.
    """

    user: UserProfileDTO
    posting: JobPostingDTO
    resume: ResumeDocument
    prefs: UserPreferences
    tone: str = DEFAULT_TONE
    max_words: int = DEFAULT_MAX_WORDS
    score: int | None = None
    recipient: str | None = None
    #: Memoised output of :meth:`posting_text`, so the prompt builder and the number
    #: validator share one screening pass and one log line.
    _screened_posting: str | None = field(default=None, init=False, repr=False, compare=False)

    def posting_text(self) -> str:
        """Return the posting as the model and the number validator read it.

        **The §10b chokepoint for the letter path.** The letter's body is free prose written
        under the user's name and has no fact-id validator behind it, which makes this the
        second most exposed surface in the product after
        :class:`~app.ai.field_answer.FieldAnswerer`.

        Returns:
            Title, company and a bounded, screened description on separate lines.

        Raises:
            UntrustedContentError: If the posting scored
                :attr:`~app.ai.untrusted.InjectionRisk.HIGH`. No letter is written and the
                application goes to a human with
                :attr:`~app.models.enums.ReviewReason.POLICY_BLOCK`.
        """
        if self._screened_posting is None:
            body = _WHITESPACE.sub(" ", self.posting.description or "").strip()
            parts = [
                self.posting.title or "",
                self.posting.company_name or "",
                body[:MAX_POSTING_CHARS],
            ]
            self._screened_posting = sanitize_or_raise(
                "\n".join(part for part in parts if part),
                source=f"posting:{self.posting.id or self.posting.external_id or 'unknown'}",
                max_chars=MAX_POSTING_CHARS + MAX_SCREENED_HEADER_CHARS,
            )
        return self._screened_posting

    def word_budget(self) -> int:
        """Return the effective word ceiling, never below one paragraph's worth.

        Returns:
            :attr:`max_words`, floored so a mis-set value cannot produce an empty letter.
        """
        return max(60, int(self.max_words or DEFAULT_MAX_WORDS))

    def company(self) -> str:
        """Return the employer's name as it should be printed, or ``""``."""
        return (self.posting.company_name or "").strip()

    def role(self) -> str:
        """Return the role's title as it should be printed, or ``""``."""
        return (self.posting.title or "").strip()


@dataclass(slots=True)
class CoverLetterResult:
    """The outcome of one letter generation.

    Attributes:
        body: The letter body: paragraphs separated by blank lines, no salutation, no
            sign-off. Written to ``cover_letters.body``.
        document: The same letter as a
            :class:`~app.documents.models.CoverLetterDocument`, ready for
            :func:`app.documents.render_cover_letter`.
        tone: The tone used, written to ``cover_letters.tone``.
        reasoning: The model's account of which résumé evidence it used, or a description of
            the deterministic assembly on the fallback path.
        token_usage: ``input_tokens`` / ``output_tokens`` / ``total_tokens``, estimated with
            the model's own tokenizer for the reason given in
            :attr:`app.ai.resume_engine.TailorResult.token_usage`. All zero on the fallback
            path.
        cached: Whether this was served from :mod:`app.cache`.
        degraded: Whether the deterministic template produced this rather than the model.
    """

    body: str
    document: CoverLetterDocument
    tone: str = DEFAULT_TONE
    reasoning: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)
    cached: bool = False
    degraded: bool = False

    def word_count(self) -> int:
        """Return how many words the finished letter contains."""
        return len(self.body.split())

    def paragraphs(self) -> list[str]:
        """Return the letter's paragraphs, as the renderer will lay them out."""
        return self.document.paragraphs()


# ======================================================================================
# The writer
# ======================================================================================


class CoverLetterWriter:
    """Decides whether to write a cover letter, and writes it (``docs/CONTRACTS.md`` §10).

    Stateless between calls; one instance is safe to share across a pipeline run.

    Usage::

        writer = CoverLetterWriter(get_llm("reasoning"), get_cache())
        if writer.should_write(posting, prefs, score=score.normalized):
            letter = await writer.write(CoverLetterRequest(user=dto, posting=posting,
                                                           resume=result.document,
                                                           prefs=prefs))
    """

    def __init__(self, llm: ModelPlugin, cache: Cache) -> None:
        """Bind the writer to its collaborators.

        Args:
            llm: The model client. Any failure it raises is degraded, never propagated.
            cache: The cache letters are read from and written to.
        """
        self.llm = llm
        self.cache = cache

    # ----------------------------------------------------------------------------------
    # Policy
    # ----------------------------------------------------------------------------------

    def should_write(
        self,
        posting: JobPostingDTO,
        prefs: UserPreferences,
        *,
        score: int | None = None,
    ) -> bool:
        """Return whether a cover letter should be written for *posting*.

        Honours ``prefs.cover_letter_policy`` exactly:

        ``never``
            Never. No inspection, no exception.
        ``always``
            Always, whatever the posting says.
        ``when_required``
            Only when the posting demands one — :func:`cover_letter_requirement` returning
            ``"required"``. A posting that explicitly calls it optional, or that never raises
            the subject, gets no letter. This is the default and the cheapest correct
            behaviour.
        ``when_high_score``
            When the posting scored at or above :data:`HIGH_SCORE_THRESHOLD` (or the user's
            own ``min_score``, whichever is higher), *or* when it demands one regardless of
            score — a required letter is required no matter how the posting scored.

        Args:
            posting: The job being applied to.
            prefs: The user's policy.
            score: The posting's normalised 0–100 score, when one has been computed. Only
                consulted under ``when_high_score``; an unscored posting there falls back to
                the requirement check rather than guessing.

        Returns:
            Whether to write one.
        """
        policy = prefs.cover_letter_policy
        if policy == "never":
            return False
        if policy == "always":
            return True

        text = f"{posting.title or ''}\n{posting.description or ''}"
        requirement = cover_letter_requirement(text)

        if policy == "when_required":
            decision = requirement == "required"
        else:  # when_high_score
            threshold = max(HIGH_SCORE_THRESHOLD, int(prefs.min_score))
            decision = requirement == "required" or (score is not None and int(score) >= threshold)

        logger.debug(
            "cover_letter.policy",
            policy=policy,
            requirement=requirement,
            score=score,
            decision=decision,
            posting=str(posting.id or ""),
        )
        return decision

    # ----------------------------------------------------------------------------------
    # Generation
    # ----------------------------------------------------------------------------------

    async def write(self, req: CoverLetterRequest) -> CoverLetterResult:
        """Write the cover letter for *req*.

        Cache lookup, model call, validation, assembly. **It never raises on a model
        failure**: anything that can go wrong upstream is answered with
        :meth:`fallback_letter`, for the same reason the résumé engine degrades rather than
        stopping — an application blocked by an API outage is a worse outcome than a plainer
        letter.

        Args:
            req: The letter request.

        Returns:
            The result, with ``cached=True`` when served from the cache and ``degraded=True``
            when the deterministic template produced it.

        Raises:
            UntrustedContentError: If the posting body is a prompt injection (§10b). Screened
                **before** the cache is consulted, so a poisoned posting cannot be answered
                from a hit that predates the defence, and before
                :meth:`fallback_letter` can quietly write a letter from the same text.
        """
        req.posting_text()

        key = self._cache_key(req)
        cached = await self._cache_read(key, req)
        if cached is not None:
            logger.info("cover_letter.cache_hit", posting=str(req.posting.id or ""))
            return cached

        try:
            payload, usage = await self._complete(req)
            result = self._assemble(req, payload, usage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "cover_letter.llm_failed",
                posting=str(req.posting.id or ""),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return self.fallback_letter(req)

        await self._cache_write(key, result)
        logger.info(
            "cover_letter.written",
            posting=str(req.posting.id or ""),
            words=result.word_count(),
            paragraphs=len(result.paragraphs()),
            tokens=result.token_usage.get("total_tokens", 0),
        )
        return result

    async def _complete(self, req: CoverLetterRequest) -> tuple[dict[str, Any], dict[str, int]]:
        """Ask the model for a letter, and account for the call.

        Args:
            req: The letter request.

        Returns:
            The decoded reply and the estimated token usage.

        Raises:
            Exception: Whatever :meth:`app.ai.llm.ModelPlugin.complete_json` raises; the
                caller degrades.
        """
        system = load_prompt("cover_letter.system")
        prompt = self._prompt(req)
        payload = await self.llm.complete_json(
            system=system,
            prompt=prompt,
            schema=COVER_LETTER_SCHEMA,
            temperature=LETTER_TEMPERATURE,
            max_tokens=LETTER_MAX_TOKENS,
        )
        try:
            rendered = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - payload came from json.loads
            rendered = str(payload)
        input_tokens = self.llm.count_tokens(system) + self.llm.count_tokens(prompt)
        output_tokens = self.llm.count_tokens(rendered)
        return payload, {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    @staticmethod
    def _prompt(req: CoverLetterRequest) -> str:
        """Render the user message: the posting, the résumé, and the budget.

        The résumé is rendered as plain text rather than as JSON so the model reads it the
        way a recruiter will, and so the number validator downstream can compare against the
        same string.

        Args:
            req: The letter request.

        Returns:
            The prompt.
        """
        return "\n".join(
            (
                "Write the cover letter for this application, following your instructions.",
                "",
                "## The posting",
                "",
                f"- Title: {req.role() or '—'}",
                f"- Company: {req.company() or '—'}",
                f"- Location: {req.posting.location or '—'}",
                f"- Known recipient: {(req.recipient or '').strip() or 'not stated'}",
                "",
                "```",
                req.posting_text(),
                "```",
                "",
                "## The tailored résumé being attached",
                "",
                "Everything you may say about the applicant is in this document.",
                "",
                "```",
                render_resume_text(req.resume)[:MAX_RESUME_CHARS],
                "```",
                "",
                "## Budget",
                "",
                f"- Tone: {req.tone or DEFAULT_TONE}",
                f"- Paragraphs: 3–{MAX_PARAGRAPHS}",
                f"- Words: at most {req.word_budget()}",
                "",
                (
                    "Return the JSON object described in your instructions. Before you "
                    "answer, check that every claim about the applicant appears in the "
                    "résumé above, every claim about the employer appears in the posting "
                    "above, every number appears in one of the two, and no bracketed "
                    "placeholder survives."
                ),
            )
        )

    # ----------------------------------------------------------------------------------
    # Validation
    # ----------------------------------------------------------------------------------

    def _assemble(
        self, req: CoverLetterRequest, payload: Mapping[str, Any], usage: dict[str, int]
    ) -> CoverLetterResult:
        """Validate a model reply and build the finished letter.

        Args:
            req: The letter request.
            payload: The decoded reply.
            usage: The estimated token usage.

        Returns:
            The result.

        Raises:
            ValueError: When validation left fewer than :data:`MIN_PARAGRAPHS` paragraphs,
                which the caller answers with :meth:`fallback_letter`.
        """
        recipient = self._resolve_recipient(req, payload.get("recipient"))
        body = payload.get("body")
        body = body if isinstance(body, str) else ""

        body = self.replace_placeholders(body, req, recipient)
        body = self.strip_unsupported_numbers(body, req)
        body = self.enforce_length(body, req)

        paragraphs = _paragraphs(body)
        if len(paragraphs) < MIN_PARAGRAPHS:
            raise ValueError(f"letter collapsed to {len(paragraphs)} paragraph(s) after validation")

        document = self._document(req, recipient, body)
        raw_reasoning = payload.get("reasoning")
        reasoning = _collapse(raw_reasoning if isinstance(raw_reasoning, str) else "")
        return CoverLetterResult(
            body=body,
            document=document,
            tone=req.tone or DEFAULT_TONE,
            reasoning=reasoning,
            token_usage=usage,
            cached=False,
            degraded=False,
        )

    def _resolve_recipient(self, req: CoverLetterRequest, proposed: Any) -> str:
        """Return who the letter is addressed to, never guessing a name.

        Args:
            req: The letter request.
            proposed: The recipient the model returned.

        Returns:
            The caller's known recipient when it supplied one; otherwise the model's, but
            only when the posting text actually contains that name; otherwise
            :data:`DEFAULT_RECIPIENT`.
        """
        known = (req.recipient or "").strip()
        if known:
            return known[:MAX_RECIPIENT_CHARS]

        candidate = _collapse(proposed if isinstance(proposed, str) else "")[:MAX_RECIPIENT_CHARS]
        if not candidate or candidate.casefold() == DEFAULT_RECIPIENT.casefold():
            return DEFAULT_RECIPIENT
        if candidate.casefold() in req.posting_text().casefold():
            return candidate
        logger.info("cover_letter.recipient_rejected", recipient=candidate)
        return DEFAULT_RECIPIENT

    @staticmethod
    def replace_placeholders(body: str, req: CoverLetterRequest, recipient: str) -> str:
        """Replace every template placeholder in *body* with a real value, or remove it.

        Models leave ``[Company]``, ``{{role}}`` and ``<Hiring Manager>`` behind even when
        told not to, and a letter that reaches an employer carrying one is worse than no
        letter at all. Known placeholder names are filled from the request; unknown ones are
        deleted outright rather than unwrapped, because printing the *name* of a placeholder
        ("Dear Manager Name,") is no better than printing its brackets.

        Args:
            body: The letter body.
            req: The letter request, supplying the real values.
            recipient: The already-resolved recipient.

        Returns:
            The body with placeholders resolved and whitespace tidied.
        """
        values = {
            "company": req.company(),
            "role": req.role(),
            "recipient": recipient,
            "sender": (req.user.full_name or "").strip(),
            "email": (req.user.email or "").strip(),
            "phone": (req.user.phone or "").strip(),
            "location": (req.posting.location or req.user.location or "").strip(),
            "date": _today(),
        }
        replaced: list[str] = []

        def _substitute(match: re.Match[str]) -> str:
            raw = next((group for group in match.groups() if group), "")
            normalized = _PLACEHOLDER_NOISE.sub(" ", raw.casefold()).strip()
            field_name = _PLACEHOLDER_FIELDS.get(normalized)
            replacement = values.get(field_name or "", "") if field_name else ""
            replaced.append(raw)
            return replacement

        result = PLACEHOLDER_PATTERN.sub(_substitute, body or "")
        if replaced:
            logger.warning(
                "cover_letter.placeholder_replaced",
                count=len(replaced),
                placeholders=sorted(set(replaced))[:MAX_PARAGRAPHS],
            )
        return _tidy(result)

    @staticmethod
    def strip_unsupported_numbers(body: str, req: CoverLetterRequest) -> str:
        """Delete any sentence carrying a number that neither source document contains.

        The résumé's bullets have already been validated against the knowledge graph, and the
        posting is the employer's own words, so their union is the complete set of numbers a
        letter may state. Deletion is per *sentence* rather than per letter: one invented
        figure should cost one claim, not the whole document.

        Args:
            body: The letter body.
            req: The letter request, supplying the two source documents.

        Returns:
            The body with unsupported sentences removed and empty paragraphs dropped.
        """
        supported = numbers_in(render_resume_text(req.resume)) | numbers_in(req.posting_text())
        kept_paragraphs: list[str] = []
        removed = 0

        for paragraph in _paragraphs(body):
            sentences = _SENTENCE_SPLIT.split(paragraph)
            kept: list[str] = []
            for sentence in sentences:
                invented = numbers_in(sentence) - supported
                if invented:
                    removed += 1
                    logger.warning(
                        "cover_letter.unsupported_metric",
                        numbers=sorted(invented),
                        sentence=sentence[:120],
                    )
                    continue
                kept.append(sentence)
            joined = " ".join(part for part in kept if part.strip()).strip()
            if joined:
                kept_paragraphs.append(joined)

        if removed:
            logger.info("cover_letter.sentences_removed", removed=removed)
        return "\n\n".join(kept_paragraphs)

    @staticmethod
    def enforce_length(body: str, req: CoverLetterRequest) -> str:
        """Trim *body* to :data:`MAX_PARAGRAPHS` and the request's word budget.

        Paragraphs go first, from the end, because that is where a model puts its weakest
        material and because losing a whole paragraph reads better than a letter that stops
        mid-argument. Only if the letter is still over budget are trailing sentences dropped
        from the last surviving paragraph.

        Args:
            body: The letter body.
            req: The letter request, supplying the word budget.

        Returns:
            The trimmed body.
        """
        budget = req.word_budget()
        paragraphs = _paragraphs(body)[:MAX_PARAGRAPHS]

        while paragraphs and _word_count(paragraphs) > budget and len(paragraphs) > MIN_PARAGRAPHS:
            paragraphs.pop()

        if paragraphs and _word_count(paragraphs) > budget:
            sentences = _SENTENCE_SPLIT.split(paragraphs[-1])
            while (
                len(sentences) > 1 and _word_count([*paragraphs[:-1], " ".join(sentences)]) > budget
            ):
                sentences.pop()
            paragraphs[-1] = " ".join(sentences).strip()

        trimmed = "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())
        if _word_count([trimmed]) < _word_count(_paragraphs(body)):
            logger.info(
                "cover_letter.trimmed",
                budget=budget,
                words=_word_count([trimmed]),
                paragraphs=len(paragraphs),
            )
        return trimmed

    # ----------------------------------------------------------------------------------
    # Deterministic fallback
    # ----------------------------------------------------------------------------------

    def fallback_letter(self, req: CoverLetterRequest) -> CoverLetterResult:
        """Assemble a letter with no language model at all.

        Every sentence is either boilerplate about the *application* — which asserts nothing
        about the applicant or the employer — or a line lifted verbatim from the tailored
        résumé, which has already been validated against the knowledge graph. It therefore
        cannot fail its own number check, cannot contain a placeholder, and cannot claim
        anything the résumé does not.

        It reads plainer than a generated letter. It is a letter a person could have written
        in five minutes, and it is always available.

        Args:
            req: The letter request.

        Returns:
            The result, with ``degraded=True`` and zero token usage.
        """
        recipient = (req.recipient or "").strip() or DEFAULT_RECIPIENT
        role = req.role() or "the advertised role"
        company = req.company()
        opening = (
            f"I am applying for {role} at {company}." if company else f"I am applying for {role}."
        )

        highlights = _resume_highlights(req.resume, MAX_PARAGRAPHS - 1)
        paragraphs = [opening]
        if req.resume.summary:
            paragraphs[0] = f"{opening} {req.resume.summary}"
        if highlights:
            paragraphs.append(
                "The most relevant work on the attached résumé: " + " ".join(highlights)
            )
        if req.resume.skills_line:
            paragraphs.append(f"The tools and areas this draws on: {req.resume.skills_line}.")
        paragraphs.append(
            "The attached résumé has the full detail, and I would be glad to talk it through."
        )

        body = self.enforce_length("\n\n".join(paragraphs), req)
        logger.info(
            "cover_letter.fallback",
            posting=str(req.posting.id or ""),
            words=len(body.split()),
        )
        return CoverLetterResult(
            body=body,
            document=self._document(req, recipient, body),
            tone=req.tone or DEFAULT_TONE,
            reasoning=(
                "Assembled without a language model from the tailored résumé's own summary, "
                "strongest bullets and skills line."
            ),
            token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            cached=False,
            degraded=True,
        )

    # ----------------------------------------------------------------------------------
    # Document and cache
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _document(req: CoverLetterRequest, recipient: str, body: str) -> CoverLetterDocument:
        """Wrap a validated body in the renderable document model.

        Args:
            req: The letter request.
            recipient: The resolved recipient.
            body: The validated body.

        Returns:
            The document, sharing its contact block with the résumé so the pair prints as a
            set. The profile is only consulted when the résumé's own block is empty, which
            happens when a caller assembled the résumé by hand.
        """
        contact = req.resume.contact
        if not (contact.name or contact.contact_line() or contact.has_links):
            contact = Contact(
                name=req.user.full_name or "",
                email=req.user.email or "",
                phone=req.user.phone or "",
                location=req.user.location or "",
            )
        return CoverLetterDocument(
            contact=contact,
            recipient=recipient,
            company=req.company(),
            role=req.role(),
            body=body,
            date=_today(),
        )

    def _cache_key(self, req: CoverLetterRequest) -> str:
        """Return the content-addressed key for one letter.

        Keyed on the résumé's own content hash rather than on the posting alone: two postings
        can share a description, and re-tailoring the résumé must produce a new letter.

        Args:
            req: The letter request.

        Returns:
            The cache key.
        """
        posting = req.posting
        posting_hash = posting.content_hash or hash_payload(
            {
                "provider": posting.provider.value,
                "external_id": posting.external_id,
                "title": posting.title,
                "company": posting.company_name,
                "description": posting.description,
            }
        )
        return make_key(
            NAMESPACES.LLM,
            CACHE_DISCRIMINATOR,
            str(getattr(req.user, "user_id", "") or ""),
            posting_hash,
            req.resume.content_hash(),
            hash_payload(req.prefs),
            req.tone or DEFAULT_TONE,
            req.word_budget(),
            (req.recipient or "").strip(),
            getattr(self.llm, "model", ""),
        )

    async def _cache_read(self, key: str, req: CoverLetterRequest) -> CoverLetterResult | None:
        """Read a stored letter, tolerating a stale or unreadable entry.

        Args:
            key: The cache key.
            req: The letter request, used to rebuild the document wrapper.

        Returns:
            The result with ``cached=True``, or ``None`` on a miss or an unusable entry.
        """
        try:
            payload = await self.cache.get(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cover_letter.cache_unavailable", error=str(exc))
            return None
        if not isinstance(payload, Mapping):
            return None
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            return None
        recipient = payload.get("recipient")
        recipient = recipient if isinstance(recipient, str) and recipient else DEFAULT_RECIPIENT
        return CoverLetterResult(
            body=body,
            document=self._document(req, recipient, body),
            tone=str(payload.get("tone") or req.tone or DEFAULT_TONE),
            reasoning=str(payload.get("reasoning") or ""),
            token_usage={
                str(name): int(value)
                for name, value in (payload.get("token_usage") or {}).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            cached=True,
            degraded=bool(payload.get("degraded", False)),
        )

    async def _cache_write(self, key: str, result: CoverLetterResult) -> None:
        """Store a letter, tolerating an unavailable cache.

        Args:
            key: The cache key.
            result: The result to store.
        """
        payload = {
            "body": result.body,
            "recipient": result.document.recipient,
            "tone": result.tone,
            "reasoning": result.reasoning,
            "token_usage": dict(result.token_usage),
            "degraded": result.degraded,
        }
        try:
            await self.cache.set(key, payload, ttl=LETTER_CACHE_TTL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cover_letter.cache_write_failed", error=str(exc))


# ======================================================================================
# Module-private helpers
# ======================================================================================


def _today() -> str:
    """Return today's UTC date, ISO-8601, as the letter should print it.

    Timezone-aware on purpose: a worker in one region must not date a letter differently from
    the API process that queued it, and the date is persisted on the document.

    Returns:
        ``"YYYY-MM-DD"``.
    """
    return dt.datetime.now(dt.UTC).date().isoformat()


def render_resume_text(resume: ResumeDocument) -> str:
    """Render a résumé as the plain text the model and the validators both read.

    One string serves three purposes, which is why it is a function rather than three: it is
    what the prompt shows the model, it is the corpus
    :meth:`CoverLetterWriter.strip_unsupported_numbers` checks numbers against, and it is what
    :func:`_resume_highlights` draws the fallback letter's evidence from. Deriving all three
    from the same rendering means the model cannot be shown a number the validator will then
    reject.

    Args:
        resume: The tailored résumé.

    Returns:
        A plain-text rendering: summary, then each section's entries with their header line
        and bullets, then the skills line.
    """
    lines: list[str] = []
    if resume.summary:
        lines.append(resume.summary)
    for section in resume.sections:
        if not section.entries:
            continue
        lines.append("")
        lines.append(section.heading or "")
        for entry in section.entries:
            header = " — ".join(part for part in entry.header_rows() if part)
            if header:
                lines.append(header)
            lines.extend(f"  • {bullet}" for bullet in entry.bullets)
    if resume.skills_line:
        lines.append("")
        lines.append(f"Skills: {resume.skills_line}")
    return "\n".join(line for line in lines).strip()


def _resume_highlights(resume: ResumeDocument, limit: int) -> list[str]:
    """Return the résumé's leading bullets, for the deterministic letter.

    Args:
        resume: The tailored résumé.
        limit: How many bullets to take.

    Returns:
        Up to *limit* bullets in document order — which is impact order, because the résumé
        engine sorted its entries by impact — each ended with a full stop so they concatenate
        into readable prose.
    """
    picked: list[str] = []
    for _s, _e, _b, text in resume.iter_bullets():
        cleaned = _collapse(text)
        if not cleaned:
            continue
        picked.append(cleaned if cleaned.endswith((".", "!", "?")) else f"{cleaned}.")
        if len(picked) >= max(0, limit):
            break
    return picked


def _paragraphs(body: str) -> list[str]:
    """Split a letter body into non-empty paragraphs.

    Args:
        body: The body, with paragraphs separated by blank lines.

    Returns:
        The paragraphs, each with internal line breaks collapsed to single spaces.
    """
    chunks = _PARAGRAPH_BREAK.split((body or "").replace("\r\n", "\n"))
    return [_collapse(chunk) for chunk in chunks if chunk.strip()]


def _tidy(body: str) -> str:
    """Normalise whitespace left behind by placeholder removal.

    Args:
        body: The body.

    Returns:
        The body with paragraph breaks preserved, runs of spaces collapsed, and no space
        stranded before a comma or full stop.
    """
    paragraphs = _paragraphs(body)
    cleaned = [re.sub(r"\s+([,.;:!?])", r"\1", paragraph).strip() for paragraph in paragraphs]
    return "\n\n".join(paragraph for paragraph in cleaned if paragraph)


def _collapse(text: str) -> str:
    """Collapse all whitespace in *text* to single spaces and trim it.

    Args:
        text: Any text.

    Returns:
        The collapsed text.
    """
    return _WHITESPACE.sub(" ", text or "").strip()


def _word_count(parts: Sequence[str]) -> int:
    """Return the total word count across *parts*.

    Args:
        parts: Paragraphs or sentences.

    Returns:
        The number of whitespace-separated words.
    """
    return sum(len(part.split()) for part in parts)
