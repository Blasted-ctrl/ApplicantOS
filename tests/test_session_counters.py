"""Run-session counters — what a finished run says it did.

The dashboard's subtitle is a sentence built entirely from these six integers, so a counter
that under-reports is not a lost statistic: it is the product telling the user something
false about the night. That is the same defect class as
``fix(dashboard): a cancelled or failed run is not a finished run``, one layer down — a
vocabulary with more members than its consumer handles.

:func:`app.workers.apply_jobs._session_deltas` is the whole mapping from a pipeline verdict
to those integers, and it is the only place it happens, so it is tested directly and
exhaustively: every member of the verdict vocabulary appears below, including the ones that
must map to nothing.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import ApplicationStatus, ReviewReason
from app.services.pipeline import (
    VERDICT_ALREADY_APPLIED,
    VERDICT_BLOCKED,
    VERDICT_FAILED,
    VERDICT_NEEDS_REVIEW,
    VERDICT_PREPARED,
    VERDICT_SKIPPED,
    VERDICT_SUBMITTED,
    PipelineResult,
)
from app.workers.apply_jobs import _session_deltas


def _result(
    verdict: str,
    status: ApplicationStatus | None,
    *,
    review_reason: ReviewReason | None = None,
) -> dict[str, object]:
    """Build the serialised pipeline result the worker actually receives.

    A real :class:`~app.services.pipeline.PipelineResult` is constructed and serialised
    rather than a hand-written dict, so a change to the payload's shape reaches this file
    instead of passing through it.

    Args:
        verdict: The pipeline verdict.
        status: The application's status when the pipeline returned, or ``None``.
        review_reason: Why it stopped, when it stopped for a human.

    Returns:
        The result as :meth:`PipelineResult.as_dict` produces it.
    """
    return PipelineResult(
        verdict=verdict,
        stage="guard",
        posting_id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        status=status,
        review_reason=review_reason,
    ).as_dict()


def test_submitted_counts_one_application() -> None:
    """A real submission is the only thing that increments ``applications_completed``."""
    deltas = _session_deltas(_result(VERDICT_SUBMITTED, ApplicationStatus.SUBMITTED))
    assert deltas == {"applications_completed": 1}


def test_needs_review_counts_one_review() -> None:
    """An escalation from the guard ladder is a review."""
    deltas = _session_deltas(
        _result(
            VERDICT_NEEDS_REVIEW,
            ApplicationStatus.NEEDS_REVIEW,
            review_reason=ReviewReason.CAPTCHA,
        )
    )
    assert deltas == {"manual_review": 1}


def test_failure_counts_one_failure() -> None:
    """A failed application increments only ``failures``."""
    deltas = _session_deltas(_result(VERDICT_FAILED, ApplicationStatus.FAILED))
    assert deltas == {"failures": 1}


def test_kill_switch_block_counts_as_a_review() -> None:
    """The shipped default — both switches closed — puts the application in front of a human.

    ``Pipeline.submit`` calls ``mark_needs_review(POLICY_BLOCK)`` and *then* returns
    ``blocked``. Reading the verdict alone lost every one of those, so a run that stopped
    each of its applications at the kill switch reported ``0 need review`` on the dashboard
    beside the queue it had just filled.
    """
    deltas = _session_deltas(
        _result(
            VERDICT_BLOCKED,
            ApplicationStatus.NEEDS_REVIEW,
            review_reason=ReviewReason.POLICY_BLOCK,
        )
    )
    assert deltas == {"manual_review": 1}


def test_daily_cap_block_counts_nothing() -> None:
    """The other ``blocked``: the cap leaves the application ``ready`` for tomorrow.

    No human is involved and nothing is waiting, so counting it would inflate the review
    figure by one per capped application every night.
    """
    deltas = _session_deltas(
        _result(
            VERDICT_BLOCKED,
            ApplicationStatus.READY,
            review_reason=ReviewReason.RATE_LIMITED,
        )
    )
    assert deltas == {}


def test_skip_over_an_existing_review_counts_nothing() -> None:
    """A run that merely passed over a waiting application must not re-count it.

    ``skipped`` carries whatever status the application already had, which is why the status
    is consulted for ``blocked`` and for nothing else. Without that restriction every run
    would re-count every application already sitting in the queue.
    """
    deltas = _session_deltas(
        _result(
            VERDICT_SKIPPED,
            ApplicationStatus.NEEDS_REVIEW,
            review_reason=ReviewReason.POLICY_BLOCK,
        )
    )
    assert deltas == {}


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        (VERDICT_SKIPPED, None),
        (VERDICT_PREPARED, ApplicationStatus.READY),
        (VERDICT_ALREADY_APPLIED, ApplicationStatus.SUBMITTED),
    ],
)
def test_verdicts_that_change_nothing(verdict: str, status: ApplicationStatus | None) -> None:
    """A skip, a prepare and the never-apply-twice refusal all cost the run nothing.

    Args:
        verdict: The verdict under test.
        status: The application status that accompanies it.
    """
    assert _session_deltas(_result(verdict, status)) == {}


def test_every_counter_named_here_is_a_real_column() -> None:
    """A typo'd counter name would be refused at runtime by ``SessionService.record``.

    The mapping writes counter names as strings, so this asserts the four the worker can
    emit are all members of the model's allow-list rather than waiting for a warning in a
    log nobody reads.
    """
    from app.models.session import SESSION_COUNTER_FIELDS
    from app.workers.apply_jobs import (
        _COMPLETED_COUNTER,
        _FAILURE_COUNTER,
        _RESUMES_COUNTER,
        _REVIEW_COUNTER,
    )

    emitted = {_COMPLETED_COUNTER, _FAILURE_COUNTER, _RESUMES_COUNTER, _REVIEW_COUNTER}
    assert emitted <= SESSION_COUNTER_FIELDS
