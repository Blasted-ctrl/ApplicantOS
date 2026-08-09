"""Golden rule #1 — never apply twice.

Two independent mechanisms, because either one alone eventually fails, and both are asserted
here separately:

1. **The database constraint** — ``UNIQUE(user_id, posting_id)`` on ``applications``. This
   holds even when application code has a bug, so it is tested by trying to violate it
   directly rather than through a service.
2. **The status guard** — ``Pipeline.submit`` refuses a post-submit application *before* the
   provider is called. A guard that runs after the network call is not a guard, so the test
   installs a **spy provider** and asserts ``spy.calls == 0``. Asserting only on the returned
   verdict would pass against a pipeline that submits and then reports "already applied".

``prepare`` is covered too: it is idempotent past ``READY``, which is what lets a scheduler
call it on every pass without generating a second resume for the same application.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.application import Application
from app.models.enums import ApplicationStatus, ReviewReason
from app.services.pipeline import Pipeline


class SpyProvider:
    """An ATS provider that records whether it was ever asked to apply.

    The point of the whole file: if ``apply`` is reached even once for an application that
    has already been submitted, the user has applied twice and no return value can undo it.
    """

    def __init__(self) -> None:
        self.calls: int = 0
        self.contexts: list[object] = []

    async def apply(self, ctx: object):
        """Record the attempt and return a success, so a leak is loud rather than silent."""
        self.calls += 1
        self.contexts.append(ctx)
        from app.jobs.base import ApplyResult

        return ApplyResult(ok=True, status=ApplicationStatus.SUBMITTED)


# ======================================================================================
# 1. The database constraint
# ======================================================================================


async def test_unique_user_posting_constraint_raises(session, user, posting) -> None:
    """A second ``Application`` for the same (user, posting) is rejected by the database."""
    session.add(
        Application(
            user_id=user.id,
            posting_id=posting.id,
            company_id=posting.company_id,
            status=ApplicationStatus.SUBMITTED,
        )
    )
    await session.commit()

    session.add(
        Application(
            user_id=user.id,
            posting_id=posting.id,
            company_id=posting.company_id,
            status=ApplicationStatus.DRAFT,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_constraint_permits_the_same_posting_for_a_different_user(
    session, user, posting
) -> None:
    """The constraint is on the *pair*; two applicants may both apply to one posting."""
    from app.models.user import User

    other = User(email="grace@example.com", full_name="Grace Hopper", preferences={})
    session.add(other)
    await session.commit()

    session.add_all(
        [
            Application(
                user_id=user.id,
                posting_id=posting.id,
                company_id=posting.company_id,
                status=ApplicationStatus.READY,
            ),
            Application(
                user_id=other.id,
                posting_id=posting.id,
                company_id=posting.company_id,
                status=ApplicationStatus.READY,
            ),
        ]
    )
    await session.commit()  # must not raise


# ======================================================================================
# 2. The status guard — with the spy
# ======================================================================================


@pytest.mark.parametrize(
    "status",
    [ApplicationStatus.SUBMITTED, ApplicationStatus.CONFIRMED],
)
async def test_submit_refuses_post_submit_without_reaching_the_provider(
    session, settings, monkeypatch, make_application, posting, status
) -> None:
    """A submitted or confirmed application is refused **before** the provider is touched."""
    application = await make_application(posting, status=status)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    pipeline = Pipeline(session, settings)
    result = await pipeline.submit(application.id)

    # The load-bearing assertion.
    assert spy.calls == 0, "provider.apply() was reached for an already-applied application"
    assert result.submitted is False
    assert result.verdict == "already_applied"


@pytest.mark.parametrize(
    "status",
    [
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.OFFER,
    ],
)
async def test_no_post_submit_state_ever_reaches_the_provider(
    session, settings, monkeypatch, make_application, make_posting, status
) -> None:
    """Every state past submission refuses, including the outcome states.

    ``REJECTED`` matters most here: it is the state a user is most tempted to "retry" from,
    and re-applying after a rejection is exactly the embarrassment golden rule #1 prevents.
    """
    posting = await make_posting()
    application = await make_application(posting, status=status)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    pipeline = Pipeline(session, settings)
    result = await pipeline.submit(application.id)

    assert spy.calls == 0, f"provider.apply() reached from {status.value}"
    assert result.submitted is False


async def test_the_guard_runs_before_the_kill_switch_and_the_score(
    session, submission_allowed, monkeypatch, make_application, posting
) -> None:
    """Ordering: the never-apply-twice check precedes every other guard.

    Both switches are open and the provider is available, so nothing except guard 1 stands
    between this call and a second submission.
    """
    application = await make_application(posting, status=ApplicationStatus.SUBMITTED)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    pipeline = Pipeline(session, submission_allowed)
    result = await pipeline.submit(application.id)

    assert spy.calls == 0
    assert result.verdict == "already_applied"


async def test_submit_refuses_when_not_ready(
    session, submission_allowed, monkeypatch, make_application, posting
) -> None:
    """An application that was never prepared is not submitted on a guess."""
    application = await make_application(posting, status=ApplicationStatus.DRAFT)

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert spy.calls == 0
    assert result.submitted is False


# ======================================================================================
# 3. `prepare` is idempotent
# ======================================================================================


@pytest.mark.parametrize(
    "status",
    [
        ApplicationStatus.READY,
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.ABANDONED,
    ],
)
async def test_prepare_is_a_noop_past_ready(
    session, settings, make_application, make_posting, status
) -> None:
    """A second ``prepare`` generates no documents and creates no ``ResumeVersion``.

    Nothing is stubbed on the generation path: if ``prepare`` did any work here it would try
    to reach the knowledge graph and the renderer, and the assertion on
    ``resume_version_id`` would fail rather than silently pass.
    """
    posting = await make_posting()
    application = await make_application(posting, status=status)
    before = application.updated_at

    returned = await Pipeline(session, settings).prepare(posting.id, application.user_id)

    assert returned.id == application.id
    assert returned.status is status, "prepare changed the status of a settled application"
    assert returned.resume_version_id is None, "prepare generated a second resume"
    assert returned.updated_at == before


async def test_prepare_returns_the_same_row_it_created(
    session, settings, user, posting, monkeypatch
) -> None:
    """Two ``prepare`` calls address one application row, never two.

    Document generation is stubbed to a fixed summary so the test exercises the *identity*
    of the row rather than the resume engine, which has its own file.
    """

    async def _fake_generate(self, application, user_, posting_):
        return {"bullets": 6, "facts": 4, "sections": 2, "cover_letter": False}

    monkeypatch.setattr(Pipeline, "_generate_documents", _fake_generate)

    pipeline = Pipeline(session, settings)
    first = await pipeline.prepare(posting.id, user.id)
    second = await pipeline.prepare(posting.id, user.id)

    assert first.id == second.id
    assert first.status is ApplicationStatus.READY
    assert second.status is ApplicationStatus.READY

    from sqlalchemy import func, select

    total = await session.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.user_id == user.id, Application.posting_id == posting.id)
    )
    assert total == 1


async def test_review_states_are_not_silently_resubmitted(
    session, submission_allowed, monkeypatch, make_application, posting
) -> None:
    """An application parked in review waits for a human, not for the next scheduler pass."""
    application = await make_application(
        posting,
        status=ApplicationStatus.NEEDS_REVIEW,
        review_reason=ReviewReason.CAPTCHA,
    )

    spy = SpyProvider()
    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: spy))

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert spy.calls == 0
    assert result.submitted is False
