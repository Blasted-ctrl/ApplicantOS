"""The status-tracking extension point (``docs/CONTRACTS.md`` §17.3).

An application's outcome is a fact that lives somewhere else — in a rejection email, in an
interview invitation, in an ATS portal the user is already logged into. This package's job
is to go and read it, so that "we learn what gets interviews" is true without the user
typing a single status by hand.

A :class:`StatusTracker` is the plugin kind that does the reading. It is deliberately the
*narrowest* possible interface: given a user and a point in time, yield
:class:`StatusSignal` values. It does not classify them, it does not match them to an
application, and it does not write anything. Those are three separate, individually
testable steps:

.. code-block:: text

    StatusTracker.poll  ->  StatusSignal      (what arrived)
    StatusClassifier    ->  Classification    (what it means)
    SignalMatcher       ->  Match             (which application it is about)
    StatusSyncService   ->  Application       (the only thing that writes)

Everything in this module is a transport shape: plain dataclasses with no ORM, no session
and no I/O, so a tracker and a classifier can both be unit-tested with nothing but a
constructor call.

Why email, and only email
-------------------------

Every ATS — and LinkedIn itself — notifies by email, so one email pipeline covers LinkedIn,
Glassdoor, Indeed and every ATS at once, including the ones ApplicantOS never integrated.
Glassdoor has no application-status surface at all and LinkedIn's terms prohibit scraping,
so **neither is scraped** (golden rule #10). The optional portal tracker adds direct checks
only where a browser session already exists.

Two invariants this module exists to enforce
--------------------------------------------

**Never guess** (golden rule #2). A :class:`Classification` carries a confidence and an
``evidence`` string, a :class:`Match` carries a confidence and structured evidence, and
anything below ``settings.status_sync_min_confidence`` is stored for one-click human
confirmation rather than applied. A wrong automatic rejection is far worse than a question.

**Minimal retention** (``docs/CONTRACTS.md`` §17.8.3). :class:`StatusSignal` carries a full
message body because classification needs one, but it is a *transport* object: it lives in
memory for the duration of a sync and the persisted row keeps only
:attr:`StatusSignal.snippet` — capped at :data:`SNIPPET_MAX_CHARS`. Full bodies are never
written to the database. :meth:`StatusSignal.log_context` exists so that logging a signal
cannot accidentally log its body, its sender or its subject (§17.8.5).
"""

from __future__ import annotations

import abc
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

import structlog

from app.models.enums import ApplicationStatus, PluginKind, SignalKind, SignalSource
from app.plugins.base import BasePlugin, PluginLoadError, PluginMeta

__all__ = [
    "ATS_RELAY_DOMAINS",
    "CONFIDENCE_MAX",
    "CONFIDENCE_MIN",
    "LLM_ESCALATION_CONFIDENCE",
    "LLM_MAX_BODY_CHARS",
    "MAX_REPORT_ERRORS",
    "SNIPPET_MAX_CHARS",
    "Classification",
    "Match",
    "StatusSignal",
    "StatusTracker",
    "SyncReport",
    "TrackerAuthError",
    "TrackerConfigurationError",
    "TrackerError",
    "TrackerUnavailableError",
    "is_relay_domain",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Hard cap on the message excerpt that may be persisted with a signal
#: (``docs/CONTRACTS.md`` §17.2 / §17.8.3). The full body never leaves memory.
SNIPPET_MAX_CHARS: Final[int] = 500

#: Most of a message body the LLM classifier is ever allowed to see (§17.4). Rules run
#: first and are usually enough; when the LLM is consulted it gets the subject plus this
#: many characters and nothing more.
LLM_MAX_BODY_CHARS: Final[int] = 2_000

#: Lower bound of every confidence score in this package.
CONFIDENCE_MIN: Final[float] = 0.0

#: Upper bound of every confidence score in this package.
CONFIDENCE_MAX: Final[float] = 1.0

#: Rule confidence at or above which the LLM is never consulted (§17.4). Below it — or on
#: :attr:`SignalKind.UNKNOWN` — the classifier may escalate, but the model may never
#: overturn a high-confidence rule match.
LLM_ESCALATION_CONFIDENCE: Final[float] = 0.7

#: Ceiling on the errors retained in one :class:`SyncReport`. A mailbox that fails on every
#: message must not turn a report into a memory leak or an unbounded API payload.
MAX_REPORT_ERRORS: Final[int] = 20

#: Domains that belong to an applicant-tracking system rather than to an employer
#: (``docs/CONTRACTS.md`` §17.5). Mail from one of these identifies the *ATS*, not the
#: company, so the matcher must read the employer from the display name, the reply-to or
#: the body instead of from the sender domain.
#:
#: They are also unconditionally added to the mailbox sender allowlist: a Greenhouse
#: rejection arrives from ``no-reply@greenhouse.io``, never from the employer's own domain,
#: so without these entries the narrow search (§17.8.2) would miss most outcomes.
ATS_RELAY_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "greenhouse.io",
        "greenhouse-mail.io",
        "lever.co",
        "hire.lever.co",
        "ashbyhq.com",
        "myworkday.com",
        "myworkdayjobs.com",
        "icims.com",
        "smartrecruiters.com",
        "workable.com",
        "workablemail.com",
        "jobvite.com",
        "taleo.net",
        "successfactors.com",
        "bamboohr.com",
        "linkedin.com",
        "indeed.com",
        "glassdoor.com",
    }
)

#: Collapses every run of whitespace — including the hard newlines of a quoted-printable
#: body — to a single space, so a snippet is 500 characters of text rather than 500
#: characters of line breaks.
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")


def is_relay_domain(domain: str) -> bool:
    """Return whether *domain* belongs to an ATS relay rather than to an employer.

    Matching is suffix-aware: ``us-east.greenhouse.io`` and ``mail.lever.co`` are relays
    just as much as their apex domains, because ATS vendors send from per-region and
    per-tenant subdomains.

    Args:
        domain: A sender domain, in any case, with or without a leading ``@``.

    Returns:
        ``True`` when the domain is, or is a subdomain of, a known ATS relay.
    """
    normalized = domain.strip().lower().lstrip("@").rstrip(".")
    if not normalized:
        return False
    return any(
        normalized == relay or normalized.endswith(f".{relay}") for relay in ATS_RELAY_DOMAINS
    )


def _clamp_confidence(value: float) -> float:
    """Force *value* into ``[0.0, 1.0]``.

    Confidence reaches this package from rule tables, fuzzy string ratios and an LLM, and
    every one of them can produce an out-of-range number. Clamping here means the
    ``status_sync_min_confidence`` comparison downstream is always meaningful.

    Args:
        value: A raw score.

    Returns:
        The score, clamped to the closed unit interval. A non-numeric value yields
        :data:`CONFIDENCE_MIN`, which is the safe direction — it routes to human review.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return CONFIDENCE_MIN
    if numeric != numeric:  # NaN, which compares false against every threshold
        return CONFIDENCE_MIN
    return max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, numeric))


def _as_utc(value: datetime) -> datetime:
    """Return *value* as a timezone-aware UTC datetime.

    Mail sources disagree about tzinfo — IMAP hands back a parsed ``Date:`` header that may
    be naive, Gmail an epoch in milliseconds, Graph an ISO-8601 string with ``Z``. Every
    comparison in this package (the lookback floor, "was the application submitted before
    this arrived?") is between datetimes, so they are normalised on the way in.

    Args:
        value: A datetime, aware or naive. Naive values are assumed to be UTC.

    Returns:
        The equivalent aware datetime in UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ======================================================================================
# Errors
# ======================================================================================


class TrackerError(Exception):
    """Base class for every status-tracking failure.

    Attributes:
        tracker: Name of the tracker plugin that failed, when attributable to one.
        transient: Whether retrying later is likely to succeed. A rate limit or a network
            blip is transient; a revoked OAuth grant is not, and the worker must not
            re-queue it forever.
    """

    def __init__(
        self,
        message: str,
        *,
        tracker: str | None = None,
        transient: bool = False,
    ) -> None:
        """Record the failure together with its provenance and retry posture.

        Args:
            message: Operator-facing explanation. Never include a message body, an email
                address or a token here — this string is logged and surfaced in the API.
            tracker: Name of the tracker involved, if known.
            transient: Whether a later retry is worth attempting.
        """
        super().__init__(message)
        self.tracker = tracker
        self.transient = transient


class TrackerAuthError(TrackerError):
    """The mailbox or portal rejected our credentials.

    Raised for a revoked OAuth grant, an expired refresh token, or an IMAP app password the
    user has rotated. Never transient: the account is disabled and the user is asked to
    reconnect, because retrying a bad credential in a loop is how accounts get locked.
    """


class TrackerUnavailableError(TrackerError):
    """The source could not be reached, or refused to answer right now.

    Network failures, timeouts, 5xx responses and rate limits. Transient by default — the
    next scheduled poll picks up exactly where this one stopped, because the cursor is only
    advanced for work that actually completed.
    """

    def __init__(
        self,
        message: str,
        *,
        tracker: str | None = None,
        transient: bool = True,
    ) -> None:
        """Construct an availability failure, transient unless told otherwise.

        Args:
            message: Operator-facing explanation.
            tracker: Name of the tracker involved, if known.
            transient: Whether a later retry is worth attempting.
        """
        super().__init__(message, tracker=tracker, transient=transient)


class TrackerConfigurationError(TrackerError):
    """The tracker cannot run because of how it is configured, not because of a fault.

    A missing OAuth client id, an unset IMAP host, or an absent ``keyring`` package. The
    fix is an operator action, so this never retries and the message says exactly what to
    do.
    """


# ======================================================================================
# Transport shapes (docs/CONTRACTS.md §17.3)
# ======================================================================================


@dataclass(slots=True)
class StatusSignal:
    """One piece of evidence that an application's state may have changed.

    This is the pre-persistence transport shape: what a tracker read, before anything has
    decided what it means or which application it belongs to. The corresponding
    ``status_signals`` row keeps only :attr:`snippet`, never :attr:`body` (§17.8.3).

    Attributes:
        source: Where this came from — which mailbox provider, the ATS portal, a webhook,
            or the user.
        external_ref: The source's own identifier for the message: an IMAP UID, a Gmail
            message id, a Graph message id. Combined with :attr:`source` it is the
            idempotency key (``UNIQUE(user_id, source, external_ref)``), which is what makes
            a re-run of an overlapping window free instead of duplicative.
        received_at: When the message arrived, in UTC. Used to bound matching: an
            application must have been submitted *before* the signal arrived.
        sender: The bare sender address.
        sender_domain: The sender's domain, lowercased — the strongest matching signal when
            it is an employer, and a hint that the company must be read from elsewhere when
            it is an ATS relay (:func:`is_relay_domain`).
        subject: The message subject.
        body: The message text, already flattened out of MIME and capped by the mailbox
            adapter. Memory-only; never persisted.
        company_hint: Employer name the adapter could read from the display name or the
            reply-to, when the sender domain is a relay.
        title_hint: Posting title the adapter could read out of the subject, if any.
        raw: Provider-specific extras a classifier or matcher may want — thread id, labels,
            list-id. Small scalars only; not a place to stash the body.
    """

    source: SignalSource
    external_ref: str
    received_at: datetime
    sender: str
    sender_domain: str
    subject: str
    body: str
    company_hint: str | None = None
    title_hint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise the fields every downstream comparison depends on.

        Coerces :attr:`source` to its enum member, forces :attr:`received_at` to aware UTC,
        lowercases :attr:`sender` and :attr:`sender_domain` (mail addresses are compared
        case-insensitively and domains are case-insensitive by definition), and drops
        hints that are empty or whitespace so that ``company_hint or fallback`` behaves.
        """
        self.source = SignalSource(self.source)
        self.received_at = _as_utc(self.received_at)
        self.external_ref = self.external_ref.strip()
        self.sender = self.sender.strip().lower()
        self.sender_domain = self.sender_domain.strip().lower().lstrip("@").rstrip(".")
        self.subject = self.subject.strip()
        self.company_hint = (self.company_hint or "").strip() or None
        self.title_hint = (self.title_hint or "").strip() or None

    @property
    def snippet(self) -> str:
        """The only part of :attr:`body` that may ever be written to the database.

        Whitespace-collapsed and capped at :data:`SNIPPET_MAX_CHARS`, so the persisted row
        is a human-readable excerpt — enough for the review screen to show *why* a status
        was proposed — and not a copy of the user's mail.
        """
        return _WHITESPACE_RUN.sub(" ", self.body).strip()[:SNIPPET_MAX_CHARS]

    @property
    def is_from_relay(self) -> bool:
        """Whether the sender domain belongs to an ATS rather than to an employer."""
        return is_relay_domain(self.sender_domain)

    @property
    def dedupe_key(self) -> tuple[SignalSource, str]:
        """The ``(source, external_ref)`` pair that makes a signal unique for one user."""
        return (self.source, self.external_ref)

    def for_llm(self, *, max_body_chars: int = LLM_MAX_BODY_CHARS) -> str:
        """Render the *only* view of this signal a language model is allowed to see.

        ``docs/CONTRACTS.md`` §17.4 caps the model's view at the subject plus the first
        2,000 characters of the body. Going through this method rather than formatting a
        prompt by hand is what keeps that cap from drifting, and it means a marketing email
        with a 200KB HTML payload costs the same as a two-line rejection.

        Args:
            max_body_chars: Override for the body cap; defaults to
                :data:`LLM_MAX_BODY_CHARS`.

        Returns:
            A ``Subject:``/``Body:`` block, never longer than the subject plus
            *max_body_chars*.
        """
        limit = max(0, int(max_body_chars))
        return f"Subject: {self.subject}\n\nBody:\n{self.body[:limit]}"

    def log_context(self) -> dict[str, Any]:
        """Return the fields that are safe to log about this signal (§17.8.5).

        Privacy invariant: never log a body, an address or a subject. What remains is an
        identifier and a provenance, which is everything an operator needs to correlate a
        log line with a ``status_signals`` row and nothing they need protecting from.

        Returns:
            A structlog-ready mapping of ``source`` and ``external_ref`` only.
        """
        return {"source": str(self.source), "external_ref": self.external_ref}


@dataclass(slots=True)
class Classification:
    """What a :class:`StatusSignal` means, and how sure we are (``§17.4``).

    Attributes:
        kind: The recognised event. :attr:`SignalKind.UNKNOWN` is a legitimate, common
            answer — most mail in any mailbox is not about a job application.
        status: The :class:`~app.models.enums.ApplicationStatus` this event implies, or
            ``None`` when it implies no change (a "your application was viewed" notice is
            informative but moves nothing).
        confidence: ``0.0``–``1.0``. Compared against ``settings.status_sync_min_confidence``
            to decide between applying the change and asking the user.
        evidence: The human-readable reason — the phrase that matched, or the model's
            one-line justification. Shown verbatim in the review screen, so a user can see
            *why* something was proposed rather than being asked to trust it.
        matched_rule: Identifier of the deterministic rule that fired, when one did.
        model_used: Name of the LLM consulted, or ``None`` when rules alone decided. Rules
            deciding alone is the common and preferred case: it is free, instant and
            reproducible.
    """

    kind: SignalKind
    status: ApplicationStatus | None = None
    confidence: float = CONFIDENCE_MIN
    evidence: str = ""
    matched_rule: str | None = None
    model_used: str | None = None

    def __post_init__(self) -> None:
        """Coerce the enum fields and clamp :attr:`confidence` into ``[0.0, 1.0]``."""
        self.kind = SignalKind(self.kind)
        if self.status is not None:
            self.status = ApplicationStatus(self.status)
        self.confidence = _clamp_confidence(self.confidence)
        self.evidence = self.evidence.strip()

    @property
    def is_actionable(self) -> bool:
        """Whether this classification implies a status change at all.

        ``False`` for :attr:`SignalKind.UNKNOWN` and for kinds that carry no status — the
        service records them for the audit trail without touching the application.
        """
        return self.status is not None and self.kind is not SignalKind.UNKNOWN

    @property
    def should_escalate_to_llm(self) -> bool:
        """Whether the LLM may be consulted about this rule result (§17.4).

        Only ``unknown`` results and low-confidence matches escalate. A high-confidence
        rule match is final: the model is never given the opportunity to overturn a
        deterministic answer, which is what keeps classification reproducible.
        """
        return self.kind is SignalKind.UNKNOWN or self.confidence < LLM_ESCALATION_CONFIDENCE

    def is_confident(self, threshold: float) -> bool:
        """Return whether :attr:`confidence` reaches *threshold*.

        Args:
            threshold: Usually ``settings.status_sync_min_confidence``.

        Returns:
            ``True`` when the classification may be applied without asking the user.
        """
        return self.confidence >= _clamp_confidence(threshold)


@dataclass(slots=True)
class Match:
    """Which application a signal is about (``§17.5``).

    Attributes:
        application_id: The best candidate, or ``None`` when nothing matched well enough to
            name. ``None`` is not a failure — an unmatched rejection from a company the
            user never applied to through ApplicantOS is simply not ours.
        confidence: ``0.0``–``1.0``, from the scoring in §17.5.
        evidence: Structured record of *what* matched — which domain, which company name,
            which confirmation id. Persisted to ``status_signals.match_evidence`` so the
            review screen can justify the proposal and so a mis-match can be debugged after
            the fact.
    """

    application_id: uuid.UUID | None = None
    confidence: float = CONFIDENCE_MIN
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Clamp :attr:`confidence` and coerce a stringified UUID to a real one."""
        self.confidence = _clamp_confidence(self.confidence)
        if self.application_id is not None and not isinstance(self.application_id, uuid.UUID):
            self.application_id = uuid.UUID(str(self.application_id))

    @property
    def matched(self) -> bool:
        """Whether an application was identified at all."""
        return self.application_id is not None

    def is_confident(self, threshold: float) -> bool:
        """Return whether this match may be acted on without human confirmation.

        Args:
            threshold: Usually ``settings.status_sync_min_confidence``.

        Returns:
            ``True`` only when an application was identified *and* the score reaches
            *threshold*. An unmatched signal is never confident, whatever its score.
        """
        return self.matched and self.confidence >= _clamp_confidence(threshold)


@dataclass(slots=True)
class SyncReport:
    """What one sync run did, for the API, the events and the logs.

    The counters are deliberately a funnel — ``fetched >= classified >= matched >=
    applied`` — so a glance at a report says where signals are being lost: a mailbox that
    fetches 400 and applies 0 is a matching problem, one that fetches 0 is a connection
    problem.

    Attributes:
        account_id: The ``email_accounts`` row this run polled, or ``None`` for an
            aggregate across accounts.
        fetched: Messages read from the source.
        classified: Messages that produced a non-``unknown`` classification.
        matched: Signals bound to an application.
        applied: Applications whose status actually changed.
        needs_review: Signals parked for one-click human confirmation.
        skipped: Messages ignored as already-seen or irrelevant.
        duration_seconds: Wall-clock duration of the run.
        errors: Operator-facing failure messages, capped at :data:`MAX_REPORT_ERRORS`.
    """

    account_id: uuid.UUID | None = None
    fetched: int = 0
    classified: int = 0
    matched: int = 0
    applied: int = 0
    needs_review: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the run completed without recording an error."""
        return not self.errors

    def record_error(self, message: str) -> None:
        """Append *message* to :attr:`errors`, bounded.

        A mailbox that fails on every one of 500 messages would otherwise produce a 500-item
        list that goes into a WebSocket event and an API response. Past the cap the count is
        summarised instead.

        Args:
            message: Operator-facing text. Must not contain a body, an address or a token.
        """
        text = message.strip()
        if not text:
            return
        if len(self.errors) < MAX_REPORT_ERRORS:
            self.errors.append(text)
        elif len(self.errors) == MAX_REPORT_ERRORS:
            self.errors.append(f"… further errors suppressed after {MAX_REPORT_ERRORS}")

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready view for ``tracking.sync_finished`` and the sync endpoint."""
        return {
            "account_id": str(self.account_id) if self.account_id else None,
            "fetched": self.fetched,
            "classified": self.classified,
            "matched": self.matched,
            "applied": self.applied,
            "needs_review": self.needs_review,
            "skipped": self.skipped,
            "duration_seconds": round(self.duration_seconds, 3),
            "errors": list(self.errors),
        }


# ======================================================================================
# The plugin
# ======================================================================================


class StatusTracker(BasePlugin, abc.ABC):
    """A source of application-status signals (``PluginKind.TRACKER``).

    Concrete trackers live in ``app/tracking/trackers/`` and are reached through
    :mod:`app.plugins.registry`, never by importing the module (golden rule #5)::

        from app.models.enums import PluginKind
        from app.plugins.registry import registry

        tracker = registry.get(PluginKind.TRACKER, "email")
        async for signal in tracker.poll(user_id, since=last_sync):
            ...

    A tracker reads and yields. It must not write to the database, must not decide what a
    signal means, and must not widen its own query: the window and the sender allowlist it
    is given are privacy boundaries (``docs/CONTRACTS.md`` §17.8.2), not hints.
    """

    meta: ClassVar[PluginMeta]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a tracker whose ``meta`` claims a kind other than ``TRACKER``.

        Only classes that declare ``meta`` directly are checked, so abstract intermediates
        (and this class itself) pass through untouched. Catching the mistake at class
        definition turns a mis-declared plugin into an import-time error with a precise
        message instead of a ``PluginNotFound`` much later.

        Args:
            **kwargs: Forwarded to :meth:`object.__init_subclass__`.

        Raises:
            PluginLoadError: If ``meta.kind`` is not :attr:`PluginKind.TRACKER`.
        """
        super().__init_subclass__(**kwargs)
        meta = cls.__dict__.get("meta")
        if isinstance(meta, PluginMeta) and meta.kind is not PluginKind.TRACKER:
            raise PluginLoadError(
                f"{cls.__name__} subclasses StatusTracker but declares "
                f"kind={meta.kind!r}; it must be {PluginKind.TRACKER!r}",
                kind=meta.kind,
                name=meta.name,
            )

    @abc.abstractmethod
    def poll(
        self,
        user_id: uuid.UUID,
        *,
        since: datetime | None = None,
    ) -> AsyncIterator[StatusSignal]:
        """Yield every status signal for *user_id* that arrived after *since*.

        Implemented as an ``async def`` containing ``yield``, which makes it an async
        generator function: it is therefore declared here without ``async`` so that the
        signature matches what subclasses actually write, exactly as
        :meth:`app.jobs.base.ATSProvider.search` does.

        Implementations must:

        * **Read nothing until the user has connected a source.** A tracker with no
          configured account yields nothing; it does not go looking (§17.8.6).
        * **Stay inside the window.** ``since`` is a floor, not a target, and it is
          additionally clamped to ``settings.status_sync_lookback_days``. There is no
          "just this once" full sweep.
        * **Stay inside the allowlist.** Queries are constrained to the domains of
          companies the user actually applied to plus :data:`ATS_RELAY_DOMAINS`.
        * **Be resumable.** Yield in ascending :attr:`StatusSignal.received_at` order and
          advance the persisted cursor only for work that completed, so a crash mid-run
          resumes rather than restarts (golden rule #8).
        * **Be read-only.** No send, delete, move, or flag-modifying call may exist
          anywhere in the implementation (§17.8.1).

        Args:
            user_id: Owner of the mailbox or portal session being polled.
            since: Lower bound on message arrival time. ``None`` means "use the configured
                lookback window", which is also the ceiling when a value is supplied.

        Yields:
            :class:`StatusSignal` values, oldest first.

        Raises:
            TrackerAuthError: If the source rejected our credentials.
            TrackerConfigurationError: If the tracker is not configured to run.
            TrackerUnavailableError: If the source could not be reached.
        """
        raise NotImplementedError

    async def collect(
        self,
        user_id: uuid.UUID,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[StatusSignal]:
        """Drain :meth:`poll` into a list, optionally stopping after *limit* signals.

        A convenience for callers that are not streaming — the ``/tracking/sync`` endpoint
        and the tests — and the correct way to bound a poll, because it stops consuming the
        generator rather than letting the source produce work nobody will read.

        Args:
            user_id: Owner of the source being polled.
            since: Lower bound on message arrival time.
            limit: Maximum number of signals to return. ``None`` means no cap beyond the
                tracker's own.

        Returns:
            The collected signals, oldest first.
        """
        collected: list[StatusSignal] = []
        ceiling = None if limit is None else max(0, int(limit))
        if ceiling == 0:
            return collected
        async for signal in self.poll(user_id, since=since):
            collected.append(signal)
            if ceiling is not None and len(collected) >= ceiling:
                break
        return collected

    async def healthcheck(self) -> bool:
        """Report whether this tracker could poll right now.

        The default is ``True`` — a tracker with nothing configured is not *unhealthy*, it
        simply has nothing to do, and reporting otherwise would make ``GET /ready`` red for
        every user who has not connected a mailbox. Trackers that hold a live connection
        override this to probe it.

        Returns:
            ``True`` when the tracker is usable.
        """
        return True
