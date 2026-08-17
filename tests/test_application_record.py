"""What an application row must remember about itself (directive §11).

For every submitted application the product promises a record a person can open months later
and understand: the company, the role, the links, the score, the documents, the timestamp,
the confirmation, the screenshot — and **the answer set**, meaning the questions the form
asked and what was submitted under the user's name.

The answer set was the one field nothing wrote on the successful path, so the applications
whose answers were recoverable were exactly the ones that had gone wrong: an application that
sailed through left no trace of what it had said.

It lives in ``submitted_answers`` and **not** in ``answers``, and the distinction is the whole
safety story. ``answers`` is an *input* — the next attempt resolves from it through
``FieldAnswerer._explicit``, which returns any match at confidence 1.0 ahead of the profile,
ahead of the EEO branch that honours "decline to self-identify", and ahead of the model. That
precedence is right only because a human typed those values. Recording the browser's own
output there would freeze machine-resolved values as if a person had chosen them.
"""

from __future__ import annotations

from app.jobs.base import ApplyResult
from app.models.enums import ApplicationStatus
from app.services.pipeline import Pipeline


class _AnsweringProvider:
    """A provider that reports the answers it wrote, as a real one now does."""

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers

    async def apply(self, ctx: object) -> ApplyResult:
        """Report a successful submission carrying its answer set."""
        return ApplyResult(
            ok=True,
            status=ApplicationStatus.SUBMITTED,
            answers=dict(self._answers),
        )


async def test_a_submitted_application_records_its_answers(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """The gap: an application that never needed a human left no record of what it said."""
    await make_score(posting, normalized=95)
    application = await make_application(posting)
    monkeypatch.setattr(
        Pipeline,
        "_provider",
        staticmethod(
            lambda _name: _AnsweringProvider(
                {"Are you authorized to work in the United States?": "Yes"}
            )
        ),
    )

    await Pipeline(session, submission_allowed).submit(application.id)

    assert application.submitted_answers == {
        "Are you authorized to work in the United States?": "Yes"
    }


async def test_a_later_attempt_does_not_erase_what_a_human_settled(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """``answers`` also holds review answers, so the browser's record merges, never replaces.

    Replacing would silently drop the salary expectation a person typed into the review queue
    the moment an automated attempt re-filled the rest of the form.
    """
    await make_score(posting, normalized=95)
    application = await make_application(
        posting, submitted_answers={"Salary expectation": "80000"}
    )
    monkeypatch.setattr(
        Pipeline,
        "_provider",
        staticmethod(lambda _name: _AnsweringProvider({"Full name": "Ada Lovelace"})),
    )

    await Pipeline(session, submission_allowed).submit(application.id)

    assert application.submitted_answers == {
        "Salary expectation": "80000",
        "Full name": "Ada Lovelace",
    }


async def test_the_browsers_record_wins_a_conflict(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """What the page received is the truth about what was submitted."""
    await make_score(posting, normalized=95)
    application = await make_application(
        posting, submitted_answers={"Full name": "A. Lovelace"}
    )
    monkeypatch.setattr(
        Pipeline,
        "_provider",
        staticmethod(lambda _name: _AnsweringProvider({"Full name": "Ada Lovelace"})),
    )

    await Pipeline(session, submission_allowed).submit(application.id)

    assert application.submitted_answers["Full name"] == "Ada Lovelace"


async def test_a_provider_reporting_no_answers_leaves_the_row_alone(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """An empty answer set is "nothing to add", never "erase what is there".

    Providers that submit through an API rather than a form legitimately report none.
    """
    await make_score(posting, normalized=95)
    application = await make_application(
        posting, submitted_answers={"Salary expectation": "80000"}
    )
    monkeypatch.setattr(
        Pipeline, "_provider", staticmethod(lambda _name: _AnsweringProvider({}))
    )

    await Pipeline(session, submission_allowed).submit(application.id)

    assert application.submitted_answers == {"Salary expectation": "80000"}


async def test_the_record_never_becomes_an_input_to_the_next_attempt(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """The defect an adversarial review found, and the reason for two columns.

    ``answers`` is what ``Pipeline.submit`` hands the field answerer, where
    ``FieldAnswerer._explicit`` returns any match at confidence 1.0 — ahead of the live
    profile, ahead of the EEO branch, ahead of the model. Writing the browser's own output
    there froze machine-resolved values as though a human had chosen them: a demographic
    disclosure the user later retracted would still be submitted on a retry, and a corrected
    phone number would be ignored.

    So the record must never appear in the resolver's input.
    """
    await make_score(posting, normalized=95)
    application = await make_application(posting)
    seen: list[dict[str, object]] = []

    class Recording:
        async def apply(self, ctx: object) -> ApplyResult:
            seen.append(dict(getattr(ctx, "answers", {})))
            return ApplyResult(
                ok=True,
                status=ApplicationStatus.SUBMITTED,
                answers={"Disability Status": "Yes, I have a disability"},
            )

    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: Recording()))
    await Pipeline(session, submission_allowed).submit(application.id)

    assert application.submitted_answers == {"Disability Status": "Yes, I have a disability"}
    # The column the resolver reads is untouched.
    assert application.answers == {}
    assert seen == [{}]


async def test_a_human_settled_answer_is_still_the_resolvers_input(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """The other half: what a person typed must still reach the next attempt."""
    await make_score(posting, normalized=95)
    application = await make_application(posting, answers={"Salary expectation": "80000"})
    seen: list[dict[str, object]] = []

    class Recording:
        async def apply(self, ctx: object) -> ApplyResult:
            seen.append(dict(getattr(ctx, "answers", {})))
            return ApplyResult(ok=True, status=ApplicationStatus.SUBMITTED)

    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: Recording()))
    await Pipeline(session, submission_allowed).submit(application.id)

    assert seen == [{"Salary expectation": "80000"}]
