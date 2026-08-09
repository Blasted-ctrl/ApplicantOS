"""Golden rule #2 — never guess; escalate instead.

    Low confidence, essay overflow, captcha, MFA, or an unknown required field ⇒
    ``NEEDS_REVIEW``.

Every case here asserts two things, and the second matters more than the first:

1. the right :class:`~app.models.enums.ReviewReason` comes back, and
2. **nothing was typed** — ``fake_page.fills == []`` and ``fake_page.writes == []``.

A system that fills the form and *then* flags it for review has already put a guessed answer
on the page; a later failure to submit is not a rescue, because the review queue then shows a
human a form pre-populated with answers nobody vouched for. So the recorded page actions are
the real assertion, exactly as in the kill-switch file.

Ordering is asserted too. ``AutoFiller.fill`` checks essay overflow *before* the first field
is resolved, so a form that is going to a human anyway costs one comparison rather than five
model calls — and, more importantly, never touches the page on the way there.
"""

from __future__ import annotations

import pytest

from app.ai.field_answer import NO_ANSWER, AnswerPlan
from app.browser.autofill import ESSAY_MIN_MAX_LENGTH, AutoFiller
from app.browser.selectors import GREENHOUSE
from app.jobs.base import FormField
from app.models.enums import FieldKind, ReviewReason
from tests.fakes import FakePage, FakeSession


class StubResolver:
    """A resolver returning a fixed plan per field label, recording what it was asked."""

    def __init__(self, plans: dict[str, AnswerPlan] | None = None, default=None) -> None:
        self.plans = plans or {}
        self.default = default
        self.asked: list[str] = []

    async def resolve(self, field: FormField) -> AnswerPlan:
        self.asked.append(field.label)
        if field.label in self.plans:
            return self.plans[field.label]
        if self.default is not None:
            return self.default(field)
        return AnswerPlan(field=field, value="", confidence=NO_ANSWER, source="unanswered")


def _field(
    label: str,
    *,
    kind: FieldKind = FieldKind.TEXT,
    required: bool = True,
    selector: str | None = None,
    options: list[str] | None = None,
    max_length: int | None = None,
) -> FormField:
    """One discovered control."""
    return FormField(
        selector=selector or f"#{label.lower().replace(' ', '_')}",
        label=label,
        kind=kind,
        required=required,
        options=options or [],
        max_length=max_length,
        hint=None,
    )


def _filler(resolver, session=None, **kwargs) -> AutoFiller:
    return AutoFiller(
        session if session is not None else FakeSession(),
        resolver,
        pack=GREENHOUSE,
        **kwargs,
    )


# ======================================================================================
# Low confidence
# ======================================================================================


async def test_low_confidence_is_not_typed(settings, monkeypatch) -> None:
    """An answer below ``min_answer_confidence`` is refused, not written."""
    monkeypatch.setattr(settings, "min_answer_confidence", 0.75)

    field = _field("Why do you want this role?", kind=FieldKind.TEXT)
    resolver = StubResolver(
        {field.label: AnswerPlan(field=field, value="Because I do", confidence=0.4, source="llm")}
    )
    page = FakePage(present={field.selector})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill([field])

    assert filled == []
    assert [f.label for f in review] == [field.label]
    assert page.fills == [], "a low-confidence answer was typed into the form"
    assert page.writes == []
    assert filler.review_reason_for(review) is ReviewReason.UNKNOWN_FIELD


async def test_confidence_exactly_at_the_threshold_is_accepted(settings, monkeypatch) -> None:
    """The comparison is ``>=``; the boundary is not itself a refusal.

    Without this the "never guess" tests would pass against a filler that refuses everything.
    """
    monkeypatch.setattr(settings, "min_answer_confidence", 0.75)

    field = _field("First Name")
    resolver = StubResolver(
        {field.label: AnswerPlan(field=field, value="Ada", confidence=0.75, source="profile")}
    )
    page = FakePage(present={field.selector})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill([field])

    assert len(filled) == 1
    assert review == []
    assert page.fills == [(field.selector, "Ada")]


async def test_an_optional_field_at_low_confidence_is_still_refused(settings, monkeypatch) -> None:
    """ "Optional" does not license a guess — the answer still goes out under the user's name."""
    monkeypatch.setattr(settings, "min_answer_confidence", 0.75)

    field = _field("How did you hear about us?", required=False)
    resolver = StubResolver(
        {field.label: AnswerPlan(field=field, value="A friend", confidence=0.5, source="llm")}
    )
    page = FakePage(present={field.selector})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill([field])

    assert filled == []
    assert page.fills == []
    # Only optional fields are unanswered, so the reason is LOW_CONFIDENCE, not UNKNOWN_FIELD.
    assert filler.review_reason_for(review) is ReviewReason.LOW_CONFIDENCE


# ======================================================================================
# An empty value at confidence 1.0
# ======================================================================================


async def test_empty_value_at_full_confidence_is_never_written(settings, monkeypatch) -> None:
    """Confidence ``1.0`` on an empty string must not clear the threshold.

    This is the sharp edge of the rule: ``is_confident`` requires a *value*, so a resolver bug
    that returns "certainly nothing" cannot blank out a required field on the form.
    """
    monkeypatch.setattr(settings, "min_answer_confidence", 0.75)

    field = _field("Work Authorization", kind=FieldKind.TEXT)
    plan = AnswerPlan(field=field, value="", confidence=1.0, source="profile")
    assert plan.is_confident(0.75) is False

    page = FakePage(present={field.selector})
    filler = _filler(StubResolver({field.label: plan}), FakeSession(page=page))

    filled, review = await filler.fill([field])

    assert filled == []
    assert [f.label for f in review] == [field.label]
    assert page.fills == [], "an empty value at confidence 1.0 was written to the form"


async def test_whitespace_only_value_is_treated_as_no_answer() -> None:
    """ "   " is not an answer either."""
    field = _field("Preferred Name")
    plan = AnswerPlan(field=field, value="   ", confidence=1.0, source="profile")
    assert plan.answered is False
    assert plan.is_confident(0.0) is False


# ======================================================================================
# An unanswerable required field
# ======================================================================================


async def test_unanswerable_required_field_reaches_unknown_field(settings, monkeypatch) -> None:
    """A required question the system cannot answer honestly routes to ``UNKNOWN_FIELD``."""
    monkeypatch.setattr(settings, "min_answer_confidence", 0.75)

    known = _field("Email")
    unknown = _field("What is your Erdős number?", required=True)
    resolver = StubResolver(
        {known.label: AnswerPlan(field=known, value="ada@example.com", confidence=0.95)}
    )
    page = FakePage(present={known.selector, unknown.selector})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill([known, unknown])

    assert [p.field.label for p in filled] == [known.label]
    assert [f.label for f in review] == [unknown.label]
    assert page.fills == [(known.selector, "ada@example.com")]
    assert filler.review_reason_for(review) is ReviewReason.UNKNOWN_FIELD


async def test_a_value_not_among_the_options_is_refused(settings, monkeypatch) -> None:
    """A choice control only accepts one of its own options, byte for byte.

    A browser silently ignores an option that does not exist, so writing one produces a form
    that fails validation in a way that looks like a selector bug rather than a wrong answer.
    """
    monkeypatch.setattr(settings, "min_answer_confidence", 0.75)

    field = _field(
        "Are you legally authorized to work in the US?",
        kind=FieldKind.SELECT,
        options=["Yes", "No"],
    )
    resolver = StubResolver(
        {field.label: AnswerPlan(field=field, value="Probably", confidence=1.0, source="llm")}
    )
    page = FakePage(present={field.selector})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill([field])

    assert filled == []
    assert [f.label for f in review] == [field.label]
    assert page.selections == [], "an option the control does not offer was selected"
    assert page.writes == []


# ======================================================================================
# Essay overflow — checked before anything is filled
# ======================================================================================


async def test_essay_overflow_reaches_too_many_essays(settings, monkeypatch) -> None:
    """More essays than the user tolerates sends the whole form to a human."""
    monkeypatch.setattr(settings, "max_essay_questions_before_review", 3)

    essays = [
        _field(f"Essay {index}", kind=FieldKind.TEXTAREA, max_length=2000) for index in range(4)
    ]
    resolver = StubResolver()
    page = FakePage(present={f.selector for f in essays})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill(essays)

    assert filled == []
    assert len(review) == len(essays)
    assert filler.review_reason_for(review, essays=len(essays)) is ReviewReason.TOO_MANY_ESSAYS


async def test_essay_overflow_resolves_no_field_and_writes_nothing(settings, monkeypatch) -> None:
    """The check runs *before* the first field is resolved.

    ``resolver.asked == []`` is the assertion that proves the ordering: a form destined for a
    human costs zero model calls, and — the safety half — zero keystrokes.
    """
    monkeypatch.setattr(settings, "max_essay_questions_before_review", 1)

    fields = [
        _field("Name"),
        _field("Essay A", kind=FieldKind.TEXTAREA, max_length=5000),
        _field("Essay B", kind=FieldKind.TEXTAREA, max_length=5000),
    ]
    resolver = StubResolver(default=lambda f: AnswerPlan(field=f, value="x", confidence=1.0))
    page = FakePage(present={f.selector for f in fields})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill(fields)

    assert filled == []
    assert resolver.asked == [], "fields were resolved despite essay overflow"
    assert page.writes == [], "the form was filled despite essay overflow"
    assert len(review) == 3


def test_a_textarea_with_no_limit_counts_as_an_essay() -> None:
    """An unbounded textarea is a writing assignment until proven otherwise."""
    unbounded = _field("Tell us about yourself", kind=FieldKind.TEXTAREA, max_length=None)
    assert AutoFiller.count_essays([unbounded]) == 1


def test_a_short_textarea_is_not_an_essay() -> None:
    """A two-sentence box is a short answer, not a composition."""
    short = _field("Preferred pronouns", kind=FieldKind.TEXTAREA, max_length=40)
    assert AutoFiller.count_essays([short]) == 0
    at_boundary = _field("Summary", kind=FieldKind.TEXTAREA, max_length=ESSAY_MIN_MAX_LENGTH)
    assert AutoFiller.count_essays([at_boundary]) == 1


# ======================================================================================
# Blockers: captcha, MFA, login wall
# ======================================================================================


@pytest.mark.parametrize(
    ("blocker", "expected"),
    [
        ("captcha", ReviewReason.CAPTCHA),
        ("cloudflare", ReviewReason.CAPTCHA),
        ("mfa", ReviewReason.MFA),
        ("login_wall", ReviewReason.LOGIN_REQUIRED),
    ],
)
async def test_blockers_reach_the_right_reason_with_nothing_typed(blocker, expected) -> None:
    """A captcha, an MFA prompt or a login wall stops the fill dead."""
    fields = [_field("Name"), _field("Email")]
    resolver = StubResolver(default=lambda f: AnswerPlan(field=f, value="x", confidence=1.0))
    page = FakePage(present={f.selector for f in fields})
    session = FakeSession(page=page, blockers={blocker})
    filler = _filler(resolver, session)

    filled, review = await filler.fill(fields)

    assert filled == []
    assert len(review) == len(fields)
    assert page.writes == [], f"the form was filled behind a {blocker}"
    assert resolver.asked == [], f"fields were resolved behind a {blocker}"
    assert filler.review_reason_for(review) is expected


async def test_a_failed_blocker_probe_is_treated_as_a_blocker() -> None:
    """ "We could not look" is not "all clear"."""
    fields = [_field("Name")]
    resolver = StubResolver(default=lambda f: AnswerPlan(field=f, value="Ada", confidence=1.0))
    page = FakePage(present={fields[0].selector})
    session = FakeSession(page=page, blocker_error=RuntimeError("page closed"))
    filler = _filler(resolver, session)

    filled, review = await filler.fill(fields)

    assert filled == []
    assert page.writes == []
    assert filler.review_reason_for(review) is ReviewReason.AMBIGUOUS_ANSWER


async def test_a_clean_page_with_confident_answers_does_fill() -> None:
    """The control: without this every assertion above is satisfiable by refusing everything."""
    fields = [_field("Name"), _field("Email")]
    resolver = StubResolver(
        default=lambda f: AnswerPlan(field=f, value=f"value-{f.label}", confidence=1.0)
    )
    page = FakePage(present={f.selector for f in fields})
    filler = _filler(resolver, FakeSession(page=page))

    filled, review = await filler.fill(fields)

    assert len(filled) == 2
    assert review == []
    assert len(page.fills) == 2


# ======================================================================================
# A resolver that raises is an answer of "ask a human"
# ======================================================================================


async def test_a_raising_answerer_becomes_a_refusal_not_a_crash(user) -> None:
    """A model outage sends applications to review rather than aborting the run."""
    from app.browser.autofill import FieldResolver

    class Exploding:
        async def answer(self, field):
            raise RuntimeError("model unavailable")

    plan = await FieldResolver(Exploding()).resolve(_field("Why us?"))

    assert plan.value == ""
    assert plan.confidence == NO_ANSWER
    assert plan.is_confident(0.01) is False


# ======================================================================================
# Demographic questions are never inferred
# ======================================================================================


async def test_eeo_fields_never_reach_the_model(user) -> None:
    """Gender, race, disability and veteran status short-circuit before the LLM.

    The model double raises if it is ever called, so reaching it is a hard failure rather than
    a soft assertion about the value returned.
    """
    from app.ai.field_answer import FieldAnswerer
    from app.jobs.base import UserProfileDTO

    class Forbidden:
        async def complete_json(self, **_kwargs):
            raise AssertionError("an EEO question was sent to the language model")

        async def complete(self, **_kwargs):
            raise AssertionError("an EEO question was sent to the language model")

        def count_tokens(self, text: str) -> int:
            return 1

    profile = UserProfileDTO(user_id=user.id, full_name=user.full_name, email=user.email)
    answerer = FieldAnswerer(profile, {}, llm=Forbidden())

    for label in (
        "Gender",
        "Race / Ethnicity",
        "Veteran Status",
        "Disability Status",
    ):
        field = _field(
            label,
            kind=FieldKind.SELECT,
            options=["Male", "Female", "I decline to self-identify"],
        )
        plan = await answerer.answer(field)
        # Either a stated answer from the profile or the decline option — never an inference.
        assert plan.value in ("", "I decline to self-identify"), (
            f"{label} was answered with {plan.value!r}, which was not stated by the user"
        )
