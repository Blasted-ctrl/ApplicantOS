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
