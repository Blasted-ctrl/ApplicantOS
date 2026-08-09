"""Memory in the prompt — the loop that was recorded, embedded, ranked, and then dropped.

``MemoryStore.as_prompt_context`` and ``MemoryStore.reinforce`` were fully implemented and had
zero callers, so everything the system learned from the user went into the database and stopped
there. This file is the proof that it no longer does, and it asserts the three properties that
make the wiring safe rather than merely present:

**It reaches the model, and it changes the answer.** The model double here is not a canned
response — it reads its own system prompt and answers only from what the memory block tells it.
A test that asserted "the block is in the prompt" would pass against a prompt the model was
never given; this one fails unless the correction genuinely arrives.

**Personal data does not reach the model.** A reviewer who types a Social Security number into
an unknown field creates a memory whose body *is* that number. The screen excludes the entry
whole — never redacted, because a mangled lesson teaches nothing — and the assertions check the
prompts *and the log records*, because a value scrubbed from one and printed in the other has
not been protected.

**The reward follows the outcome.** A memory injected into an answer that was submitted gains
weight; one injected into an answer that went to a human does not. That asymmetry is the entire
supervised signal, and it is cheap only because nothing extra has to be measured to collect it.

On the résumé path the additional property is structural: memories go into the **system**
prompt and never into ``$facts``, so golden rule #7 and the four validators stay authoritative.
The test for that reads the two prompts the engine actually sent.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import structlog.testing

from app.ai.memory_prompt import (
    EMPTY_MEMORY_BLOCK,
    REINFORCE_CLEAN_DELTA,
    MemoryBlock,
    build_memory_block,
)
from app.jobs.base import FormField, UserProfileDTO
from app.knowledge.memory import KIND_WEIGHTS, MemoryStore
from app.knowledge.retrieval import KnowledgeRetriever
from app.models.enums import FieldKind, MemoryKind

#: A Social Security number, in the shape a reviewer would type it into a form.
SSN = "123-45-6789"

#: A free-text question that matches no ``KNOWN_FIELDS`` alias and no EEO alias, so it reaches
#: the model path. "notice period" is an alias; "notice must you give" is not.
NOTICE_QUESTION = "How much notice must you give your current employer before starting?"


class MemoryReadingLLM:
    """A model double that answers only from what its **system** prompt tells it.

    The point of this double is that it cannot be satisfied by a memory block that was built
    and then dropped. It answers if and only if *marker* appears in the system prompt, which is
    where :class:`~app.ai.field_answer.FieldAnswerer` injects memories, and declines otherwise —
    exactly as the real prompt instructs a model to do when nothing supports an answer.

    Attributes:
        prompts: Every call's keyword arguments, so a test can inspect what was sent.
    """

    def __init__(self, marker: str, answer: str, *, confidence: float = 0.9) -> None:
        """Bind the double to the marker it looks for and the answer it gives.

        Args:
            marker: Text that must appear in the system prompt for the double to answer.
            answer: What to answer when it does.
            confidence: The confidence to report.
        """
        self.marker = marker
        self.answer = answer
        self.confidence = confidence
        self.prompts: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        """Model identifier, for token accounting."""
        return "memory-reading-test-model"

    def count_tokens(self, text: str) -> int:
        """A stable, dependency-free token estimate."""
        return max(1, len(text) // 4)

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        """Answer from the system prompt, or decline."""
        self.prompts.append(dict(kwargs))
        if self.marker in str(kwargs.get("system", "")):
            return {
                "answer": self.answer,
                "confidence": self.confidence,
                "reasoning": "The applicant told me this on a previous application.",
            }
        return {
            "answer": "",
            "confidence": 0.0,
            "reasoning": "Nothing in the supplied material answers this.",
        }

    def every_prompt(self) -> str:
        """Return every system and user message this double has been sent, concatenated."""
        return "\n".join(
            f"{call.get('system', '')}\n{call.get('prompt', '')}" for call in self.prompts
        )


def _profile(user, **overrides: Any) -> UserProfileDTO:
    """Build the applicant DTO the answerer and the résumé engine take.

    Args:
        user: The persisted :class:`~app.models.user.User`.
        **overrides: Extra DTO fields.

    Returns:
        The DTO.
    """
    values: dict[str, Any] = {
        "user_id": user.id,
        "full_name": user.full_name,
        "email": user.email,
    }
    values.update(overrides)
    return UserProfileDTO(**values)


def _answerer(user, session, llm) -> Any:
    """Build a :class:`~app.ai.field_answer.FieldAnswerer` wired to the real memory store.

    Args:
        user: The applicant.
        session: The test's session.
        llm: The model double.

    Returns:
        The answerer.
    """
    from app.ai.field_answer import FieldAnswerer

    return FieldAnswerer(_profile(user), {}, llm=llm, knowledge=KnowledgeRetriever(session))


def _question(label: str = NOTICE_QUESTION) -> FormField:
    """Return a free-text field carrying *label*."""
    return FormField(selector="#q1", label=label, kind=FieldKind.TEXTAREA, required=True)


async def _give_profile(session, user) -> None:
    """Persist a bare :class:`~app.models.profile.UserProfile` for *user*.

    ``UserProfileDTO.from_model`` reads ``user_id`` off the profile, and the résumé engine
    returns an empty fact list without one. A pipeline test that skipped this would escalate
    for the wrong reason and would keep passing if the memory wiring were deleted.

    Args:
        session: The test's session.
        user: The applicant.
    """
    from app.models.profile import UserProfile

    session.add(UserProfile(user_id=user.id))
    await session.commit()
    await session.refresh(user)


# ======================================================================================
# A correction changes a later answer
# ======================================================================================


async def test_a_recorded_correction_changes_a_later_answer(session, settings, user) -> None:
    """The whole point of the feature, asserted as a before/after on the same question.

    The same field is answered twice by the same model double. The only difference between the
    two runs is that a human corrected the system in between. Before, the model has nothing and
    declines — which routes the field to a human. After, the correction is in its system prompt
    and it answers, which is the interruption the user does not get.
    """
    llm = MemoryReadingLLM(marker="4 weeks", answer="4 weeks")
    field = _question()

    before = await _answerer(user, session, llm).answer(field)
    assert before.value == "", "with no memory the model has nothing to answer from"
    assert before.confidence == pytest.approx(0.0)

    await MemoryStore(session).record_correction(
        user.id,
        before="2 weeks",
        after="4 weeks",
        context={"field": "#notice", "source": "review_resolve"},
    )

    after = await _answerer(user, session, llm).answer(field)
    assert after.value == "4 weeks"
    assert after.confidence == pytest.approx(0.9)
    assert after.source == "llm"

    system = str(llm.prompts[-1]["system"])
    assert "4 weeks" in system, "the memory must arrive in the system prompt"
    assert "4 weeks" not in str(llm.prompts[-1]["prompt"]), (
        "memories belong in the instructions, not in the material the answer is drawn from"
    )


async def test_the_memory_block_is_a_stated_absence_when_there_is_nothing(
    session, settings, user
) -> None:
    """An unfilled ``$memories`` placeholder would read to a model as a truncated instruction."""
    llm = MemoryReadingLLM(marker="never appears", answer="unused")
    await _answerer(user, session, llm).answer(_question())

    system = str(llm.prompts[-1]["system"])
    assert EMPTY_MEMORY_BLOCK in system
    assert "$memories" not in system


async def test_a_correction_supports_the_number_it_carries(session, settings, user) -> None:
    """The unsupported-metric guard must not reject the user's own recorded answer.

    ``field_answer`` rejects any number the supporting material does not contain, and without
    the memory block in that material a correction reading "four weeks" would be answered
    "4 weeks" and then thrown away as invented — so the system would re-ask, forever, the one
    question the user has already answered.
    """
    await MemoryStore(session).record_correction(
        user.id, before="2 weeks", after="4 weeks", context={"field": "#notice"}
    )
    llm = MemoryReadingLLM(marker="4 weeks", answer="4 weeks")

    plan = await _answerer(user, session, llm).answer(_question())

    assert plan.value == "4 weeks", "a number quoted from the user's own correction is not invented"


# ======================================================================================
# The PII screen
# ======================================================================================


async def test_a_memory_carrying_an_ssn_never_reaches_the_model(session, settings, user) -> None:
    """The prerequisite the research doc makes non-negotiable, asserted on prompts *and* logs.

    ``ReviewService._remember`` stores the human's literal answer to a form field, so a reviewer
    who typed a Social Security number into an unknown field created a memory whose body *is*
    that number. It is excluded whole — a redacted lesson ("Preferred wording: ***") teaches
    nothing — and the exclusion names the category, never the value.
    """
    entry = await MemoryStore(session).record_correction(
        user.id,
        before="",
        after=SSN,
        context={"field": "#national_id", "source": "review_resolve"},
    )
    assert SSN in entry.text, "the stored memory is never mangled; only the reader excludes it"

    llm = MemoryReadingLLM(marker=SSN, answer=SSN)
    with structlog.testing.capture_logs() as records:
        plan = await _answerer(user, session, llm).answer(_question("What is your national id?"))

    assert SSN not in llm.every_prompt(), "an excluded memory must not reach any prompt"
    assert plan.value == "", "with the memory excluded the model has nothing and declines"

    excluded = [record for record in records if record.get("event") == "memory.excluded_pii"]
    assert excluded, "the exclusion has to be observable, or nobody can tell it happened"
    assert "ssn" in excluded[0]["categories"]
    assert excluded[0]["memory_id"] == str(entry.id)
    assert SSN not in str(records), "the log names the category and never the value"


async def test_the_users_own_email_is_not_treated_as_a_leak(session, settings, user) -> None:
    """Allow-listing the profile's own contact fields is what keeps the screen usable.

    Without it, "my email is ada@example.com" reads as a leak and nearly every memory worth
    injecting is excluded — which would make the screen a switch that turns the feature off.
    """
    await MemoryStore(session).record_correction(
        user.id,
        before="Contact: (see resume)",
        after=f"Reach me at {user.email}, not the address on the old resume.",
    )
    llm = MemoryReadingLLM(marker=user.email, answer="fine")

    plan = await _answerer(user, session, llm).answer(_question("How should we contact you?"))

    assert user.email in str(llm.prompts[-1]["system"])
    assert plan.value == "fine"


async def test_the_recorded_pii_stamp_alone_excludes_a_memory(session, settings, user) -> None:
    """The stamp exists so a reader can skip an entry without re-running the screen.

    A memory whose body no longer looks like a leak but which was stamped at record time is
    still excluded. That is the cautious direction on purpose: one memory the model does not
    see, versus a value that should never have been in a prompt.
    """
    store = MemoryStore(session)
    entry = await store.record_feedback(user.id, "A perfectly ordinary preference.")
    entry.context = {"pii": {"categories": ["ssn"], "hits": 1, "allowed": 0}}
    await session.flush()

    block = await build_memory_block(store, user.id, "ordinary preference", purpose="test", k=8)

    assert block.empty
    assert block.excluded == 1
    assert block.memory_ids == ()


# ======================================================================================
# The reinforcement loop
# ======================================================================================


async def test_a_clean_answer_reinforces_the_memory_behind_it(session, settings, user) -> None:
    """The supervised half: weight follows an outcome that did not need a human."""
    entry = await MemoryStore(session).record_correction(
        user.id, before="2 weeks", after="4 weeks", context={"field": "#notice"}
    )
    assert entry.weight == pytest.approx(KIND_WEIGHTS[MemoryKind.CORRECTION])

    answerer = _answerer(user, session, MemoryReadingLLM(marker="4 weeks", answer="4 weeks"))
    plan = await answerer.answer(_question())

    assert plan.is_confident(settings.min_answer_confidence)
    assert answerer.injected_memory_ids == [entry.id]
    await session.refresh(entry)
    assert entry.weight == pytest.approx(
        KIND_WEIGHTS[MemoryKind.CORRECTION] + REINFORCE_CLEAN_DELTA
    )


async def test_an_escalated_answer_leaves_the_weight_alone(session, settings, user) -> None:
    """No penalty and no reward.

    A field escalates for a hundred reasons — a model outage, an unanswerable question, an
    option that matched nothing — and docking a memory for all of them would eventually silence
    the corrections this product runs on. It simply earns nothing.
    """
    entry = await MemoryStore(session).record_correction(
        user.id, before="2 weeks", after="4 weeks", context={"field": "#notice"}
    )
    original = float(entry.weight)

    # The double answers only when its marker is in the system prompt; this one never matches,
    # so the model declines and the field goes to a human.
    answerer = _answerer(user, session, MemoryReadingLLM(marker="nothing matches", answer="x"))
    plan = await answerer.answer(_question())

    assert plan.value == ""
    assert answerer.injected_memory_ids == [entry.id], "it was injected, it just did not pay off"
    await session.refresh(entry)
    assert entry.weight == pytest.approx(original)


async def test_a_low_confidence_answer_is_not_a_clean_outcome(session, settings, user) -> None:
    """Below ``min_answer_confidence`` the field goes to a human, so nothing is credited."""
    entry = await MemoryStore(session).record_correction(
        user.id, before="2 weeks", after="4 weeks", context={"field": "#notice"}
    )
    original = float(entry.weight)

    answerer = _answerer(
        user, session, MemoryReadingLLM(marker="4 weeks", answer="4 weeks", confidence=0.3)
    )
    plan = await answerer.answer(_question())

    assert plan.confidence == pytest.approx(0.3)
    assert not plan.is_confident(settings.min_answer_confidence)
    await session.refresh(entry)
    assert entry.weight == pytest.approx(original)


async def test_a_memory_dropped_by_the_token_budget_is_never_credited(
    session, settings, user
) -> None:
    """Only what is *in* the block counts.

    ``as_prompt_context`` stops at the first entry that would breach its 600-token budget.
    Crediting an entry the model never saw would poison the one signal this loop collects.
    """
    store = MemoryStore(session)
    for index in range(8):
        await store.record_feedback(user.id, f"Lesson {index}: " + ("padding words " * 60))

    block = await build_memory_block(store, user.id, "lesson padding", purpose="test", k=8)

    assert block.considered == 8
    assert 0 < len(block.memory_ids) < 8, "the budget must actually bite for this to prove much"
    assert block.text.count("\n") == len(block.memory_ids), "one header line, then one per memory"


# ======================================================================================
# The résumé path — style only
# ======================================================================================


async def test_memories_reach_the_resume_system_prompt_and_never_the_facts(
    session, settings, user, posting, master_facts
) -> None:
    """Golden rule #7 stays authoritative by construction, not by instruction.

    The memory block is in the instructions. ``$facts`` — the only place a ``fact_id`` can come
    from, and the only material :meth:`ResumeEngine.validate` reads — never sees it. There is
    therefore no path by which "mention my Kubernetes work" puts Kubernetes on a résumé that has
    no Kubernetes fact behind it.
    """
    from app.ai.resume_engine import ResumeEngine, TailorRequest
    from app.jobs.base import JobPostingDTO
    from app.models.user import UserPreferences
    from tests.fakes import RecordingLLM

    marker = "Never call me a full-stack developer"
    await MemoryStore(session).record_preference(user.id, marker)

    llm = RecordingLLM(
        default_json={
            "summary": "",
            "skills_line": "Python",
            "reasoning": "",
            "sections": [
                {
                    "heading": "Experience",
                    "entries": [
                        {
                            "title": "",
                            "bullets": [
                                {"fact_id": str(master_facts[0].id), "text": master_facts[0].text}
                            ],
                        }
                    ],
                }
            ],
        }
    )
    engine = ResumeEngine(session, llm, KnowledgeRetriever(session), None)
    result = await engine.tailor(
        TailorRequest(
            user=_profile(user),
            posting=JobPostingDTO.from_model(posting),
            prefs=UserPreferences(),
        )
    )

    call = llm.prompts[-1]
    assert marker in str(call["system"]), "a memory is a style constraint, so it goes in the rules"
    assert marker not in str(call["prompt"]), "a memory is never fact material"
    assert result.memory_ids, "the ids travel out so the pipeline can reward them later"
    assert result.selected_fact_ids == [str(master_facts[0].id)]


async def test_a_memory_cannot_put_content_on_a_resume(
    session, settings, user, posting, master_facts
) -> None:
    """The adversarial case for rule #7: the memory asks, the model complies, the validator wins.

    A preference the user themselves recorded is still not a source of content. The bullet the
    model returns cites a ``fact_id`` that was never in the list, so it is dropped exactly as a
    hallucination is — the memory bought it nothing.
    """
    from app.ai.resume_engine import ResumeEngine, TailorRequest
    from app.jobs.base import JobPostingDTO
    from app.models.user import UserPreferences
    from tests.fakes import RecordingLLM

    await MemoryStore(session).record_preference(
        user.id, "Always mention my Kubernetes work at Globex."
    )

    llm = RecordingLLM(
        default_json={
            "summary": "",
            "skills_line": "Kubernetes",
            "reasoning": "",
            "sections": [
                {
                    "heading": "Experience",
                    "entries": [
                        {
                            "title": "Platform Engineer",
                            "organization": "Globex",
                            "bullets": [
                                {
                                    "fact_id": str(uuid.uuid4()),
                                    "text": "Ran Kubernetes clusters at Globex.",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    engine = ResumeEngine(session, llm, KnowledgeRetriever(session), None)
    result = await engine.tailor(
        TailorRequest(
            user=_profile(user),
            posting=JobPostingDTO.from_model(posting),
            prefs=UserPreferences(),
        )
    )

    rendered = " ".join(
        bullet
        for section in result.document.sections
        for entry in section.entries
        for bullet in entry.bullets
    )
    assert "Kubernetes" not in rendered
    assert "Globex" not in rendered
    assert result.degraded, "nothing survived validation, so the deterministic fallback ran"


async def test_prepare_reinforces_only_when_it_reaches_ready(
    session, settings, user, posting, master_facts
) -> None:
    """The pipeline is where a résumé's outcome is finally known.

    ``prepare`` returns early on every escalation and every failure; only the ``ready`` branch
    credits the memories that shaped the document. This asserts both halves against the same
    memory, using the empty-résumé escalation as the not-clean case.
    """
    from app.models.enums import ApplicationStatus
    from app.services.pipeline import MEMORY_IDS_SUMMARY_KEY, Pipeline

    await _give_profile(session, user)
    entry = await MemoryStore(session).record_preference(
        user.id, "Lead with the backend performance work, not the tooling."
    )
    await session.commit()
    original = float(entry.weight)

    application = await Pipeline(session, settings).prepare(posting.id, user.id)
    assert application.status is ApplicationStatus.READY

    await session.refresh(entry)
    assert entry.weight == pytest.approx(original + REINFORCE_CLEAN_DELTA)

    events = [event for event in application.events if event.payload]
    assert any(MEMORY_IDS_SUMMARY_KEY in (event.payload or {}) for event in events), (
        "the ready event records which lessons shaped the document"
    )


async def test_prepare_does_not_reinforce_an_escalation(
    session, settings, user, make_posting
) -> None:
    """No facts means an empty résumé, which escalates — and credits nothing.

    The complement to :func:`test_an_escalated_answer_leaves_the_weight_alone`, which covers the
    case where a memory *was* injected and the work still went to a human. This one covers the
    early returns: every path out of ``prepare`` other than ``ready`` skips the credit, and a
    reinforcement placed at injection time rather than here would fire on all of them.
    """
    from app.models.enums import ApplicationStatus
    from app.services.pipeline import Pipeline

    await _give_profile(session, user)
    entry = await MemoryStore(session).record_preference(user.id, "Keep it to one page.")
    await session.commit()
    original = float(entry.weight)

    # No `master_facts` fixture, so the knowledge graph is empty and the résumé would be blank.
    target = await make_posting()
    application = await Pipeline(session, settings).prepare(target.id, user.id)

    assert application.status is ApplicationStatus.NEEDS_REVIEW
    await session.refresh(entry)
    assert entry.weight == pytest.approx(original)


# ======================================================================================
# The review queue's negative signal
# ======================================================================================


async def test_dismiss_records_the_negative_signal(
    session, settings, user, posting, make_application
) -> None:
    """ "I don't want jobs like this" is a judgement, and it used to be thrown away.

    ``resolve`` fed ``record_correction`` and ``dismiss`` wrote a note to ``Application.notes``
    and nothing else. The company and the title go into the body rather than staying behind a
    foreign key, because a memory reading "application 8f3a… was dismissed" teaches nothing when
    it is retrieved six months after the posting is deleted.
    """
    from app.models.enums import ApplicationStatus
    from app.services.review_service import MEMORY_SOURCE_DISMISS, ReviewService

    application = await make_application(posting, status=ApplicationStatus.NEEDS_REVIEW)
    dismissed = await ReviewService(session).dismiss(application.id, "Too much on-call.")

    assert dismissed.status is ApplicationStatus.ABANDONED

    memories = await MemoryStore(session).relevant(user.id, "dismissed backend engineer", k=8)
    feedback = [item for item in memories if item.kind is MemoryKind.FEEDBACK]
    assert feedback, "the dismissal has to be recorded, or the signal is still discarded"

    recorded = feedback[0]
    assert "Acme Robotics, Inc." in recorded.text
    assert "Senior Backend Engineer" in recorded.text
    assert "Too much on-call." in recorded.text
    assert recorded.context["source"] == MEMORY_SOURCE_DISMISS
    assert recorded.context["application_id"] == str(application.id)


async def test_dismiss_without_a_note_still_records_the_choice(
    session, settings, user, posting, make_application
) -> None:
    """The decision itself is the signal; the note is colour."""
    from app.models.enums import ApplicationStatus
    from app.services.review_service import ReviewService

    application = await make_application(posting, status=ApplicationStatus.NEEDS_REVIEW)
    await ReviewService(session).dismiss(application.id)

    stats = await MemoryStore(session).stats(user.id)
    assert stats.get("memory_feedback", 0) == 1


async def test_a_dismissal_memory_reaches_a_later_prompt(
    session, settings, user, posting, make_application
) -> None:
    """The loop closes: recorded on dismissal, retrieved and injected on the next question."""
    from app.models.enums import ApplicationStatus
    from app.services.review_service import ReviewService

    application = await make_application(posting, status=ApplicationStatus.NEEDS_REVIEW)
    await ReviewService(session).dismiss(application.id, "No on-call rotations, ever.")

    llm = MemoryReadingLLM(marker="No on-call rotations", answer="I am not open to on-call.")
    plan = await _answerer(user, session, llm).answer(
        _question("Are you comfortable joining an on-call rotation, and why?")
    )

    assert "No on-call rotations" in str(llm.prompts[-1]["system"])
    assert plan.value == "I am not open to on-call."


# ======================================================================================
# Degradation
# ======================================================================================


async def test_a_broken_memory_store_never_fails_an_answer(session, settings, user) -> None:
    """Memory is an improvement, not a dependency. A store that raises costs a block, not a run."""

    class _BrokenStore:
        """A store whose every read raises."""

        async def relevant(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            """Fail the way an unreachable vector backend would."""
            raise RuntimeError("vector backend unreachable")

        @staticmethod
        def as_prompt_context(_memories: Any) -> str:  # pragma: no cover - never reached
            """Never called: retrieval failed first."""
            return ""

    block = await build_memory_block(_BrokenStore(), user.id, "anything", purpose="test")
    assert block == MemoryBlock()

    llm = MemoryReadingLLM(marker="unreachable", answer="unused")
    plan = await _answerer(user, session, llm).answer(_question())
    assert plan.value == "", "the model still ran; it simply had no memory to work from"


async def test_an_answerer_without_a_retriever_still_answers(session, settings, user) -> None:
    """``knowledge=None`` is a supported configuration and must not reach the memory path."""
    from app.ai.field_answer import FieldAnswerer

    llm = MemoryReadingLLM(marker=EMPTY_MEMORY_BLOCK, answer="answered anyway")
    answerer = FieldAnswerer(_profile(user), {}, llm=llm)

    plan = await answerer.answer(_question())

    assert plan.value == "answered anyway"
    assert answerer.injected_memory_ids == []


# =======================================================================================
# The live screen — the branch that catches memories written before the stamp existed
# =======================================================================================


async def test_live_screen_excludes_an_unstamped_memory(session: Any, user: Any) -> None:
    """A memory carrying PII but **no stamp** is still excluded.

    ``_screen`` has two independent tests, and every other test in this file records its
    memory through ``MemoryStore._record``, which stamps ``context["pii"]``. That means only
    the stamp branch was ever exercised — mutation-proven: replacing the live screen's
    ``if verdict.found:`` with ``if False:`` left this whole file green.

    The live screen exists for exactly one situation the stamp cannot cover: a user upgrades
    from a build that predates stamping, so their existing ``memory_entries`` rows carry no
    stamp at all. One of them is the Social Security number they typed into an unknown field.
    Without this branch it goes straight into the next prompt.

    So this constructs the row the way the database would hand it back — text with PII,
    context without a stamp — and asserts the reader still refuses it.
    """
    from app.ai.memory_prompt import _screen
    from app.knowledge.memory import CONTEXT_KEY_PII
    from app.models.knowledge import MemoryEntry

    unstamped = MemoryEntry(
        user_id=user.id,
        kind=MemoryKind.CORRECTION,
        text=f"Preferred answer: {SSN}",
        context={"field": "#national_id"},  # note: no CONTEXT_KEY_PII
        weight=1.0,
    )
    assert CONTEXT_KEY_PII not in unstamped.context, "fixture must be genuinely unstamped"

    with structlog.testing.capture_logs() as records:
        screened = _screen([unstamped], allow=(), purpose="field_answer", user_id=user.id)

    assert screened.excluded == 1
    assert screened.kept == [], "an unstamped PII memory must not reach the prompt"

    blob = repr(records)
    assert SSN not in blob, "the excluded value must never appear in a log line"
    assert any("pii" in str(record.get("event", "")).lower() for record in records)


async def test_live_screen_keeps_an_unstamped_clean_memory(session: Any, user: Any) -> None:
    """The live screen must not over-fire: an unstamped, PII-free memory still gets through.

    The failure direction matters. Excluding everything unstamped would be "safe" and would
    also silently delete the entire feature for anyone upgrading.
    """
    from app.ai.memory_prompt import _screen
    from app.models.knowledge import MemoryEntry

    clean = MemoryEntry(
        user_id=user.id,
        kind=MemoryKind.PREFERENCE,
        text="Prefers a four week notice period.",
        context={"field": "#notice"},
        weight=1.0,
    )

    screened = _screen([clean], allow=(), purpose="field_answer", user_id=user.id)

    assert screened.excluded == 0
    assert len(screened.kept) == 1
