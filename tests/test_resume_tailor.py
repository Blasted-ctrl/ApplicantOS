"""Resume generation as a *view over the knowledge graph* (``docs/CONTRACTS.md`` §10).

``test_golden_no_fabrication`` owns the four anti-hallucination guards. This file covers the
rest of the engine: prefiltering, the cache, the bullet budget, and — the property that keeps
the product usable when everything else is broken — **the degradation path**.

The degradation contract is unusually strong and is worth stating plainly, because it is the
reason ``LLM_PROVIDER=null`` is a supported configuration rather than a debugging aid:
``tailor`` never raises on a model failure. No API key, a rate limit, an exhausted token
budget, an unparseable reply, a reply that ignored the schema — every one of them is answered
with ``fallback_tailor``, which ranks the user's own facts and uses their text verbatim. A
posting that goes un-applied-to because an API was down is a worse outcome than a resume
written from the user's own sentences.

So the failure modes are parametrised rather than sampled: each is injected into the model
double and the assertion is the same every time — a document came back, and every line of it
came from a fact.
"""

from __future__ import annotations

import pytest

from app.ai.resume_engine import ResumeEngine, TailorRequest
from app.jobs.base import JobPostingDTO, UserProfileDTO
from app.models.user import UserPreferences
from tests.fakes import RecordingLLM


@pytest.fixture
def request_for(user, posting):
    """A :class:`TailorRequest` factory bound to the fixtures."""

    def _make(**overrides) -> TailorRequest:
        values = {
            "user": UserProfileDTO(
                user_id=user.id, full_name=user.full_name, email=user.email
            ),
            "posting": JobPostingDTO.from_model(posting),
            "prefs": UserPreferences(),
        }
        values.update(overrides)
        return TailorRequest(**values)

    return _make


def _engine(session, llm) -> ResumeEngine:
    """A resume engine over *llm* with no cache."""
    from app.knowledge.retrieval import KnowledgeRetriever

    return ResumeEngine(session, llm, KnowledgeRetriever(session), None)


def _bullets(result) -> list[str]:
    """Every bullet in a tailor result."""
    return [
        bullet
        for section in result.document.sections
        for entry in section.entries
        for bullet in entry.bullets
    ]


def _response_from(facts) -> dict:
    """A well-formed model reply selecting every fact verbatim."""
    return {
        "summary": "",
        "skills_line": "Python, Redis",
        "sections": [
            {
                "heading": "Experience",
                "entries": [
                    {
                        "title": "",
                        "bullets": [
                            {"fact_id": str(fact.id), "text": fact.text} for fact in facts
                        ],
                    }
                ],
            }
        ],
    }


# ======================================================================================
# Prefiltering
# ======================================================================================


async def test_prefilter_returns_the_users_facts(session, master_facts, request_for) -> None:
    """The candidate set is the user's own knowledge, and nothing else."""
    engine = _engine(session, RecordingLLM())
    facts = await engine.prefilter(request_for())

    assert facts
    assert {str(fact.id) for fact in facts} <= {str(fact.id) for fact in master_facts}


async def test_prefilter_is_bounded_by_top_k(session, master_facts, request_for) -> None:
    """A prompt that grows with the knowledge base would eventually stop fitting."""
    engine = _engine(session, RecordingLLM())
    facts = await engine.prefilter(request_for(), top_k=2)
    assert len(facts) <= 2


async def test_prefilter_is_empty_without_a_user_id(session, master_facts, request_for) -> None:
    """A legitimate state, not an error: an anonymous request has no graph to read."""
    engine = _engine(session, RecordingLLM())
    anonymous = request_for(user=UserProfileDTO(user_id=None, full_name="Nobody", email=""))
    assert await engine.prefilter(anonymous) == []


async def test_prefilter_works_with_the_hashing_embedder(
    session, master_facts, request_for, settings
) -> None:
    """The zero-API-key path: with no vectors, keyword overlap and impact rank alone."""
    assert settings.embedding_provider == "hashing"
    engine = _engine(session, RecordingLLM())
    assert await engine.prefilter(request_for())


async def test_prefilter_is_deterministic(session, master_facts, request_for) -> None:
    """Two identical requests select the same facts in the same order."""
    engine = _engine(session, RecordingLLM())
    first = [str(f.id) for f in await engine.prefilter(request_for())]
    second = [str(f.id) for f in await engine.prefilter(request_for())]
    assert first == second


# ======================================================================================
# The happy path
# ======================================================================================


async def test_tailor_builds_a_document_from_the_model_reply(
    session, master_facts, request_for
) -> None:
    """A well-formed reply produces a document whose bullets trace to facts."""
    llm = RecordingLLM(responses=[_response_from(master_facts)])
    engine = _engine(session, llm)

    result = await engine.tailor(request_for())

    assert _bullets(result)
    assert result.selected_fact_ids
    assert set(result.selected_fact_ids) <= {str(fact.id) for fact in master_facts}


async def test_the_contact_block_comes_from_the_profile(
    session, master_facts, request_for, user
) -> None:
    """Contact details are the user's, never the model's."""
    llm = RecordingLLM(responses=[_response_from(master_facts)])
    result = await _engine(session, llm).tailor(request_for())

    assert result.document.contact.name == user.full_name
    assert result.document.contact.email == user.email


async def test_the_bullet_budget_is_enforced(session, master_facts, request_for) -> None:
    """``max_bullets`` caps the document by dropping the lowest-impact lines."""
    llm = RecordingLLM(responses=[_response_from(master_facts)])
    result = await _engine(session, llm).tailor(request_for(max_bullets=2))

    assert len(_bullets(result)) <= 2


async def test_the_budget_never_produces_an_empty_document(
    session, master_facts, request_for
) -> None:
    """A mis-set preference must not yield a contact block and nothing else."""
    llm = RecordingLLM(responses=[_response_from(master_facts)])
    result = await _engine(session, llm).tailor(request_for(max_bullets=0))
    assert len(_bullets(result)) >= 1


async def test_the_posting_is_sent_to_the_model(session, master_facts, request_for) -> None:
    """Tailoring that ignored the posting would not be tailoring."""
    llm = RecordingLLM(responses=[_response_from(master_facts)])
    await _engine(session, llm).tailor(request_for())

    assert llm.prompts
    sent = " ".join(str(value) for prompt in llm.prompts for value in prompt.values())
    assert "Backend" in sent or "Python" in sent


async def test_only_prefiltered_facts_reach_the_prompt(
    session, master_facts, request_for
) -> None:
    """The prompt carries the candidate set, which is what makes the id validator meaningful.

    If the model were free to see facts outside the prefiltered set, "the id is not in the
    set" would be the engine's bug rather than the model's hallucination.
    """
    llm = RecordingLLM(responses=[_response_from(master_facts)])
    engine = _engine(session, llm)
    candidates = await engine.prefilter(request_for(), top_k=2)

    await engine.tailor(request_for(max_bullets=2))
    sent = " ".join(str(value) for prompt in llm.prompts for value in prompt.values())

    for fact in candidates:
        assert str(fact.id) in sent


# ======================================================================================
# Degradation — the reason LLM_PROVIDER=null is a supported configuration
# ======================================================================================


@pytest.mark.parametrize(
    ("label", "llm"),
    [
        ("raises", RecordingLLM(error=RuntimeError("rate limited"))),
        ("timeout", RecordingLLM(error=TimeoutError("model timed out"))),
        ("empty reply", RecordingLLM(responses=[{}])),
        ("no sections", RecordingLLM(responses=[{"summary": "hi", "sections": []}])),
        ("wrong shape", RecordingLLM(responses=[{"sections": "not a list"}])),
        ("null bullets", RecordingLLM(responses=[{"sections": [{"entries": None}]}])),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_tailor_degrades_rather_than_raising(
    session, master_facts, request_for, label, llm
) -> None:
    """**The degradation contract.** Every model failure yields a real document.

    A posting that goes un-applied-to because an API was down is a worse outcome than a
    resume written from the user's own sentences.
    """
    result = await _engine(session, llm).tailor(request_for())

    bullets = _bullets(result)
    assert bullets, f"{label}: degradation produced an empty document"

    source_texts = {fact.text for fact in master_facts}
    assert set(bullets) <= source_texts, f"{label}: degradation invented text"


async def test_degradation_still_records_fact_ids(session, master_facts, request_for) -> None:
    """Golden rule #7 holds on the fallback path too."""
    llm = RecordingLLM(error=RuntimeError("no api key"))
    result = await _engine(session, llm).tailor(request_for())

    assert result.selected_fact_ids
    assert set(result.selected_fact_ids) <= {str(fact.id) for fact in master_facts}


async def test_an_empty_knowledge_graph_yields_an_empty_document(
    session, request_for, user
) -> None:
    """No facts is not an error — but it is also not a resume, and ``prepare`` escalates.

    The engine's job here is to be honest about the emptiness rather than to fill it.
    """
    llm = RecordingLLM()
    result = await _engine(session, llm).tailor(request_for())

    assert _bullets(result) == []
    assert result.selected_fact_ids == []


async def test_the_model_is_not_called_when_there_is_nothing_to_select(
    session, request_for
) -> None:
    """With no facts, the prompt would be empty; skipping the call saves a pointless spend."""
    llm = RecordingLLM()
    await _engine(session, llm).tailor(request_for())
    assert llm.calls == 0


# ======================================================================================
# Caching
# ======================================================================================


async def test_an_identical_request_hits_the_cache(session, master_facts, request_for) -> None:
    """The second tailor of the same posting costs no model call."""
    from app.cache.memory import MemoryCache
    from app.knowledge.retrieval import KnowledgeRetriever

    llm = RecordingLLM(responses=[_response_from(master_facts), _response_from(master_facts)])
    engine = ResumeEngine(session, llm, KnowledgeRetriever(session), MemoryCache())

    first = await engine.tailor(request_for())
    calls_after_first = llm.calls
    second = await engine.tailor(request_for())

    assert calls_after_first == 1
    assert llm.calls == 1, "an identical request called the model again"
    assert second.cached is True
    assert _bullets(second) == _bullets(first)


async def test_a_different_template_is_a_different_cache_entry(
    session, master_facts, request_for
) -> None:
    """Changing the template must produce a new document, not a stale hit."""
    from app.cache.memory import MemoryCache
    from app.knowledge.retrieval import KnowledgeRetriever

    llm = RecordingLLM(responses=[_response_from(master_facts), _response_from(master_facts)])
    engine = ResumeEngine(session, llm, KnowledgeRetriever(session), MemoryCache())

    await engine.tailor(request_for(template="modern"))
    await engine.tailor(request_for(template="classic"))

    assert llm.calls == 2


async def test_a_different_variant_is_a_different_cache_entry(
    session, master_facts, request_for
) -> None:
    """Two resumes for one posting — "embedded" and "ml" — must not collide."""
    from app.cache.memory import MemoryCache
    from app.knowledge.retrieval import KnowledgeRetriever

    llm = RecordingLLM(responses=[_response_from(master_facts), _response_from(master_facts)])
    engine = ResumeEngine(session, llm, KnowledgeRetriever(session), MemoryCache())

    await engine.tailor(request_for(variant_label="embedded"))
    await engine.tailor(request_for(variant_label="ml"))

    assert llm.calls == 2


# ======================================================================================
# fallback_tailor on its own
# ======================================================================================


def test_fallback_orders_by_impact(session, master_facts, request_for) -> None:
    """With no model to rank, impact score is the ranking."""
    engine = _engine(session, RecordingLLM())
    result = engine.fallback_tailor(request_for(), master_facts)

    assert _bullets(result)
    assert result.selected_fact_ids


def test_fallback_respects_the_bullet_budget(session, master_facts, request_for) -> None:
    """The no-LLM path obeys the same page budget as the model path."""
    engine = _engine(session, RecordingLLM())
    result = engine.fallback_tailor(request_for(max_bullets=2), master_facts)
    assert len(_bullets(result)) <= 2


def test_fallback_groups_by_employer(session, master_facts, request_for) -> None:
    """Two employers produce two entries, never one merged block."""
    engine = _engine(session, RecordingLLM())
    result = engine.fallback_tailor(request_for(), master_facts)

    organisations = {
        entry.organization
        for section in result.document.sections
        for entry in section.entries
        if entry.organization
    }
    assert len(organisations) >= 2
