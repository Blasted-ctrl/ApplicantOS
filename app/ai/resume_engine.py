"""Résumé generation — a tailored document assembled as a *view* of the knowledge graph.

This is the product. Everything upstream of it (analyzers, the indexer, the vector store,
retrieval) exists to put the right :class:`~app.models.knowledge.KnowledgeFact` rows in
front of :meth:`ResumeEngine.tailor`, and everything downstream of it (the renderer, the
browser, the pipeline) exists to get the result in front of an employer. ``docs/CONTRACTS.md``
§10 fixes its shape; golden rules #6 and #7 fix its ethics.

**A résumé here is generated, never stored.** There is no master document to edit. There are
facts, and there is a request, and this module composes one from the other. That is what makes
a résumé re-tailorable for every posting without the user maintaining nine variants by hand,
and what makes ``ResumeVersion.content_json`` worth retaining forever while the rendered PDF
stays disposable.

**The model may only select and rewrite.** The pipeline is deliberately shaped so that the
language model never gets to be the source of a claim:

1. :meth:`ResumeEngine.prefilter` retrieves facts from the user's own graph and ranks them by
   embedding similarity to the posting, keyword overlap, and the extractor's ``impact_score``.
2. The prompt renders each surviving fact as one line, id first.
3. The model returns bullets, each carrying the ``fact_id`` it rewrote.
4. :meth:`ResumeEngine.validate` throws away everything that cannot be traced back:

   * a ``fact_id`` outside the prefiltered set is **dropped** (``resume_engine.hallucinated_fact``);
   * a rewrite sharing less than :data:`MIN_REWRITE_OVERLAP` of its content words with its
     source fact is **reverted to the fact's own text** (``resume_engine.rewrite_rejected``);
   * a number that appears in neither the fact's text nor its ``metrics`` **reverts the whole
     bullet** (``resume_engine.unsupported_metric``);
   * organisation, role, location and dates are **copied from the source fact**, overwriting
     whatever the model returned, always;
   * a skill on the skills line that no selected fact evidences is **stripped**
     (``resume_engine.unsupported_skill``);
   * a summary that is not grounded in the facts and the posting is **discarded**
     (``resume_engine.summary_rejected``).

Every threshold is a named constant in this module, because they are the numbers that decide
what a stranger reads about the user.

**It never stops the pipeline.** :meth:`ResumeEngine.tailor` degrades to
:meth:`ResumeEngine.fallback_tailor` — a deterministic, LLM-free ranking that groups facts by
organisation and prints their original text verbatim — on *any* model failure: no API key, a
rate limit, a budget exhaustion, an unparseable reply, a schema the model ignored. A résumé
composed of the user's own sentences in impact order is a perfectly good résumé. An outage is
not a reason to skip a posting.

Results are cached on ``(user, posting content, preferences, fact set)``, so re-running a
pipeline over the same posting is free and re-tailoring after the knowledge graph changes is
not.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog

from app.ai.embeddings import content_tokens, cosine, embed_texts
from app.ai.prompts import load_prompt
from app.cache import NAMESPACES, hash_payload, make_key
from app.documents.models import (
    PRIORITIES_META_KEY,
    Contact,
    ResumeDocument,
    ResumeEntry,
    ResumeSection,
)
from app.models.enums import FactKind

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ai.llm import ModelPlugin
    from app.cache import Cache
    from app.jobs.base import JobPostingDTO, UserProfileDTO
    from app.knowledge.retrieval import KnowledgeRetriever
    from app.models.knowledge import KnowledgeFact
    from app.models.user import UserPreferences

__all__ = [
    "DEFAULT_MAX_BULLETS",
    "DEFAULT_PREFILTER_TOP_K",
    "EXPERIENCE_HEADING",
    "IMPACT_WEIGHT",
    "KEYWORD_WEIGHT",
    "MAX_IMPACT_SCORE",
    "MAX_SKILLS_ON_LINE",
    "MIN_REWRITE_OVERLAP",
    "MIN_SUMMARY_GROUNDING",
    "MIN_TITLE_GROUNDING",
    "ONGOING_DATE_LABEL",
    "PROJECTS_HEADING",
    "RESUME_TAILOR_SCHEMA",
    "SECTION_FOR_KIND",
    "SECTION_ORDER",
    "SIMILARITY_WEIGHT",
    "TAILOR_CACHE_TTL_SECONDS",
    "ResumeEngine",
    "TailorRequest",
    "TailorResult",
    "numbers_in",
    "token_overlap",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

# -- validation thresholds (the numbers that enforce golden rule #7) --------------------

#: **The anti-hallucination threshold.** The fraction of a rewritten bullet's content words
#: that must also occur in its source fact. Below this the rewrite is discarded and the
#: fact's own text is printed instead.
#:
#: 0.35 is deliberately permissive about *style* and strict about *substance*. A genuine
#: tightening — "Wrote the motor-control firmware in C++ for our STM32H7 drive board" becoming
#: "Wrote motor-control firmware in C++ for an STM32H7 drive board" — keeps every noun and
#: scores near 1.0. A sentence invented "in the spirit of" the fact keeps the connective
#: tissue and loses the nouns, and lands far below. Fixed by ``docs/CONTRACTS.md`` §10.
MIN_REWRITE_OVERLAP: Final[float] = 0.35

#: Grounding required of an entry's title before the model's version is used at all. Titles
#: are only ever taken from the model when the source fact states no role — a project name,
#: typically — and then only when the words are already in the fact. Higher than
#: :data:`MIN_REWRITE_OVERLAP` because a title is two or three words, so a low bar would
#: accept anything.
MIN_TITLE_GROUNDING: Final[float] = 0.6

#: A single-word title is only accepted when it is capitalised, because a project *name* is a
#: proper noun ("PoseNet", "Bench Logger") while a bare lowercase word lifted out of the fact
#: ("embedded", "firmware") is a topic and reads as a formatting bug on the page.
MIN_TITLE_WORDS: Final[int] = 2

#: Grounding required of the professional summary against the selected facts plus the
#: posting. A summary is the one piece of prose with no single source fact behind it, so it
#: is measured against the whole corpus it is allowed to draw on.
MIN_SUMMARY_GROUNDING: Final[float] = 0.5

# -- prefilter ranking ------------------------------------------------------------------

#: Facts :meth:`ResumeEngine.prefilter` returns by default, matching
#: :meth:`app.knowledge.retrieval.KnowledgeRetriever.retrieve_for_posting`'s own default.
#: Wide enough that the model has real choices, narrow enough to fit a prompt.
DEFAULT_PREFILTER_TOP_K: Final[int] = 60

#: Weights of the three prefilter signals. They sum to 1.0 so the composite score stays in
#: ``[0, 1]`` and is readable in a log line.
#:
#: Similarity leads because it is the only signal that crosses vocabulary — a posting saying
#: "real-time embedded control" and a fact saying "1 kHz PID loop on an STM32" share no word.
#: Keyword overlap is the corrective: it catches the product name, acronym or employer that an
#: embedder smooths away. Impact is a tiebreak, not a ranking — a spectacular fact about the
#: wrong subject still does not belong on this résumé.
SIMILARITY_WEIGHT: Final[float] = 0.5
KEYWORD_WEIGHT: Final[float] = 0.3
IMPACT_WEIGHT: Final[float] = 0.2

#: Upper end of ``KnowledgeFact.impact_score``'s 0–100 scale, used to normalise it into the
#: same ``[0, 1]`` currency as the other two signals.
MAX_IMPACT_SCORE: Final[int] = 100

# -- prompt shaping ---------------------------------------------------------------------

#: Characters of the posting body folded into the prompt. Past this a posting is legal
#: boilerplate and benefits copy, which costs tokens and dilutes the instruction.
MAX_POSTING_CHARS: Final[int] = 6000

#: Characters of one fact's text rendered on its prompt line. Facts are atomic by
#: construction; this only guards against a pathological extraction.
MAX_FACT_TEXT_CHARS: Final[int] = 400

#: Skills or technologies listed per fact line.
MAX_FACT_SKILLS: Final[int] = 8

#: Placeholder printed for a fact field the source never stated.
ABSENT_FIELD: Final[str] = "—"

#: Separator between the fields of one rendered fact line.
FACT_FIELD_SEPARATOR: Final[str] = " | "

#: Temperature for tailoring. Zero, which is what makes the call cacheable in
#: :class:`app.ai.llm.GuardedModelPlugin` and what makes two runs over the same posting
#: produce the same résumé — a person comparing versions must be seeing real changes.
TAILOR_TEMPERATURE: Final[float] = 0.0

#: Completion budget for one tailoring call. A full résumé of JSON is comfortably inside this.
TAILOR_MAX_TOKENS: Final[int] = 4096

# -- document shaping -------------------------------------------------------------------

#: Bullets one résumé may carry, per ``docs/CONTRACTS.md`` §10.
DEFAULT_MAX_BULLETS: Final[int] = 18

#: Résumé template used when a request does not name one.
DEFAULT_TEMPLATE: Final[str] = "modern"

#: Skills printed on the skills line. Past this it stops being read.
MAX_SKILLS_ON_LINE: Final[int] = 20

#: Skills below which the line is topped up from the selected facts' own ``skills`` and
#: ``technologies``. A validator that strips most of a model's list leaves a one-word skills
#: line, which looks broken; topping up from the facts is not fabrication, because those are
#: exactly the values the surviving bullets already evidence.
MIN_SKILLS_ON_LINE: Final[int] = 5

#: Separator between skills on the skills line.
SKILLS_SEPARATOR: Final[str] = ", "

#: Longest section heading accepted from the model.
MAX_HEADING_CHARS: Final[int] = 48

#: Longest entry title accepted.
MAX_TITLE_CHARS: Final[int] = 120

#: Longest bullet accepted. A bullet longer than this wraps to three lines and stops being a
#: bullet; the source fact's own text is used instead.
MAX_BULLET_CHARS: Final[int] = 400

#: Printed between the two ends of a date range.
DATE_RANGE_SEPARATOR: Final[str] = " – "

#: Printed as the end of a range whose source fact states a start and no end. The fact
#: extractor's contract (``app/ai/prompts/extract_facts.system.md``) defines a null
#: ``date_end`` as ongoing work, so this is reading the schema, not guessing a date.
ONGOING_DATE_LABEL: Final[str] = "Present"

#: Canonical section headings, in print order.
EXPERIENCE_HEADING: Final[str] = "Experience"
PROJECTS_HEADING: Final[str] = "Projects"
EDUCATION_HEADING: Final[str] = "Education"
LEADERSHIP_HEADING: Final[str] = "Leadership"
AWARDS_HEADING: Final[str] = "Awards"
PUBLICATIONS_HEADING: Final[str] = "Publications"

#: The order sections print in. A heading the model invents is kept but sorts last, so an
#: unexpected-but-harmless heading does not displace Experience from the top of the page.
SECTION_ORDER: Final[tuple[str, ...]] = (
    EXPERIENCE_HEADING,
    PROJECTS_HEADING,
    EDUCATION_HEADING,
    LEADERSHIP_HEADING,
    AWARDS_HEADING,
    PUBLICATIONS_HEADING,
)

#: Which section a fact belongs under, by kind. The work kinds are absent because they are
#: decided by whether the fact names an organisation — employment goes under Experience,
#: unaffiliated work goes under Projects — which is a property of the row, not of its kind.
SECTION_FOR_KIND: Final[dict[FactKind, str]] = {
    FactKind.EDUCATION_ITEM: EDUCATION_HEADING,
    FactKind.LEADERSHIP_ITEM: LEADERSHIP_HEADING,
    FactKind.AWARD: AWARDS_HEADING,
    FactKind.PUBLICATION_ITEM: PUBLICATIONS_HEADING,
}

# -- caching -----------------------------------------------------------------------------

#: Discriminator on the cache key, so tailoring results cannot collide with the raw model
#: completions that share the ``llm`` namespace.
CACHE_DISCRIMINATOR: Final[str] = "resume_tailor"

#: TTL for a cached tailoring. ``None`` defers to ``settings.cache_default_ttl``; the key
#: already covers the fact set, so a knowledge change invalidates precisely (golden rule #9).
TAILOR_CACHE_TTL_SECONDS: Final[int | None] = None

# -- text analysis ------------------------------------------------------------------------

#: A number as it appears in prose: digits, with optional thousands separators or decimals.
#: Matched greedily so ``1,200.5`` is one number rather than three.
_NUMBER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d+(?:[.,]\d+)*")

#: Trailing separators trimmed from a captured number before comparison, so ``"4 ms."``
#: and ``"4 ms"`` yield the same token.
_NUMBER_TRAILING: Final[str] = ".,"

#: Collapsed whitespace.
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Splits a comma/semicolon/slash-separated skills line into items.
_SKILL_SPLIT: Final[re.Pattern[str]] = re.compile(r"[,;/|]|\s+·\s+")

#: JSON Schema for the tailoring reply, passed to
#: :meth:`app.ai.llm.ModelPlugin.complete_json`. Mirrors the schema block in
#: ``resume_tailor.system.md``; the two must be edited together.
RESUME_TAILOR_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["summary", "sections", "skills_line", "reasoning"],
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences supported by the selected facts, or empty.",
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["heading", "entries"],
                "additionalProperties": False,
                "properties": {
                    "heading": {"type": "string", "description": "Section heading."},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["fact_ids", "bullets"],
                            "additionalProperties": False,
                            "properties": {
                                "fact_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Every supplied fact id used here.",
                                },
                                "title": {"type": "string"},
                                "organization": {"type": "string"},
                                "location": {"type": "string"},
                                "date_range": {"type": "string"},
                                "bullets": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["fact_id", "text"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "fact_id": {
                                                "type": "string",
                                                "description": "Id of the one fact rewritten.",
                                            },
                                            "text": {
                                                "type": "string",
                                                "description": "The rewritten bullet.",
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
        "skills_line": {"type": "string"},
        "reasoning": {"type": "string"},
    },
}


# ======================================================================================
# Text helpers
# ======================================================================================


def token_overlap(candidate: str, source: str) -> float:
    """Return the fraction of *candidate*'s content words that also occur in *source*.

    The hallucination test, and the reason :data:`MIN_REWRITE_OVERLAP` is expressible as a
    single number. A faithful rewrite keeps the nouns; an invention keeps only the grammar.
    Tokenisation is :func:`app.ai.embeddings.content_tokens`, so English function words are
    ignored and technical identifiers (``c++``, ``node.js``, ``ci/cd``) survive whole.

    Args:
        candidate: Text the model produced.
        source: The text it was given.

    Returns:
        A value in ``[0.0, 1.0]``. ``0.0`` when the candidate has no content words at all,
        which is the safe answer — an empty rewrite must never pass.
    """
    candidate_tokens = set(content_tokens(candidate))
    if not candidate_tokens:
        return 0.0
    return len(candidate_tokens & set(content_tokens(source))) / len(candidate_tokens)


def numbers_in(text: str) -> set[str]:
    """Return the distinct numeric tokens *text* contains, normalised for comparison.

    Thousands separators are removed and trailing punctuation trimmed, so ``"1,200"``,
    ``"1200"`` and ``"1200."`` all reduce to ``"1200"`` and a bullet quoting a fact's number
    in a different typographic style is not accused of inventing it.

    Args:
        text: Any text.

    Returns:
        The normalised numbers. Empty when *text* contains none, which is the common — and
        entirely acceptable — case for a résumé bullet.
    """
    found: set[str] = set()
    for raw in _NUMBER_PATTERN.findall(text or ""):
        cleaned = raw.replace(",", "").strip(_NUMBER_TRAILING)
        if cleaned:
            found.add(cleaned)
    return found


def _one_line(text: str | None, limit: int) -> str:
    """Collapse *text* onto one line and truncate it.

    Args:
        text: Any text, possibly ``None``.
        limit: Maximum characters to keep.

    Returns:
        The whitespace-collapsed, trimmed excerpt.
    """
    collapsed = _WHITESPACE.sub(" ", (text or "")).strip()
    return collapsed[:limit].strip()


# ======================================================================================
# Contract dataclasses
# ======================================================================================


@dataclass(slots=True)
class TailorRequest:
    """One "write me a résumé for this posting" request (``docs/CONTRACTS.md`` §10).

    Attributes:
        user: The applicant, detached from the database. Supplies the contact block and, via
            :attr:`~app.jobs.base.UserProfileDTO.user_id`, the knowledge graph to retrieve
            from. A request whose ``user_id`` is ``None`` can still be rendered, but it has
            no facts to draw on and degrades to an empty document.
        posting: The job being applied to. Its title and description are the retrieval query
            and the tailoring target.
        prefs: The user's policy. Part of the cache key, because changing the template or the
            variant must produce a different document rather than a stale hit.
        template: Résumé template name, passed through to
            :func:`app.documents.render_resume` and recorded in the document's metadata.
        max_bullets: Ceiling on bullets in the finished document. Enforced after validation
            by dropping the lowest-``impact_score`` bullets.
        variant_label: Optional label distinguishing two résumés for the same posting, e.g.
            ``"embedded"`` versus ``"ml"``.
    """

    user: UserProfileDTO
    posting: JobPostingDTO
    prefs: UserPreferences
    template: str = DEFAULT_TEMPLATE
    max_bullets: int = DEFAULT_MAX_BULLETS
    variant_label: str | None = None

    def posting_text(self) -> str:
        """Return the posting rendered as the text retrieval and tailoring read.

        Returns:
            The title on its own first line — which is what
            :meth:`app.knowledge.retrieval.KnowledgeRetriever.retrieve_for_posting` lifts as
            the role — followed by the bounded description.
        """
        title = _one_line(self.posting.title, MAX_TITLE_CHARS)
        body = _WHITESPACE.sub(" ", self.posting.description or "").strip()
        return f"{title}\n{body[:MAX_POSTING_CHARS]}".strip()

    def bullet_budget(self) -> int:
        """Return the effective bullet ceiling, never below one.

        Returns:
            :attr:`max_bullets`, floored at 1 so a mis-set preference cannot produce a résumé
            with a contact block and nothing else.
        """
        return max(1, int(self.max_bullets or DEFAULT_MAX_BULLETS))


@dataclass(slots=True)
class TailorResult:
    """The outcome of one tailoring (``docs/CONTRACTS.md`` §10).

    Attributes:
        document: The résumé, ready to render and to persist as
            ``ResumeVersion.content_json``.
        selected_fact_ids: Every :class:`~app.models.knowledge.KnowledgeFact` id the document
            cites, in document order. Written to ``ResumeVersion.fact_ids``; this is the
            audit trail behind golden rule #7.
        reasoning: The model's account of what it chose and what it dropped, or a description
            of the deterministic ranking when the fallback path produced this. Written to
            ``ResumeVersion.reasoning`` and shown in the desktop app.
        token_usage: ``input_tokens`` / ``output_tokens`` / ``total_tokens`` for the call.
            Estimated with the model's own tokenizer, because
            :meth:`app.ai.llm.ModelPlugin.complete_json` returns a decoded object rather than
            an :class:`~app.ai.llm.LLMResponse` and therefore surfaces no provider counts.
            All zero on the fallback path, which makes no call.
        cached: Whether this result was served from :mod:`app.cache` rather than generated.
        degraded: Whether the LLM path failed and :meth:`ResumeEngine.fallback_tailor`
            produced this. Appended to the contract's fields so the pipeline can surface
            "written offline" in the UI without inspecting :attr:`token_usage`.
    """

    document: ResumeDocument
    selected_fact_ids: list[str] = field(default_factory=list)
    reasoning: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)
    cached: bool = False
    degraded: bool = False

    def bullet_count(self) -> int:
        """Return how many bullets the finished document carries."""
        return self.document.total_bullets()


# ======================================================================================
# Internal working types
# ======================================================================================


@dataclass(slots=True)
class _Bullet:
    """One validated bullet, still bound to the fact it came from.

    Attributes:
        fact: The source fact. Never ``None`` — a bullet without one has already been
            dropped by :meth:`ResumeEngine.validate`.
        text: The text that will be printed: the model's rewrite when it survived
            validation, otherwise the fact's own words.
        rewritten: Whether the model's wording survived. Recorded for the generation log.
    """

    fact: KnowledgeFact
    text: str
    rewritten: bool

    @property
    def fact_id(self) -> str:
        """The source fact's id, as the string form the document model stores."""
        return str(self.fact.id)

    @property
    def impact(self) -> int:
        """The source fact's impact score, which is what ranks this bullet when shrinking."""
        return int(self.fact.impact_score or 0)


@dataclass(slots=True)
class _Group:
    """Bullets that share one (organisation, role, period) header.

    Attributes:
        key: The identity the group was formed on.
        heading: The section this group prints under.
        bullets: The group's bullets, in the order they were selected.
        title_hint: The model's proposed title, kept only until it has been validated
            against the source facts.
    """

    key: tuple[str, str, str, str]
    heading: str
    bullets: list[_Bullet] = field(default_factory=list)
    title_hint: str = ""

    @property
    def lead(self) -> KnowledgeFact:
        """The fact whose header fields the whole group prints.

        Returns:
            The highest-impact fact in the group. Every fact in a group agrees on
            organisation, role and dates by construction, so this only decides which row
            supplies ``location`` when they disagree.
        """
        return max(self.bullets, key=lambda item: item.impact).fact

    @property
    def rank(self) -> int:
        """The group's best impact score, used to order entries within a section."""
        return max((bullet.impact for bullet in self.bullets), default=0)


# ======================================================================================
# The engine
# ======================================================================================


class ResumeEngine:
    """Generates a tailored :class:`~app.documents.models.ResumeDocument` (§10).

    Bound to one session, one model client, one retriever and one cache for its lifetime;
    every request is passed in. Stateless between calls, so a single instance is safe to
    share across a pipeline run.

    Usage::

        engine = ResumeEngine(session, get_llm("reasoning"), retriever, get_cache())
        result = await engine.tailor(TailorRequest(user=dto, posting=posting, prefs=prefs))
        await render_resume(result.document, out_path, template=req.template)
    """

    def __init__(
        self,
        session: AsyncSession,
        llm: ModelPlugin,
        retriever: KnowledgeRetriever,
        cache: Cache,
    ) -> None:
        """Bind the engine to its collaborators.

        Args:
            session: The async session the retriever reads through. Held so that callers
                needing to persist a :class:`~app.models.resume.ResumeVersion` alongside a
                result share one unit of work; this class itself never writes.
            llm: The model client. Any failure it raises is caught and degraded — the engine
                never lets a model outage stop a pipeline run.
            retriever: The knowledge retriever. **The only source of résumé content.**
            cache: The cache tailoring results are read from and written to.
        """
        self.session = session
        self.llm = llm
        self.retriever = retriever
        self.cache = cache

    # ----------------------------------------------------------------------------------
    # Prefilter
    # ----------------------------------------------------------------------------------

    async def prefilter(
        self, req: TailorRequest, top_k: int = DEFAULT_PREFILTER_TOP_K
    ) -> list[KnowledgeFact]:
        """Return the facts this posting should be written from, best first.

        Retrieval (:meth:`~app.knowledge.retrieval.KnowledgeRetriever.retrieve_for_posting`)
        answers "what is relevant?" with a fused hybrid ranking. This answers the narrower
        question the résumé actually needs — "what is worth a line?" — by re-scoring the
        retrieved rows against three signals that a résumé cares about and retrieval does not
        weight the same way:

        * **Cosine similarity** between the posting and the fact's stored embedding, which is
          the only signal that survives a vocabulary mismatch.
        * **Keyword overlap** with the posting's content words, computed over the fact's text,
          organisation, role, skills and technologies — the exact-token corrective.
        * **Impact**, normalised from ``KnowledgeFact.impact_score``, as a tiebreak.

        Everything degrades. With no embedder or no stored vectors the similarity term is
        zero and the other two rank alone, which is why this works with
        ``EMBEDDING_PROVIDER=hashing`` and with no vectors at all.

        Args:
            req: The tailoring request.
            top_k: How many facts to return.

        Returns:
            At most *top_k* facts, highest composite score first. Empty when the user has no
            facts, when the posting has no text, or when the request carries no ``user_id`` —
            each of which is a legitimate state, not an error.
        """
        user_id = getattr(req.user, "user_id", None)
        if user_id is None:
            logger.warning("resume_engine.no_user_id", posting=str(req.posting.id or ""))
            return []

        posting_text = req.posting_text()
        if not posting_text:
            logger.warning("resume_engine.empty_posting", posting=str(req.posting.id or ""))
            return []

        limit = max(1, int(top_k))
        retrieved = await self.retriever.retrieve_for_posting(
            user_id, posting_text, k_facts=limit
        )
        facts = list(retrieved.facts)
        if not facts:
            logger.info("resume_engine.no_facts_retrieved", user_id=str(user_id))
            return []

        query_vector = await self._posting_vector(posting_text)
        keywords = set(content_tokens(posting_text))

        scored = [
            (self._prefilter_score(fact, query_vector, keywords), fact) for fact in facts
        ]
        scored.sort(key=lambda item: (-item[0], -int(item[1].impact_score or 0), str(item[1].id)))
        ranked = [fact for _score, fact in scored[:limit]]

        logger.debug(
            "resume_engine.prefiltered",
            user_id=str(user_id),
            retrieved=len(facts),
            kept=len(ranked),
            embedded=query_vector is not None,
            top_score=round(scored[0][0], 4) if scored else 0.0,
        )
        return ranked

    async def _posting_vector(self, posting_text: str) -> list[float] | None:
        """Embed the posting once, tolerating an unavailable embedding backend.

        Args:
            posting_text: The posting as :meth:`TailorRequest.posting_text` renders it.

        Returns:
            The embedding, or ``None`` when embedding is unavailable or produced nothing —
            in which case the similarity term simply drops out of the ranking.
        """
        try:
            vectors = await embed_texts([posting_text])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - ranking must survive an embedder outage
            logger.warning("resume_engine.embedding_unavailable", error=str(exc))
            return None
        if not vectors or not vectors[0] or not any(vectors[0]):
            return None
        return list(vectors[0])

    @staticmethod
    def _prefilter_score(
        fact: KnowledgeFact,
        query_vector: Sequence[float] | None,
        keywords: set[str],
    ) -> float:
        """Return one fact's composite prefilter score, in ``[0, 1]``.

        Args:
            fact: The candidate.
            query_vector: The posting embedding, or ``None``.
            keywords: The posting's content tokens.

        Returns:
            The weighted sum of similarity, keyword overlap and normalised impact. Negative
            cosines are clamped to zero: "actively unlike the posting" and "unrelated to the
            posting" are the same amount of evidence, namely none.
        """
        similarity = 0.0
        embedding = fact.embedding
        if query_vector is not None and embedding and len(embedding) == len(query_vector):
            similarity = max(0.0, cosine(query_vector, embedding))

        overlap = 0.0
        if keywords:
            haystack = " ".join(
                part
                for part in (
                    fact.text or "",
                    fact.organization or "",
                    fact.role or "",
                    " ".join(fact.skills or ()),
                    " ".join(fact.technologies or ()),
                )
                if part
            )
            fact_tokens = set(content_tokens(haystack))
            if fact_tokens:
                overlap = len(fact_tokens & keywords) / len(keywords)

        impact = min(1.0, max(0, int(fact.impact_score or 0)) / MAX_IMPACT_SCORE)
        return (
            SIMILARITY_WEIGHT * similarity
            + KEYWORD_WEIGHT * overlap
            + IMPACT_WEIGHT * impact
        )

    # ----------------------------------------------------------------------------------
    # Tailoring
    # ----------------------------------------------------------------------------------

    async def tailor(self, req: TailorRequest) -> TailorResult:
        """Generate a tailored résumé for *req*.

        The full path: prefilter, cache lookup, model call, hard validation, bullet budget,
        assembly. **It never raises on a model failure.** Anything the LLM path can go wrong
        with — no API key, a rate limit, an exhausted token budget, an unparseable reply, a
        reply that ignored the schema — is caught and answered with
        :meth:`fallback_tailor`, because a posting that goes un-applied-to because an API was
        down is a worse outcome than a résumé written from the user's own sentences.

        Args:
            req: The tailoring request.

        Returns:
            The result, with ``cached=True`` when it was served from the cache and
            ``degraded=True`` when the deterministic fallback produced it.
        """
        facts = await self.prefilter(req)
        if not facts:
            return self.fallback_tailor(req, facts)

        key = self._cache_key(req, facts)
        cached = await self._cache_read(key)
        if cached is not None:
            logger.info(
                "resume_engine.cache_hit",
                posting=str(req.posting.id or ""),
                bullets=cached.bullet_count(),
            )
            return cached

        try:
            payload, usage = await self._complete(req, facts)
            result = self._assemble(req, facts, payload, usage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any model failure degrades, never raises
            logger.warning(
                "resume_engine.llm_failed",
                posting=str(req.posting.id or ""),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return self.fallback_tailor(req, facts)

        await self._cache_write(key, result)
        logger.info(
            "resume_engine.tailored",
            posting=str(req.posting.id or ""),
            candidates=len(facts),
            selected=len(result.selected_fact_ids),
            bullets=result.bullet_count(),
            estimated_lines=result.document.estimated_lines(),
            tokens=result.token_usage.get("total_tokens", 0),
        )
        return result

    async def _complete(
        self, req: TailorRequest, facts: Sequence[KnowledgeFact]
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Ask the model to select and rewrite, and account for the call.

        Args:
            req: The tailoring request.
            facts: The prefiltered facts, which are the only material the prompt carries.

        Returns:
            The decoded reply and the estimated token usage.

        Raises:
            Exception: Whatever :meth:`app.ai.llm.ModelPlugin.complete_json` raises. The
                caller degrades; this method deliberately does not swallow anything, so the
                failure is logged with its real type.
        """
        system = load_prompt("resume_tailor.system")
        prompt = load_prompt(
            "resume_tailor.user",
            posting_title=req.posting.title or ABSENT_FIELD,
            company=req.posting.company_name or ABSENT_FIELD,
            posting_location=req.posting.location or ABSENT_FIELD,
            work_arrangement=req.posting.work_arrangement.value,
            posting=req.posting_text(),
            target_skills=self._target_skills(req),
            max_bullets=req.bullet_budget(),
            facts=self.render_facts(facts),
        )
        payload = await self.llm.complete_json(
            system=system,
            prompt=prompt,
            schema=RESUME_TAILOR_SCHEMA,
            temperature=TAILOR_TEMPERATURE,
            max_tokens=TAILOR_MAX_TOKENS,
        )
        usage = self._estimate_usage(system, prompt, payload)
        return payload, usage

    def _estimate_usage(
        self, system: str, prompt: str, payload: Mapping[str, Any]
    ) -> dict[str, int]:
        """Estimate the token cost of one tailoring call.

        :meth:`app.ai.llm.ModelPlugin.complete_json` returns a decoded object, not an
        :class:`~app.ai.llm.LLMResponse`, so the provider's own counts are not available
        here. The model client's :meth:`~app.ai.llm.ModelPlugin.count_tokens` is used
        instead, which is the same estimator the budget guard pre-flights with.

        Args:
            system: The system prompt sent.
            prompt: The user message sent.
            payload: The decoded reply.

        Returns:
            ``input_tokens``, ``output_tokens`` and ``total_tokens``.
        """
        try:
            rendered = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - payload came from json.loads
            rendered = str(payload)
        input_tokens = self.llm.count_tokens(system) + self.llm.count_tokens(prompt)
        output_tokens = self.llm.count_tokens(rendered)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    @staticmethod
    def _target_skills(req: TailorRequest) -> str:
        """Return the skills the posting names, for the prompt's guidance section.

        Args:
            req: The tailoring request.

        Returns:
            A comma-separated list, or :data:`ABSENT_FIELD` when the posting names none.
            Extraction is best-effort: a failure here costs a hint, not a résumé.
        """
        from app.knowledge.extractors import extract_skills

        try:
            skills = extract_skills(req.posting_text())
        except Exception as exc:  # noqa: BLE001 - a hint is not worth failing a generation
            logger.debug("resume_engine.skill_extraction_failed", error=str(exc))
            return ABSENT_FIELD
        return SKILLS_SEPARATOR.join(skills[:MAX_SKILLS_ON_LINE]) or ABSENT_FIELD

    # ----------------------------------------------------------------------------------
    # Prompt rendering
    # ----------------------------------------------------------------------------------

    @staticmethod
    def render_facts(facts: Sequence[KnowledgeFact]) -> str:
        """Render the prefiltered facts as the prompt's fact list.

        One fact per line, id first::

            <id> | <kind> | <organization> | <role> | <dates> | <text> | skills: … | tech: … | metrics: …

        Leading with the id is deliberate: the model must return ids, and a format that puts
        the id anywhere else invites it to summarise the line and drop the identifier. Absent
        fields print :data:`ABSENT_FIELD` rather than an empty column, so a fact with no
        employer is visibly *unaffiliated* rather than ambiguously blank.

        Args:
            facts: The facts to render, in rank order.

        Returns:
            The rendered block, one line per fact.
        """
        lines: list[str] = []
        for fact in facts:
            parts = (
                str(fact.id),
                FactKind(fact.kind).value,
                _one_line(fact.organization, MAX_TITLE_CHARS) or ABSENT_FIELD,
                _one_line(fact.role, MAX_TITLE_CHARS) or ABSENT_FIELD,
                _format_date_range(fact) or ABSENT_FIELD,
                _one_line(fact.text, MAX_FACT_TEXT_CHARS),
                "skills: " + (SKILLS_SEPARATOR.join(list(fact.skills or ())[:MAX_FACT_SKILLS]) or ABSENT_FIELD),
                "tech: " + (SKILLS_SEPARATOR.join(list(fact.technologies or ())[:MAX_FACT_SKILLS]) or ABSENT_FIELD),
                "metrics: " + (SKILLS_SEPARATOR.join(list(fact.metrics or ())[:MAX_FACT_SKILLS]) or ABSENT_FIELD),
            )
            lines.append(FACT_FIELD_SEPARATOR.join(parts))
        return "\n".join(lines)

    # ----------------------------------------------------------------------------------
    # Validation — golden rule #7
    # ----------------------------------------------------------------------------------

    def validate(
        self, payload: Mapping[str, Any], facts: Sequence[KnowledgeFact]
    ) -> list[_Group]:
        """Turn a model reply into groups of bullets that are all provably sourced.

        This is the whole trustworthiness story of the product, and it assumes the model is
        adversarial rather than merely fallible. Five checks, in order:

        1. **Id membership.** A ``fact_id`` outside *facts* is dropped and logged as
           ``resume_engine.hallucinated_fact``. There is no repair — a bullet with no source
           has nothing to revert to.
        2. **Rewrite fidelity.** A bullet whose :func:`token_overlap` with its source fact is
           below :data:`MIN_REWRITE_OVERLAP` is reverted to the fact's own text and logged as
           ``resume_engine.rewrite_rejected``.
        3. **Metric support.** Every number in a bullet must appear in the source fact's text
           or ``metrics``. Otherwise the bullet reverts, logged as
           ``resume_engine.unsupported_metric``. Checked *after* fidelity so a bullet that
           already reverted is not accused twice.
        4. **Length.** A bullet past :data:`MAX_BULLET_CHARS` reverts; a bullet that is empty
           after trimming reverts.
        5. **Header authority.** Bullets are regrouped by their source facts'
           ``(organisation, role, dates)`` identity, discarding the model's grouping wherever
           it disagreed. Header fields are then copied from the facts — never from the model.

        Args:
            payload: The decoded model reply.
            facts: The prefiltered facts, which define the legal id set.

        Returns:
            The surviving groups, in the order their first bullet was selected. Empty when
            nothing survived, which the caller treats as a model failure and degrades.
        """
        by_id = {str(fact.id): fact for fact in facts}
        seen: set[str] = set()
        groups: dict[tuple[str, str, str, str], _Group] = {}
        hallucinated = 0

        for section in _as_sequence(payload.get("sections")):
            if not isinstance(section, Mapping):
                continue
            model_heading = _one_line(_as_text(section.get("heading")), MAX_HEADING_CHARS)
            for entry in _as_sequence(section.get("entries")):
                if not isinstance(entry, Mapping):
                    continue
                title_hint = _one_line(_as_text(entry.get("title")), MAX_TITLE_CHARS)
                for raw in _as_sequence(entry.get("bullets")):
                    if not isinstance(raw, Mapping):
                        continue
                    fact_id = _as_text(raw.get("fact_id")).strip()
                    fact = by_id.get(fact_id)
                    if fact is None:
                        hallucinated += 1
                        logger.warning(
                            "resume_engine.hallucinated_fact",
                            fact_id=fact_id or "(missing)",
                            heading=model_heading,
                            candidates=len(by_id),
                        )
                        continue
                    if fact_id in seen:
                        # One fact, one bullet. A repeat is the model padding the page.
                        logger.debug("resume_engine.duplicate_fact", fact_id=fact_id)
                        continue
                    seen.add(fact_id)

                    bullet = self._validated_bullet(fact, _as_text(raw.get("text")))
                    heading = _section_for(fact, model_heading)
                    key = _identity(fact)
                    group = groups.get(key)
                    if group is None:
                        group = _Group(key=key, heading=heading, title_hint=title_hint)
                        groups[key] = group
                    group.bullets.append(bullet)

        if hallucinated:
            logger.warning(
                "resume_engine.hallucinations_dropped",
                dropped=hallucinated,
                kept=len(seen),
            )
        return [group for group in groups.values() if group.bullets]

    def _validated_bullet(self, fact: KnowledgeFact, proposed: str) -> _Bullet:
        """Return the bullet to print for *fact*, reverting an unsafe rewrite.

        Args:
            fact: The source fact.
            proposed: The model's rewrite.

        Returns:
            A :class:`_Bullet` carrying either the accepted rewrite or the fact's own text.
        """
        original = _one_line(fact.text, MAX_BULLET_CHARS)
        candidate = _one_line(proposed, MAX_BULLET_CHARS + 1)
        fact_id = str(fact.id)

        if not candidate:
            return _Bullet(fact=fact, text=original, rewritten=False)

        if len(candidate) > MAX_BULLET_CHARS:
            logger.info(
                "resume_engine.rewrite_rejected",
                fact_id=fact_id,
                reason="too_long",
                characters=len(candidate),
            )
            return _Bullet(fact=fact, text=original, rewritten=False)

        overlap = token_overlap(candidate, fact.text or "")
        if overlap < MIN_REWRITE_OVERLAP:
            logger.warning(
                "resume_engine.rewrite_rejected",
                fact_id=fact_id,
                reason="low_overlap",
                overlap=round(overlap, 3),
                threshold=MIN_REWRITE_OVERLAP,
            )
            return _Bullet(fact=fact, text=original, rewritten=False)

        supported = numbers_in(fact.text or "") | numbers_in(" ".join(fact.metrics or ()))
        invented = numbers_in(candidate) - supported
        if invented:
            logger.warning(
                "resume_engine.unsupported_metric",
                fact_id=fact_id,
                numbers=sorted(invented),
                overlap=round(overlap, 3),
            )
            return _Bullet(fact=fact, text=original, rewritten=False)

        return _Bullet(fact=fact, text=candidate, rewritten=True)

    # ----------------------------------------------------------------------------------
    # Assembly
    # ----------------------------------------------------------------------------------

    def _assemble(
        self,
        req: TailorRequest,
        facts: Sequence[KnowledgeFact],
        payload: Mapping[str, Any],
        usage: dict[str, int],
    ) -> TailorResult:
        """Validate a model reply and build the finished document from it.

        Args:
            req: The tailoring request.
            facts: The prefiltered facts.
            payload: The decoded model reply.
            usage: The estimated token usage.

        Returns:
            The result.

        Raises:
            ValueError: When validation left nothing at all, which the caller treats as a
                model failure and answers with :meth:`fallback_tailor`. Raising rather than
                returning an empty document is deliberate: an empty résumé must never reach a
                renderer.
        """
        groups = self.validate(payload, facts)
        if not groups:
            raise ValueError("no bullet in the model reply could be traced to a fact")

        used = [bullet.fact for group in groups for bullet in group.bullets]
        document = self._build_document(
            req,
            groups,
            summary=self._validated_summary(_as_text(payload.get("summary")), used, req),
            skills_line=self._validated_skills(_as_text(payload.get("skills_line")), used),
        )
        self._enforce_budget(document, req)

        return TailorResult(
            document=document,
            selected_fact_ids=document.all_fact_ids(),
            reasoning=_one_line(_as_text(payload.get("reasoning")), MAX_POSTING_CHARS),
            token_usage=usage,
            cached=False,
            degraded=False,
        )

    def _build_document(
        self,
        req: TailorRequest,
        groups: Sequence[_Group],
        *,
        summary: str,
        skills_line: str,
    ) -> ResumeDocument:
        """Assemble validated groups into a :class:`~app.documents.models.ResumeDocument`.

        Args:
            req: The tailoring request, supplying the contact block and metadata.
            groups: The validated groups.
            summary: The validated summary, possibly empty.
            skills_line: The validated skills line, possibly empty.

        Returns:
            The document, with ``fact_ids`` populated parallel to every entry's bullets and
            a bullet-priority map in :attr:`~app.documents.models.ResumeDocument.meta` for the
            renderer's shrink loop.
        """
        sections: dict[str, ResumeSection] = {}
        for group in sorted(groups, key=lambda item: -item.rank):
            entry = self._entry_for(group)
            section = sections.get(group.heading)
            if section is None:
                section = ResumeSection(heading=group.heading, entries=[])
                sections[group.heading] = section
            section.entries.append(entry)

        ordered = sorted(sections.values(), key=lambda item: _heading_rank(item.heading))
        priorities = {
            bullet.fact_id: float(bullet.impact)
            for group in groups
            for bullet in group.bullets
        }

        return ResumeDocument(
            contact=_contact_for(req.user),
            summary=summary,
            sections=ordered,
            skills_line=skills_line,
            meta=self._meta(req, priorities),
        )

    def _entry_for(self, group: _Group) -> ResumeEntry:
        """Build one entry, taking every header field from the source facts.

        The model's ``title`` is used only when the source fact states no role — an
        unaffiliated project, typically — and only when :func:`token_overlap` shows the words
        already occur in the fact. Everything else is copied outright.

        Args:
            group: The validated group.

        Returns:
            The entry, with ``fact_ids[i]`` naming the fact behind ``bullets[i]``.
        """
        lead = group.lead
        title = _one_line(lead.role, MAX_TITLE_CHARS)
        if not title and group.title_hint:
            grounding = " ".join(
                part
                for part in (
                    lead.text or "",
                    lead.organization or "",
                    " ".join(lead.skills or ()),
                    " ".join(lead.technologies or ()),
                )
                if part
            )
            words = group.title_hint.split()
            looks_like_a_name = len(words) >= MIN_TITLE_WORDS or (
                bool(words) and words[0][:1].isupper()
            )
            if (
                looks_like_a_name
                and token_overlap(group.title_hint, grounding) >= MIN_TITLE_GROUNDING
            ):
                title = group.title_hint
            else:
                logger.info(
                    "resume_engine.title_rejected",
                    fact_id=str(lead.id),
                    title=group.title_hint,
                )

        return ResumeEntry(
            title=title,
            organization=_one_line(lead.organization, MAX_TITLE_CHARS),
            location=_one_line(lead.location, MAX_TITLE_CHARS),
            date_range=_format_date_range(lead),
            bullets=[bullet.text for bullet in group.bullets],
            fact_ids=[bullet.fact_id for bullet in group.bullets],
        )

    def _validated_summary(
        self, proposed: str, facts: Sequence[KnowledgeFact], req: TailorRequest
    ) -> str:
        """Return the summary to print, discarding an ungrounded one.

        A summary has no single source fact, so it is measured against everything it is
        allowed to draw on: the selected facts and the posting. It must also invent no
        number, by the same rule bullets obey.

        Args:
            proposed: The model's summary.
            facts: The facts that made it onto the page.
            req: The tailoring request, supplying the posting text.

        Returns:
            The summary, or ``""`` when it failed either check. An absent summary is normal;
            a fabricated one is not.
        """
        candidate = _one_line(proposed, MAX_BULLET_CHARS)
        if not candidate:
            return ""

        corpus = " ".join(
            [req.posting_text()]
            + [
                " ".join(
                    part
                    for part in (
                        fact.text or "",
                        fact.organization or "",
                        fact.role or "",
                        " ".join(fact.skills or ()),
                        " ".join(fact.technologies or ()),
                    )
                    if part
                )
                for fact in facts
            ]
        )
        overlap = token_overlap(candidate, corpus)
        if overlap < MIN_SUMMARY_GROUNDING:
            logger.warning(
                "resume_engine.summary_rejected",
                reason="low_overlap",
                overlap=round(overlap, 3),
                threshold=MIN_SUMMARY_GROUNDING,
            )
            return ""

        supported = numbers_in(corpus)
        invented = numbers_in(candidate) - supported
        if invented:
            logger.warning(
                "resume_engine.summary_rejected",
                reason="unsupported_metric",
                numbers=sorted(invented),
            )
            return ""
        return candidate

    @staticmethod
    def _validated_skills(proposed: str, facts: Sequence[KnowledgeFact]) -> str:
        """Return the skills line, stripped of anything no selected fact evidences.

        Args:
            proposed: The model's comma-separated skills line.
            facts: The facts that made it onto the page.

        Returns:
            The surviving skills, in the model's order, capped at
            :data:`MAX_SKILLS_ON_LINE`. Topped up from the facts' own skills and technologies
            when fewer than :data:`MIN_SKILLS_ON_LINE` survived — including the case where the
            model supplied nothing at all, which is how the fallback path builds this line.
        """
        evidenced: dict[str, str] = {}
        for fact in facts:
            for value in (*(fact.skills or ()), *(fact.technologies or ())):
                name = _one_line(str(value), MAX_TITLE_CHARS)
                if name:
                    evidenced.setdefault(name.casefold(), name)

        kept: list[str] = []
        seen: set[str] = set()
        for raw in _SKILL_SPLIT.split(proposed or ""):
            name = _one_line(raw, MAX_TITLE_CHARS)
            if not name:
                continue
            canonical = evidenced.get(name.casefold())
            if canonical is None:
                logger.info("resume_engine.unsupported_skill", skill=name)
                continue
            if canonical.casefold() in seen:
                continue
            seen.add(canonical.casefold())
            kept.append(canonical)
            if len(kept) >= MAX_SKILLS_ON_LINE:
                break

        for name in evidenced.values():
            if len(kept) >= MIN_SKILLS_ON_LINE:
                break
            if name.casefold() not in seen:
                seen.add(name.casefold())
                kept.append(name)
        return SKILLS_SEPARATOR.join(kept)

    @staticmethod
    def _enforce_budget(document: ResumeDocument, req: TailorRequest) -> int:
        """Shrink *document* to the request's bullet budget, lowest impact first.

        Delegates to :meth:`~app.documents.models.ResumeDocument.drop_lowest_priority_bullets`
        so the per-section floor is honoured — a heading with nothing under it reads as a
        rendering bug — and ranks by the priority map built from each bullet's source
        ``impact_score``.

        Args:
            document: The assembled document, modified in place.
            req: The tailoring request, supplying the budget.

        Returns:
            How many bullets were dropped.
        """
        budget = req.bullet_budget()
        excess = document.total_bullets() - budget
        if excess <= 0:
            return 0
        priorities = document.meta.get(PRIORITIES_META_KEY)
        dropped = document.drop_lowest_priority_bullets(
            excess, priorities if isinstance(priorities, Mapping) else None
        )
        logger.info(
            "resume_engine.budget_enforced",
            budget=budget,
            dropped=dropped,
            remaining=document.total_bullets(),
            estimated_lines=document.estimated_lines(),
        )
        return dropped

    def _meta(self, req: TailorRequest, priorities: Mapping[str, float]) -> dict[str, Any]:
        """Return the document's generation metadata.

        Args:
            req: The tailoring request.
            priorities: Bullet priorities keyed by fact id.

        Returns:
            A JSON-serialisable mapping. ``meta`` is never printed, so it carries the
            provenance a later audit needs plus the priority map
            :func:`app.documents.render_resume` reads when it has to drop bullets.
        """
        return {
            "template": req.template or DEFAULT_TEMPLATE,
            "variant_label": req.variant_label,
            "posting_id": str(req.posting.id) if req.posting.id else None,
            "posting_title": req.posting.title,
            "company": req.posting.company_name,
            "provider": req.posting.provider.value,
            "model": getattr(self.llm, "model", ""),
            PRIORITIES_META_KEY: dict(priorities),
        }

    # ----------------------------------------------------------------------------------
    # Deterministic fallback
    # ----------------------------------------------------------------------------------

    def fallback_tailor(
        self, req: TailorRequest, facts: Sequence[KnowledgeFact]
    ) -> TailorResult:
        """Build a résumé from *facts* with no language model at all.

        Called whenever the LLM path fails, and callable directly by anything that wants a
        document without spending a token. The facts are already ranked by
        :meth:`prefilter`, so this is pure assembly: group by
        ``(organisation, role, dates)``, place each group under the section its facts' kinds
        and affiliation imply, and print **the original fact text, verbatim**. Nothing is
        rewritten, so nothing can be wrong.

        The output is a real résumé, not a placeholder. It is less tailored — bullets read as
        the extractor wrote them rather than as the posting's vocabulary — and it is complete,
        traceable and safe to submit.

        Args:
            req: The tailoring request.
            facts: The prefiltered facts, best first. An empty sequence yields a document
                with a contact block and nothing else, which is the honest rendering of "this
                user has no indexed knowledge yet".

        Returns:
            The result, with ``degraded=True`` and zero token usage.
        """
        groups: dict[tuple[str, str, str, str], _Group] = {}
        for fact in facts:
            key = _identity(fact)
            group = groups.get(key)
            if group is None:
                group = _Group(key=key, heading=_section_for(fact, ""))
                groups[key] = group
            group.bullets.append(
                _Bullet(
                    fact=fact,
                    text=_one_line(fact.text, MAX_BULLET_CHARS),
                    rewritten=False,
                )
            )

        ordered = [group for group in groups.values() if group.bullets]
        document = self._build_document(
            req,
            ordered,
            summary="",
            skills_line=self._validated_skills("", list(facts)),
        )
        self._enforce_budget(document, req)

        reasoning = (
            f"Generated without a language model from {len(facts)} retrieved fact(s), "
            f"ranked by similarity to the posting, keyword overlap and impact score. "
            f"Bullets are the source facts' own wording, unmodified."
        )
        logger.info(
            "resume_engine.fallback",
            posting=str(req.posting.id or ""),
            facts=len(facts),
            bullets=document.total_bullets(),
        )
        return TailorResult(
            document=document,
            selected_fact_ids=document.all_fact_ids(),
            reasoning=reasoning,
            token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            cached=False,
            degraded=True,
        )

    # ----------------------------------------------------------------------------------
    # Caching
    # ----------------------------------------------------------------------------------

    def _cache_key(self, req: TailorRequest, facts: Sequence[KnowledgeFact]) -> str:
        """Return the content-addressed key for one tailoring.

        Four things decide whether a stored result is still correct, and all four are in the
        key: **who** it is for, **what** posting it targets, **which policy** was in force,
        and **which facts** were available. A knowledge re-index that changes the fact set
        therefore invalidates precisely, without a sweeping purge (golden rule #9).

        Args:
            req: The tailoring request.
            facts: The prefiltered facts.

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
                "location": posting.location,
            }
        )
        return make_key(
            NAMESPACES.LLM,
            CACHE_DISCRIMINATOR,
            str(getattr(req.user, "user_id", "") or ""),
            posting_hash,
            hash_payload(req.prefs),
            hash_payload([str(fact.id) for fact in facts]),
            req.template,
            req.variant_label or "",
            req.bullet_budget(),
            getattr(self.llm, "model", ""),
        )

    async def _cache_read(self, key: str) -> TailorResult | None:
        """Read a stored tailoring, tolerating a stale or unreadable entry.

        Args:
            key: The cache key.

        Returns:
            The result with ``cached=True``, or ``None`` on a miss or an entry this build can
            no longer parse — a document shape change must degrade to regeneration, never to
            a crash.
        """
        try:
            payload = await self.cache.get(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a cache outage must not fail a generation
            logger.warning("resume_engine.cache_unavailable", error=str(exc))
            return None
        if not isinstance(payload, Mapping):
            return None
        try:
            document = ResumeDocument.model_validate(payload.get("document"))
        except Exception as exc:  # noqa: BLE001 - stale shape, regenerate instead
            logger.info("resume_engine.cache_stale", error=str(exc))
            return None
        return TailorResult(
            document=document,
            selected_fact_ids=[str(item) for item in _as_sequence(payload.get("selected_fact_ids"))],
            reasoning=_as_text(payload.get("reasoning")),
            token_usage={
                str(name): int(value)
                for name, value in (payload.get("token_usage") or {}).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            cached=True,
            degraded=bool(payload.get("degraded", False)),
        )

    async def _cache_write(self, key: str, result: TailorResult) -> None:
        """Store a tailoring, tolerating an unavailable cache.

        Args:
            key: The cache key.
            result: The result to store.
        """
        payload = {
            "document": result.document.model_dump(mode="json"),
            "selected_fact_ids": list(result.selected_fact_ids),
            "reasoning": result.reasoning,
            "token_usage": dict(result.token_usage),
            "degraded": result.degraded,
        }
        try:
            await self.cache.set(key, payload, ttl=TAILOR_CACHE_TTL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a write failure costs a cache hit, nothing more
            logger.warning("resume_engine.cache_write_failed", error=str(exc))


# ======================================================================================
# Module-private helpers
# ======================================================================================


def _as_sequence(value: Any) -> list[Any]:
    """Return *value* as a list, treating anything unexpected as empty.

    Args:
        value: A field from a decoded model reply.

    Returns:
        The list, or ``[]``. Strings and mappings are *not* iterated: a model that returned
        a string where an array was specified supplied no items, not N single characters.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _as_text(value: Any) -> str:
    """Return *value* as a string, treating anything non-textual as empty.

    Args:
        value: A field from a decoded model reply.

    Returns:
        The string, or ``""``.
    """
    return value if isinstance(value, str) else ""


def _identity(fact: KnowledgeFact) -> tuple[str, str, str, str]:
    """Return the ``(organisation, role, start, end)`` key one résumé entry groups on.

    Case-folded so "Northwind Robotics" and "northwind robotics" are one employer; dates are
    compared as written, because their surface form is meaningful and normalising them would
    merge two genuinely different stints.

    A fact that names **neither** an organisation nor a role is unaffiliated work, and the
    employer key would then be empty for every such fact — which would file two unrelated
    side projects from the same year under one entry. Those fall back to the fact's graph
    entity when the indexer anchored one (so every fact about the same project groups
    together) and to the fact's own id otherwise (so each project gets its own entry).

    Args:
        fact: The fact to key.

    Returns:
        The identity tuple.
    """
    organization = (fact.organization or "").strip().casefold()
    role = (fact.role or "").strip().casefold()
    if not organization and not role:
        anchor = str(fact.entity_id) if fact.entity_id is not None else str(fact.id)
        return ("", "", f"project:{anchor}", (fact.date_end or "").strip())
    return (
        organization,
        role,
        (fact.date_start or "").strip(),
        (fact.date_end or "").strip(),
    )


def _format_date_range(fact: KnowledgeFact) -> str:
    """Return the printed date range for *fact*, using only what the source stated.

    Args:
        fact: The source fact.

    Returns:
        ``"<start> – <end>"``, ``"<start> – Present"`` when the fact states a start and no
        end (which the extractor's contract defines as ongoing), the end alone when only an
        end is known, or ``""``.
    """
    start = (fact.date_start or "").strip()
    end = (fact.date_end or "").strip()
    if start and end:
        return f"{start}{DATE_RANGE_SEPARATOR}{end}"
    if start:
        return f"{start}{DATE_RANGE_SEPARATOR}{ONGOING_DATE_LABEL}"
    return end


def _section_for(fact: KnowledgeFact, model_heading: str) -> str:
    """Return the section heading *fact* belongs under.

    The fact's kind decides first, because education, awards, leadership and publications are
    unambiguous. Work kinds then split on affiliation: a fact naming an organisation is
    employment, a fact naming none is a project. The model's heading is honoured only when it
    is one of the canonical ones and the kind does not already dictate otherwise, which stops
    an invented heading from fragmenting the page.

    Args:
        fact: The fact being placed.
        model_heading: The heading the model proposed, possibly empty.

    Returns:
        One of :data:`SECTION_ORDER`.
    """
    by_kind = SECTION_FOR_KIND.get(FactKind(fact.kind))
    if by_kind is not None:
        return by_kind
    for heading in SECTION_ORDER:
        if model_heading.casefold() == heading.casefold():
            return heading
    return EXPERIENCE_HEADING if (fact.organization or "").strip() else PROJECTS_HEADING


def _heading_rank(heading: str) -> int:
    """Return a section heading's print position.

    Args:
        heading: The heading.

    Returns:
        Its index in :data:`SECTION_ORDER`, or a value past the end for anything unexpected,
        so an unknown heading sorts last rather than displacing Experience.
    """
    folded = heading.casefold()
    for index, known in enumerate(SECTION_ORDER):
        if folded == known.casefold():
            return index
    return len(SECTION_ORDER)


def _contact_for(user: UserProfileDTO) -> Contact:
    """Build the résumé header block from the applicant's profile.

    Args:
        user: The applicant.

    Returns:
        A :class:`~app.documents.models.Contact` carrying only non-empty values. Links are
        emitted in the conventional résumé order and empty ones are omitted, so an absent
        portfolio prints as nothing rather than as a bare label.
    """
    links: dict[str, str] = {}
    for label in ("github", "linkedin", "portfolio", "website"):
        url = user.link(label)
        if url:
            links[label] = url
    return Contact(
        name=user.full_name or "",
        email=user.email or "",
        phone=user.phone or "",
        location=user.location or "",
        links=links,
    )
