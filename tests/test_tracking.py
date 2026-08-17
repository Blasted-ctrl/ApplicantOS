"""Application status sync (``docs/CONTRACTS.md`` §17).

This subsystem reads the user's mailbox, which makes it the most privacy-sensitive code in
the product and the one with the most explicit invariants. Three groups of tests:

**The classifier corpus.** ``classify_rules`` is pure, synchronous and deterministic, and the
LLM is consulted only when the rules return ``unknown`` or low confidence. Its precision is
what stops a "thanks for applying" auto-reply from being recorded as an interview invitation.
The corpus below is written as realistic subject/body pairs rather than as the phrase list
itself, so a rule that only matches its own documentation string fails here.

**Relay-domain matching.** ``no-reply@greenhouse.io`` identifies the *ATS*, not the employer,
so the company has to be recovered from the display name, the reply-to or the body. Matching
on the sender domain alone would attribute every Greenhouse rejection to a company called
"Greenhouse".

**Idempotent re-sync.** ``UNIQUE(user_id, source, external_ref)`` means re-reading the same
message is a no-op. Without that, a mailbox re-scan after a cursor reset would re-apply every
status transition the user has ever received.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.enums import (
    APPLICATION_POST_SUBMIT_STATES,
    ApplicationStatus,
    SignalKind,
    SignalSource,
)
from app.tracking.base import ATS_RELAY_DOMAINS, StatusSignal, is_relay_domain
from app.tracking.classifier import StatusClassifier
from app.tracking.matcher import SignalMatcher

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _signal(
    *,
    subject: str = "",
    body: str = "",
    sender: str = "no-reply@acme-robotics.com",
    sender_domain: str | None = None,
    external_ref: str = "msg-1",
    received_at: datetime | None = None,
    **extra,
) -> StatusSignal:
    """One inbound message, in the transport shape the classifier reads."""
    return StatusSignal(
        source=SignalSource.EMAIL_IMAP,
        external_ref=external_ref,
        received_at=received_at or NOW,
        sender=sender,
        sender_domain=sender_domain if sender_domain is not None else sender.split("@")[-1],
        subject=subject,
        body=body,
        **extra,
    )


@pytest.fixture
def classifier() -> StatusClassifier:
    """The deterministic classifier."""
    return StatusClassifier()


# ======================================================================================
# The classifier corpus
# ======================================================================================

#: Realistic messages, one per documented ``SignalKind``. Written as prose rather than as the
#: phrase table so a rule that only matches its own example fails.
CORPUS: list[tuple[str, str, SignalKind, ApplicationStatus | None]] = [
    (
        "Thank you for applying to Acme Robotics",
        "We have received your application for Senior Backend Engineer and will be in touch.",
        SignalKind.APPLICATION_RECEIVED,
        ApplicationStatus.SUBMITTED,
    ),
    (
        "Your application was viewed",
        "Good news — your resume was viewed by the hiring team at Acme Robotics.",
        SignalKind.VIEWED,
        None,
    ),
    (
        "Next step: online assessment",
        "Please complete the following coding challenge on HackerRank within five days.",
        SignalKind.ASSESSMENT_REQUESTED,
        ApplicationStatus.NEEDS_REVIEW,
    ),
    (
        "Interview invitation — Acme Robotics",
        "We would like to schedule an interview. Please share your availability for a call.",
        SignalKind.INTERVIEW_INVITE,
        ApplicationStatus.INTERVIEW,
    ),
    (
        "Your offer from Acme Robotics",
        "We are pleased to offer you the position of Senior Backend Engineer.",
        SignalKind.OFFER,
        ApplicationStatus.OFFER,
    ),
    (
        "Update on your application",
        "After careful consideration we have decided to move forward with other candidates.",
        SignalKind.REJECTION,
        ApplicationStatus.REJECTED,
    ),
    (
        "Welcome to Acme Robotics!",
        "We have received your acceptance and your signed offer letter. Welcome to the team — "
        "your first day is Monday the 3rd.",
        SignalKind.OFFER_ACCEPTED,
        ApplicationStatus.ACCEPTED,
    ),
]


@pytest.mark.parametrize(
    ("subject", "body", "kind", "status"),
    CORPUS,
    ids=[entry[2].value for entry in CORPUS],
)
def test_the_classifier_corpus(classifier, subject, body, kind, status) -> None:
    """Every documented ``SignalKind`` is recognised from a realistic message."""
    result = classifier.classify_rules(_signal(subject=subject, body=body))

    assert result.kind is kind
    assert result.status is status


def test_classification_is_deterministic(classifier) -> None:
    """``classify_rules`` is pure and synchronous; the same message always classifies alike."""
    signal = _signal(subject=CORPUS[5][0], body=CORPUS[5][1])
    results = {classifier.classify_rules(signal).kind for _ in range(50)}
    assert results == {SignalKind.REJECTION}


def test_an_unrecognised_message_is_unknown_not_a_guess(classifier) -> None:
    """Golden rule #2 applies here too: silence beats a fabricated status change."""
    result = classifier.classify_rules(
        _signal(subject="Lunch tomorrow?", body="Are you free at 1pm?")
    )
    assert result.kind is SignalKind.UNKNOWN
    assert result.status is None


def test_a_rejection_is_not_read_as_an_interview(classifier) -> None:
    """The expensive confusion, asserted directly.

    "We will not be moving forward to the interview stage" contains the word *interview*, and
    a substring matcher would call it an invitation and tell the user they got one.
    """
    result = classifier.classify_rules(
        _signal(
            subject="Your application",
            body="We will not be moving forward with your application at this time.",
        )
    )
    assert result.kind is not SignalKind.INTERVIEW_INVITE
    assert result.status is not ApplicationStatus.INTERVIEW


def test_an_acknowledgement_is_not_read_as_an_offer(classifier) -> None:
    """ "We are pleased to have received your application" is not an offer."""
    result = classifier.classify_rules(
        _signal(
            subject="Application received",
            body="We are pleased to confirm we have received your application.",
        )
    )
    assert result.status is not ApplicationStatus.OFFER


def test_confidence_is_always_in_range(classifier) -> None:
    """The ``status_sync_min_confidence`` comparison is only meaningful if this holds."""
    for subject, body, _kind, _status in CORPUS:
        result = classifier.classify_rules(_signal(subject=subject, body=body))
        assert 0.0 <= result.confidence <= 1.0


async def test_the_llm_is_not_consulted_for_a_confident_rule_match(classifier) -> None:
    """§17.4: the model may never override a high-confidence rule match.

    The model double raises, so consulting it is a hard failure rather than a soft assertion.
    """

    class Forbidden:
        async def complete_json(self, **_kwargs):
            raise AssertionError("the LLM was consulted for a confident rule match")

        def count_tokens(self, text: str) -> int:
            return 1

    hostile = StatusClassifier()
    hostile._llm = Forbidden()

    result = await hostile.classify(_signal(subject=CORPUS[4][0], body=CORPUS[4][1]), use_llm=True)
    assert result.status is ApplicationStatus.OFFER


# ======================================================================================
# Relay domains
# ======================================================================================


@pytest.mark.parametrize(
    "domain",
    ["greenhouse.io", "lever.co", "ashbyhq.com", "myworkday.com", "icims.com"],
)
def test_known_relays_are_recognised(domain: str) -> None:
    """The relay list is what tells the matcher "this domain is not the employer"."""
    assert is_relay_domain(domain) is True


@pytest.mark.parametrize(
    "domain",
    ["mail.greenhouse.io", "us-east.lever.co", "@greenhouse.io", "GREENHOUSE.IO", "greenhouse.io."],
)
def test_relay_matching_handles_subdomains_and_formatting(domain: str) -> None:
    """Real senders are subdomained, cased inconsistently, and sometimes trailing-dotted."""
    assert is_relay_domain(domain) is True


@pytest.mark.parametrize(
    "domain", ["acme-robotics.com", "", "notgreenhouse.io", "greenhouse.io.evil.com"]
)
def test_a_non_relay_is_not_matched(domain: str) -> None:
    """An employer's own domain — and a lookalike — must not be treated as a relay."""
    assert is_relay_domain(domain) is False


def test_the_relay_list_covers_the_contract() -> None:
    """§17.5 names these explicitly."""
    for required in (
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "myworkday.com",
        "icims.com",
        "smartrecruiters.com",
        "workable.com",
        "jobvite.com",
        "taleo.net",
    ):
        assert required in ATS_RELAY_DOMAINS


# ======================================================================================
# Matching
# ======================================================================================


@pytest.fixture
async def submitted(session, make_posting, make_application, company):
    """An application submitted three days ago at a company with a known domain."""
    company.domain = "acme-robotics.com"
    posting = await make_posting(title="Senior Backend Engineer", external_id="match-1")
    application = await make_application(
        posting,
        status=ApplicationStatus.SUBMITTED,
        submitted_at=NOW - timedelta(days=3),
    )
    await session.commit()
    return application


@pytest.fixture
def matcher(session) -> SignalMatcher:
    """The signal matcher over the test session."""
    return SignalMatcher(session)


async def test_a_sender_domain_matching_the_company_wins(
    matcher, classifier, submitted, user
) -> None:
    """Rule 1 of §17.5, the strongest signal."""
    signal = _signal(
        sender="careers@acme-robotics.com",
        subject="Update on your application",
        body="We have decided to move forward with other candidates.",
    )
    classification = classifier.classify_rules(signal)

    match = await matcher.match(user.id, signal, classification)

    assert match.application_id == submitted.id
    assert match.confidence > 0.0


async def test_a_relay_sender_matches_on_the_company_in_the_body(
    matcher, classifier, submitted, user
) -> None:
    """**The relay case.** The sender is Greenhouse; the employer is in the message.

    Matching on the sender domain alone would attribute this to a company called
    "Greenhouse" and never reach the real application.
    """
    signal = _signal(
        sender="no-reply@greenhouse.io",
        subject="Your application to Acme Robotics",
        body=(
            "Thank you for applying to Acme Robotics for the Senior Backend Engineer role. "
            "We have decided to move forward with other candidates."
        ),
        company_hint="Acme Robotics",
    )
    classification = classifier.classify_rules(signal)

    match = await matcher.match(user.id, signal, classification)

    assert match.application_id == submitted.id


async def test_an_unrelated_company_does_not_match(matcher, classifier, submitted, user) -> None:
    """A rejection from a company the user never applied to must not bind to anything."""
    signal = _signal(
        sender="careers@totally-different.com",
        subject="Update on your application",
        body="We have decided to move forward with other candidates at Totally Different Inc.",
        company_hint="Totally Different",
    )
    classification = classifier.classify_rules(signal)

    match = await matcher.match(user.id, signal, classification)

    assert match.application_id is None or match.confidence < 0.8


async def test_a_message_predating_the_application_does_not_match(
    matcher, classifier, submitted, user
) -> None:
    """Rule 5: the application must have been submitted *before* the message arrived."""
    signal = _signal(
        sender="careers@acme-robotics.com",
        subject="Update on your application",
        body="We have decided to move forward with other candidates.",
        received_at=NOW - timedelta(days=30),
    )
    classification = classifier.classify_rules(signal)

    match = await matcher.match(user.id, signal, classification)

    assert match.application_id is None or match.confidence < 0.8


# ======================================================================================
# Idempotent re-sync
# ======================================================================================


async def test_the_same_message_cannot_be_stored_twice(session, user) -> None:
    """``UNIQUE(user_id, source, external_ref)`` — a cursor reset must not re-apply history."""
    from sqlalchemy.exc import IntegrityError

    from app.models.tracking import StatusSignal as StatusSignalRow

    def _row() -> StatusSignalRow:
        return StatusSignalRow(
            user_id=user.id,
            source=SignalSource.EMAIL_IMAP,
            kind=SignalKind.REJECTION,
            external_ref="imap-uid-42",
            sender="careers@acme-robotics.com",
            sender_domain="acme-robotics.com",
            subject="Update",
            snippet="moved forward with other candidates",
            received_at=NOW,
            confidence=0.95,
        )

    session.add(_row())
    await session.commit()

    session.add(_row())
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_a_re_sync_does_not_multiply_signals(session, user) -> None:
    """Counting rows after a simulated double scan — the property the constraint buys.

    Written with ``begin_nested`` because that is how ``StatusSyncService`` itself absorbs
    the conflict: a savepoint keeps the outer transaction usable, so one duplicate message
    does not abort the rest of the sync batch.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.tracking import StatusSignal as StatusSignalRow

    def _row() -> StatusSignalRow:
        return StatusSignalRow(
            user_id=user.id,
            source=SignalSource.EMAIL_IMAP,
            kind=SignalKind.INTERVIEW_INVITE,
            external_ref="imap-uid-7",
            sender="careers@acme-robotics.com",
            sender_domain="acme-robotics.com",
            subject="Interview",
            snippet="schedule an interview",
            received_at=NOW,
            confidence=0.9,
        )

    inserted = 0
    for _ in range(3):
        try:
            async with session.begin_nested():
                session.add(_row())
                await session.flush()
            inserted += 1
        except IntegrityError:
            # The expected outcome for attempts 2 and 3, and the whole point of the test:
            # the savepoint absorbs the conflict and the outer transaction stays usable.
            continue
    await session.commit()

    total = await session.scalar(
        select(func.count()).select_from(StatusSignalRow).where(StatusSignalRow.user_id == user.id)
    )
    assert inserted == 1, "more than one insert of the same message succeeded"
    assert total == 1


# ======================================================================================
# Privacy invariants (§17.8) — grep-verifiable, so grepped
# ======================================================================================


#: Mailbox-mutating call names that cannot mean anything else, forbidden package-wide.
UNIVERSALLY_FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {"send_message", "sendmail", "delete_message", "move_message", "uid_store", "trash"}
)

#: IMAP verbs forbidden inside the mail adapters — the sole place an IMAP connection exists.
#:
#: ``expunge`` and ``store`` are excluded from the package-wide check above because
#: ``session.expunge()`` detaches an ORM object and has nothing to do with mail. ``append``
#: and ``copy`` are IMAP verbs too, but ``list.append`` and ``dict.copy`` are so common that
#: including them would flag ordinary Python and teach everyone to ignore this test — the
#: read-only-selection check below covers that ground instead.
MAIL_ONLY_FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {"store", "expunge", "uid_copy", "add_flags", "remove_flags", "set_flags", "uid_expunge"}
)


def _mutating_calls(path, forbidden: frozenset[str]) -> list[str]:
    """Return every call in *path* whose name is in *forbidden*."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in forbidden:
            found.append(f"{path.name}:{node.lineno} calls {name}()")
    return found


def test_the_tracking_package_contains_no_mutating_mail_call() -> None:
    """§17.8 invariant 1: "no send, delete, move, or flag-modifying call — verifiable by grep"."""
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "app" / "tracking"
    offenders: list[str] = []
    for path in package.rglob("*.py"):
        offenders.extend(_mutating_calls(path, UNIVERSALLY_FORBIDDEN_CALLS))

    assert not offenders, "mutating mailbox calls found:\n  " + "\n  ".join(offenders)


def test_the_mail_adapters_contain_no_imap_mutation_verb() -> None:
    """The narrower, stronger check, scoped to where an IMAP connection actually exists."""
    from pathlib import Path

    mail = Path(__file__).resolve().parent.parent / "app" / "tracking" / "email"
    assert mail.is_dir(), "the mail adapter package moved; this test needs updating"

    offenders: list[str] = []
    for path in mail.rglob("*.py"):
        offenders.extend(
            _mutating_calls(path, MAIL_ONLY_FORBIDDEN_CALLS | UNIVERSALLY_FORBIDDEN_CALLS)
        )

    assert not offenders, "IMAP mutation verbs found:\n  " + "\n  ".join(offenders)


def _string_literals(path) -> list[str]:
    """Return every string constant in *path* that is not a docstring.

    Scope names have to be checked as *values*, not as text: the module docstrings in this
    package explain at length that ``mail.readwrite`` is never requested, and a plain
    substring search over the source would flag exactly the prose promising the opposite.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_mailboxes_are_opened_read_only() -> None:
    """§17.8 invariant 1: IMAP selects with ``readonly=True`` and the scopes are read-only."""
    from pathlib import Path

    mail = Path(__file__).resolve().parent.parent / "app" / "tracking" / "email"
    literals = [
        literal.lower() for path in mail.rglob("*.py") for literal in _string_literals(path)
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in mail.rglob("*.py"))

    assert "readonly" in source.lower(), "no read-only mailbox selection appears anywhere"

    for scope in ("gmail.modify", "gmail.compose", "gmail.send", "mail.readwrite", "mail.send"):
        offenders = [literal for literal in literals if scope in literal]
        assert not offenders, f"a read-write scope is requested: {scope} in {offenders}"


def test_the_requested_scopes_are_read_only() -> None:
    """The positive half: whatever scopes *are* named must be read-only ones."""
    from pathlib import Path

    mail = Path(__file__).resolve().parent.parent / "app" / "tracking" / "email"
    literals = [literal for path in mail.rglob("*.py") for literal in _string_literals(path)]
    scopes = [
        literal
        for literal in literals
        if "googleapis.com/auth/gmail" in literal or literal.lower().startswith("mail.")
    ]

    for scope in scopes:
        lowered = scope.lower()
        assert "readonly" in lowered or lowered in ("mail.read",), (
            f"non-read-only scope requested: {scope}"
        )


def test_snippets_are_bounded() -> None:
    """§17.8 invariant 3: full message bodies are never written to the database."""
    from app.tracking.base import SNIPPET_MAX_CHARS

    assert SNIPPET_MAX_CHARS <= 500


def test_the_llm_never_sees_more_than_a_bounded_body() -> None:
    """§17.4: the model sees the subject plus the first 2,000 characters of the body."""
    from app.tracking.base import LLM_MAX_BODY_CHARS

    assert LLM_MAX_BODY_CHARS <= 2000


# ======================================================================================
# Acceptance — the outcome the product names and the state machine could not reach
# ======================================================================================


def test_an_acceptance_is_not_read_as_a_fresh_offer(classifier) -> None:
    """The reason ``offer_accepted`` is a kind of its own.

    An acceptance confirmation restates the offer's whole vocabulary — "your offer", "accept"
    — so on score alone it classifies as ``offer``. The application would then be moved to
    ``offer``, which it already was, and "Accepted" would never appear anywhere.
    """
    result = classifier.classify_rules(
        _signal(
            subject="Your signed offer — Acme Robotics",
            body="Thank you, we have received your acceptance of the offer of employment. "
            "Welcome aboard!",
        )
    )

    assert result.kind is SignalKind.OFFER_ACCEPTED
    assert result.status is ApplicationStatus.ACCEPTED


def test_a_rejection_naming_an_acceptance_is_still_a_rejection(classifier) -> None:
    """"The position has been filled — another candidate accepted" is not good news.

    ``KIND_PRECEDENCE`` puts rejection above acceptance for exactly this shape, the same way
    it already outranks ``offer`` because "we will not be extending an offer" contains
    "extending an offer".
    """
    result = classifier.classify_rules(
        _signal(
            subject="Update on your application",
            body="We regret to inform you that the position has been filled — another "
            "candidate accepted the offer. We will not be moving forward.",
        )
    )

    assert result.kind is SignalKind.REJECTION
    assert result.status is ApplicationStatus.REJECTED


async def test_an_offer_can_be_accepted(session, application, make_posting) -> None:
    """``offer`` had no outgoing edge, so ``accepted`` was unreachable by any path."""
    from app.services.application_service import ApplicationService

    service = ApplicationService(session)
    application.status = ApplicationStatus.OFFER
    await session.commit()

    moved = await service.transition(application, ApplicationStatus.ACCEPTED)

    assert moved.status is ApplicationStatus.ACCEPTED
    assert moved.status.is_terminal()
    assert moved.status.is_post_submit()


async def test_accepting_is_the_end_of_the_funnel(session, application) -> None:
    """``accepted`` leads nowhere except back, and back only to ``offer``.

    The first version of this state asserted *no* outgoing edge at all, which is the natural
    reading of "end of the funnel" and was wrong: it made a mistaken acceptance permanent
    through every API the product has. The single edge back to ``offer`` is a correction
    path, and ``offer`` leads only to ``accepted``, so the pair is a closed loop that reaches
    no pre-submit state — which is what
    ``test_no_post_submit_state_can_reach_a_pre_submit_one`` proves for the whole graph.
    """
    from app.services.application_service import ALLOWED_TRANSITIONS

    assert ALLOWED_TRANSITIONS[ApplicationStatus.ACCEPTED] == {ApplicationStatus.OFFER}
    assert ApplicationStatus.ACCEPTED.is_terminal()


async def test_an_accepted_application_can_never_be_submitted_again(
    session, submission_allowed, posting, make_application, make_score, monkeypatch
) -> None:
    """Golden rule #1 covers the new state too, and by the same mechanism.

    ``accepted`` joining the post-submit band is what makes rung 1 of the submit ladder
    refuse it; adding an outcome state without adding it there would open a hole.
    """
    from app.services.pipeline import Pipeline

    await make_score(posting, normalized=95)
    application = await make_application(posting, status=ApplicationStatus.ACCEPTED)

    calls: list[object] = []

    class Spy:
        async def apply(self, ctx: object):
            calls.append(ctx)
            raise AssertionError("an accepted application was submitted again")

    monkeypatch.setattr(Pipeline, "_provider", staticmethod(lambda _name: Spy()))
    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert calls == []
    assert result.verdict == "already_applied"


def test_no_internal_status_is_missing_from_the_four() -> None:
    """A status with no mapping would render as a blank cell in the summary."""
    from app.models.enums import USER_FACING_STATUS

    assert set(USER_FACING_STATUS) == set(ApplicationStatus)


def test_the_four_categories_are_reachable() -> None:
    """Each of the product's four words has at least one internal status behind it."""
    from app.models.enums import USER_FACING_STATUS, UserFacingStatus

    covered = {value for value in USER_FACING_STATUS.values() if value is not None}
    assert covered == set(UserFacingStatus)


def test_an_application_that_was_never_sent_is_in_none_of_the_four() -> None:
    """Counting a crashed run as "Applied" would tell a user they applied when they did not."""
    assert ApplicationStatus.FAILED.user_facing() is None
    assert ApplicationStatus.ABANDONED.user_facing() is None


# ======================================================================================
# The acceptance classifier, after an adversarial review executed it and found four holes
# ======================================================================================


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("signature request", "Please return the signed offer letter by Friday and we'll "
                              "begin onboarding."),
        ("start date", "We are pleased to offer you the position. If you accept, your first "
                       "day will be 6 April 2026."),
        ("countersignature", "Sign electronically and we will return the countersigned offer "
                             "letter."),
        ("enthusiasm", "Let me know if you'd like to accept the offer; we're excited to have "
                       "you join us."),
    ],
)
def test_an_offer_being_made_is_never_read_as_one_already_taken(
    classifier, label: str, body: str
) -> None:
    """All four reproduce a real misclassification, verified by running the classifier.

    "signed offer letter", "your first day", "countersigned" and "excited to have you join"
    are the vocabulary of an offer *letter*, not of an acceptance. Because ``KIND_PRECEDENCE``
    ranks ``offer_accepted`` above ``offer``, matching them strongly did not merely invent an
    acceptance — it stopped genuine offers registering as offers at all, breaking the single
    most valuable signal the mailbox sync reads.
    """
    result = classifier.classify_rules(_signal(subject="Your offer", body=body))

    assert result.kind is not SignalKind.OFFER_ACCEPTED, label
    assert result.status is not ApplicationStatus.ACCEPTED


def test_a_conditional_acceptance_is_not_an_acceptance(classifier) -> None:
    """"Once you have accepted" is a negotiation, not a completed act.

    It classified as ``offer_accepted`` at 0.97 and auto-applied an irreversible status.
    """
    result = classifier.classify_rules(
        _signal(
            subject="Next steps",
            body="Great speaking today. Once you have accepted, HR will reach out about "
            "onboarding and benefits enrolment.",
        )
    )

    assert result.status is not ApplicationStatus.ACCEPTED


def test_someone_elses_acceptance_is_not_the_users(classifier) -> None:
    """A rejection naming the winning candidate must not record the user as hired.

    ``KIND_PRECEDENCE`` does not save this: a kind whose matches are all WEAK is discarded
    *before* precedence is consulted, so a rejection carrying only "unfortunately" loses to a
    strong ``accepted our offer``. The fix is that third-party phrasing is no longer strong —
    it never identifies who accepted.
    """
    result = classifier.classify_rules(
        _signal(
            subject="Thank you for your interest",
            body="The candidate we selected has accepted our offer, so we are closing this "
            "requisition. Unfortunately we cannot consider you further.",
        )
    )

    assert result.status is not ApplicationStatus.ACCEPTED


def test_a_genuine_acceptance_still_classifies(classifier) -> None:
    """The tightening must not have thrown away the signal it exists to detect."""
    for body in (
        "We have received your acceptance and your signed offer letter. Welcome to the team!",
        "Thank you for accepting our offer. Onboarding details to follow.",
    ):
        result = classifier.classify_rules(_signal(subject="Welcome", body=body))
        assert result.kind is SignalKind.OFFER_ACCEPTED
        assert result.status is ApplicationStatus.ACCEPTED


def test_an_acceptance_is_never_applied_without_a_human(session, settings) -> None:
    """Golden rule #2 for the one status nothing can walk back.

    Every other status a signal can produce is recoverable — a wrong ``rejected`` still has
    edges to ``interview`` and ``offer``. A wrong ``accepted`` records a hire that did not
    happen, in the analytics, in the memory weights and on the dashboard. So it parks for
    one-click confirmation however confident the phrase match was.
    """
    from app.tracking.service import NEVER_AUTO_APPLIED

    assert ApplicationStatus.ACCEPTED in NEVER_AUTO_APPLIED
    assert ApplicationStatus.REJECTED not in NEVER_AUTO_APPLIED
    assert ApplicationStatus.OFFER not in NEVER_AUTO_APPLIED


async def test_a_wrong_acceptance_can_be_corrected(session, application) -> None:
    """``accepted`` had no outgoing edge, so a mistake was permanent through every API."""
    from app.services.application_service import ApplicationService

    service = ApplicationService(session)
    application.status = ApplicationStatus.ACCEPTED
    await session.commit()

    moved = await service.transition(application, ApplicationStatus.OFFER)

    assert moved.status is ApplicationStatus.OFFER


def test_no_post_submit_state_can_reach_a_pre_submit_one() -> None:
    """Golden rule #1, proved by reachability rather than by inspection.

    Adding ``accepted`` added two edges to the state machine, and the correction edge back to
    ``offer`` is the kind of change that quietly opens a path to re-submission. This walks the
    whole graph instead of trusting a reading of it, so any future edge that opens one fails
    here rather than in production.
    """
    from app.models.enums import APPLICATION_PRE_SUBMIT_STATES
    from app.services.application_service import ALLOWED_TRANSITIONS

    def reachable(start: ApplicationStatus) -> set[ApplicationStatus]:
        seen: set[ApplicationStatus] = set()
        stack = [start]
        while stack:
            for nxt in ALLOWED_TRANSITIONS.get(stack.pop(), set()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    for state in APPLICATION_POST_SUBMIT_STATES:
        assert not reachable(state) & APPLICATION_PRE_SUBMIT_STATES, state.value


# ======================================================================================
# IMAP folder names — found by pointing the real adapter at a real Gmail account
# ======================================================================================


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("INBOX", "INBOX"),
        ("[Gmail]/All Mail", '"[Gmail]/All Mail"'),
        ("[Gmail]/Sent Mail", '"[Gmail]/Sent Mail"'),
        ("My Label", '"My Label"'),
        # Already quoted by the caller: left alone, so this is safe to apply unconditionally.
        ('"[Gmail]/Spam"', '"[Gmail]/Spam"'),
        # RFC 3501 requires these escaped inside a quoted string.
        ('Odd"Name', '"Odd\\"Name"'),
        ("Back\\slash", '"Back\\\\slash"'),
        ("", '""'),
    ],
)
def test_a_folder_name_with_a_space_is_quoted(folder: str, expected: str) -> None:
    """``imaplib`` passes a mailbox name through untouched, and IMAP tokenises on whitespace.

    Found by running the real adapter against a real Gmail account: selecting
    ``[Gmail]/All Mail`` sent ``EXAMINE [Gmail]/All Mail`` — three tokens — and the server
    answered ``BAD Could not parse command``. That surfaced as a
    ``MailboxUnavailableError`` which aborts the **whole sync**, not just that folder, so any
    user who configured All Mail, Sent Mail, or a label with a space in it got no status sync
    at all and an error that named the folder without explaining it.
    """
    from app.tracking.email.imap import _quote_folder

    assert _quote_folder(folder) == expected


def test_the_default_folder_needs_no_quoting() -> None:
    """The common case must not gain quotes it does not need.

    Servers accept a quoted ``"INBOX"``, but changing the wire format of the one folder
    every install uses, to fix a bug in the folders it does not, is a poor trade.
    """
    from app.tracking.email.base import DEFAULT_FOLDERS
    from app.tracking.email.imap import _quote_folder

    for folder in DEFAULT_FOLDERS:
        assert _quote_folder(folder) == folder
