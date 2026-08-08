"""Which application a signal is about (``docs/CONTRACTS.md`` §17.5).

Classification says *what happened*; this module says *to what*. It is the harder half, and
it is where a mistake is unrecoverable in a way a misclassification is not: marking the wrong
application rejected destroys a real opportunity and destroys the user's trust in every other
number the product shows them.

The failure mode this module exists to defeat
---------------------------------------------

**A Greenhouse rejection arrives from ``no-reply@greenhouse.io``, not from
``@microsoft.com``.** The sender domain of most job mail identifies the applicant-tracking
system, not the employer, so the single strongest matching signal — sender domain equals
company domain — is simply *absent* on the majority of real messages. An implementation that
leans on it matches the handful of employers who send from their own domain and silently
misses everything else, or, worse, matches every Greenhouse-relayed message to whichever
application happens to be first.

:data:`~app.tracking.base.ATS_RELAY_DOMAINS` is therefore consulted *before* the domain test,
and for a relay sender the employer is read from the display name, the reply-to address, or
the body — never from the sender domain.

How the body is used, and why it is used backwards
--------------------------------------------------

The obvious approach — extract a company name from the message and look it up — is a named
entity recognition problem with a long tail of failures. The reliable approach is the
inverse: take each of the user's candidate applications, and ask whether *that* company's
name appears in this message. There are rarely more than a few hundred candidates, the test
is a substring check on normalised text, and it cannot invent an employer that does not
exist in the user's own history.

Scoring, and the two ways it refuses to answer
-----------------------------------------------

Evidence is additive and every contribution is named in :attr:`Match.evidence`, so a match is
explainable after the fact rather than a bare number. Two guards make "never guess" real:

* **The floor.** Below :data:`MIN_MATCH_SCORE` nothing is bound at all — the signal is stored
  unmatched, and the user is asked.
* **The tie.** When the best two candidates are within :data:`TIE_MARGIN` of each other the
  confidence is capped at :data:`AMBIGUOUS_CEILING`, which is below every sane
  ``status_sync_min_confidence``. Two open applications at the same employer is a common,
  ordinary situation, and picking one of them by a hundredth of a point is precisely the
  guess golden rule #2 forbids.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import select

from app.jobs.dedupe import normalize_company, normalize_title
from app.models.application import Application
from app.models.enums import ATSProviderName
from app.tracking.base import ATS_RELAY_DOMAINS, Classification, Match, StatusSignal

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config.settings import Settings
    from app.models.company import Company
    from app.models.posting import JobPosting

__all__ = [
    "AMBIGUOUS_CEILING",
    "CLOCK_GRACE_MINUTES",
    "MAX_CANDIDATES",
    "MAX_MATCH_SCORE",
    "MIN_COMPANY_TOKEN_CHARS",
    "MIN_EXTERNAL_ID_CHARS",
    "MIN_MATCH_SCORE",
    "RELAY_TO_PROVIDER",
    "TIE_MARGIN",
    "WEIGHTS",
    "SignalMatcher",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Tuning constants
# ======================================================================================

#: Confidence returned when an employer-issued identifier from the application appears
#: verbatim in the message. §17.5 calls this decisive, and it is: the ATS generated that
#: string for this one application and put it in this one email.
_DECISIVE: Final[float] = 0.99

#: Contribution of each kind of evidence. Tuned so that the two combinations that occur in
#: real mail both clear a default ``status_sync_min_confidence`` of 0.80:
#:
#: * employer-sent — ``sender_domain`` (0.55) + ``company_body`` (0.30) + recency ≈ 0.90
#: * ATS-relayed — ``company_hint`` (0.42) + ``company_body`` (0.30) + ``title`` (0.22) ≈ 0.94
#:
#: and so that *any single one of them alone* stays below it. One coincidence is never
#: enough; two independent coincidences about the same application are.
WEIGHTS: Final[dict[str, float]] = {
    # The sender is the employer's own mail domain. Strongest non-decisive evidence, and
    # only ever awarded when the sender is not an ATS relay.
    "sender_domain": 0.55,
    # Reply-to is the employer's domain even though the envelope sender is a relay — which
    # is how a recruiter's "reply to me directly" configuration reaches us.
    "reply_to_domain": 0.50,
    # The employer's name is in the display name or the reply-to name. On relayed mail this
    # is where the company actually lives, so it outweighs a body mention.
    "company_hint": 0.42,
    # The employer's name appears in the subject.
    "company_subject": 0.34,
    # The employer's name appears in the body.
    "company_body": 0.30,
    # A partial (token-overlap) name match, in any of the above.
    "company_partial": 0.16,
    # The posting's title appears in the subject.
    "title_subject": 0.26,
    # The posting's title appears in the body.
    "title_body": 0.22,
    # Most of the title's tokens appear.
    "title_partial": 0.11,
    # The relay the message came from is the ATS this posting was discovered on. Weak by
    # itself — the user may have twenty Greenhouse applications — but it discriminates
    # sharply between candidates once a name or title has narrowed the field.
    "relay_provider": 0.12,
    # Both the employer *and* the role are named in the same message. Worth more than the
    # sum of its parts, and it is the signal that makes a body-only ATS rejection —
    # "your application for Senior Backend Engineer at Acme Robotics" from
    # no-reply@greenhouse.io — actionable, which is the single most common real message
    # this whole feature exists to read.
    "company_title_pair": 0.16,
    # The message arrived soon after the application was sent. Scaled continuously; the
    # constant here is the maximum.
    "recency": 0.08,
}

#: Below this a signal is stored with no application bound at all. A single weak coincidence
#: is not a match, and an unbound signal in the review queue is far better than a bound one
#: that points at the wrong job.
MIN_MATCH_SCORE: Final[float] = 0.30

#: Ceiling on a non-decisive score, so evidence stacking cannot manufacture certainty.
MAX_MATCH_SCORE: Final[float] = 0.96

#: How close the runner-up may get before the match is treated as ambiguous.
TIE_MARGIN: Final[float] = 0.06

#: Confidence an ambiguous match is capped to. Below every sane
#: ``status_sync_min_confidence``, so an ambiguous match always reaches a human.
AMBIGUOUS_CEILING: Final[float] = 0.50

#: Shortest employer-issued identifier that may be searched for in a body. A four-character
#: confirmation id would hit inside URLs, dates and tracking pixels.
MIN_EXTERNAL_ID_CHARS: Final[int] = 6

#: Shortest normalised company name that may be matched by containment. Two-letter names
#: ("HP") would match inside ordinary words and are left to the domain and title signals.
MIN_COMPANY_TOKEN_CHARS: Final[int] = 3

#: Tolerance for clock skew between the submission timestamp and the provider's ``Date:``
#: header. An acknowledgement genuinely can be stamped a moment *before* the local clock
#: recorded the submission.
CLOCK_GRACE_MINUTES: Final[int] = 60

#: Ceiling on candidate applications examined for one signal. A user with more submitted
#: applications than this in the lookback window has the most recent ones considered, which
#: is where an incoming message is overwhelmingly likely to belong.
MAX_CANDIDATES: Final[int] = 500

#: ATS relay domain → the provider that owns it. Lets a Greenhouse-relayed message prefer a
#: Greenhouse-sourced application over an otherwise identical Lever one.
RELAY_TO_PROVIDER: Final[dict[str, ATSProviderName]] = {
    "greenhouse.io": ATSProviderName.GREENHOUSE,
    "greenhouse-mail.io": ATSProviderName.GREENHOUSE,
    "lever.co": ATSProviderName.LEVER,
    "hire.lever.co": ATSProviderName.LEVER,
    "ashbyhq.com": ATSProviderName.ASHBY,
    "myworkday.com": ATSProviderName.WORKDAY,
    "myworkdayjobs.com": ATSProviderName.WORKDAY,
    "linkedin.com": ATSProviderName.LINKEDIN,
}

#: Signals that mean "this message named the employer", for the pairing bonus.
_COMPANY_SIGNALS: Final[frozenset[str]] = frozenset(
    {"company_hint", "company_subject", "company_body"}
)

#: Signals that mean "this message named the role", for the pairing bonus. The partial
#: variants are excluded from both sets: a token-overlap guess is not a naming.
_TITLE_SIGNALS: Final[frozenset[str]] = frozenset({"title_subject", "title_body"})

#: Everything that is not a letter or a digit, collapsed before containment testing so that
#: "Acme, Inc." in the body still contains the normalised key "acme".
_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def _normalize_corpus(text: str) -> str:
    """Reduce free text to space-delimited lowercase alphanumerics.

    The same shape :func:`~app.jobs.dedupe.normalize_company` produces, so a containment
    test between the two is meaningful. Padded with a leading and trailing space by the
    caller so that ``" acme "`` cannot match inside ``"acmecorp"``.

    Args:
        text: Subject, body, or a display name.

    Returns:
        The normalised text, or ``""``.
    """
    if not text:
        return ""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def _contains(haystack: str, needle: str) -> bool:
    """Return whether *needle* appears in *haystack* as whole tokens.

    Args:
        haystack: Already-normalised corpus text.
        needle: Already-normalised phrase.

    Returns:
        ``True`` when the phrase occurs on token boundaries.
    """
    if not haystack or not needle:
        return False
    return f" {needle} " in f" {haystack} "


def _token_overlap(needle: str, haystack: str) -> float:
    """Return the fraction of *needle*'s tokens present in *haystack*.

    Args:
        needle: Normalised phrase whose tokens are being looked for.
        haystack: Normalised corpus.

    Returns:
        ``0.0``–``1.0``. ``0.0`` when either side is empty.
    """
    tokens = [token for token in needle.split() if token]
    if not tokens or not haystack:
        return 0.0
    present = set(haystack.split())
    return sum(1 for token in tokens if token in present) / len(tokens)


def _domain_matches(candidate: str, domain: str) -> bool:
    """Return whether *candidate* is *domain* or a subdomain of it.

    Args:
        candidate: A sender or reply-to domain, already lowercased.
        domain: The company's registered domain.

    Returns:
        ``True`` on an exact or subdomain match. Never the reverse: ``acme.com`` is not
        covered by ``careers.acme.com``.
    """
    if not candidate or not domain:
        return False
    normalized = domain.strip().lower().lstrip("@").rstrip(".")
    if not normalized:
        return False
    return candidate == normalized or candidate.endswith(f".{normalized}")


def _relay_of(sender_domain: str) -> str | None:
    """Return the ATS relay apex *sender_domain* belongs to, if any.

    Args:
        sender_domain: The message's sender domain.

    Returns:
        The relay's apex domain (``"greenhouse.io"`` for ``"us-east.greenhouse.io"``), or
        ``None`` when the sender is not a known relay.
    """
    if not sender_domain:
        return None
    for relay in ATS_RELAY_DOMAINS:
        if sender_domain == relay or sender_domain.endswith(f".{relay}"):
            return relay
    return None


@dataclass(slots=True)
class _Candidate:
    """One scored application, with the reasons it scored that way.

    Attributes:
        application_id: The application being scored.
        score: Accumulated evidence weight.
        signals: Names of the :data:`WEIGHTS` entries that fired, in the order they fired.
        detail: Extra facts worth persisting on ``status_signals.match_evidence`` — the
            company that matched, the title, the gap in hours.
        decisive: Whether an employer-issued identifier settled it outright.
    """

    application_id: uuid.UUID
    score: float = 0.0
    signals: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    decisive: bool = False

    def award(self, name: str, weight: float | None = None) -> None:
        """Add one named piece of evidence.

        Args:
            name: Key in :data:`WEIGHTS`, recorded on :attr:`signals`.
            weight: Override the tabled weight, for the continuously-scaled recency signal.
        """
        self.score += WEIGHTS[name] if weight is None else weight
        self.signals.append(name)


class SignalMatcher:
    """Binds a :class:`~app.tracking.base.StatusSignal` to one of the user's applications.

    Args:
        session: The unit of work used to read candidate applications. Nothing here writes.
        settings: Application settings, for the lookback window. Resolved from the process
            singleton when ``None``.

    Usage::

        matcher = SignalMatcher(session, settings)
        match = await matcher.match(user.id, signal, classification)
        if match.is_confident(settings.status_sync_min_confidence):
            ...
    """

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        """Bind the matcher to one session and one settings object."""
        self._session = session
        self._settings = settings
        self.logger = logger

    # ----------------------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------------------

    async def match(
        self,
        user_id: uuid.UUID,
        sig: StatusSignal,
        cls: Classification,
    ) -> Match:
        """Score every plausible application for *user_id* and return the best.

        Args:
            user_id: Owner of the mailbox the signal came from.
            sig: The message.
            cls: What the classifier made of it. Recorded in the evidence so a stored match
                explains both halves of the decision, and used to bound the candidate set:
                an acknowledgement can only be about a very recently sent application.

        Returns:
            The best :class:`~app.tracking.base.Match`. ``application_id`` is ``None`` when
            nothing scored above :data:`MIN_MATCH_SCORE` — a rejection from a company the
            user never applied to through ApplicantOS is simply not ours, and saying so is
            the correct answer, not a failure.
        """
        candidates = await self._candidates(user_id, sig)
        if not candidates:
            return Match(
                application_id=None,
                confidence=0.0,
                evidence={
                    "matched": False,
                    "reason": "no application was submitted before this message arrived",
                    "kind": cls.kind.value,
                    "sender_domain": sig.sender_domain,
                    "candidates_considered": 0,
                },
            )

        corpus = _Corpus(sig)
        scored = [self._score(sig, application, corpus) for application in candidates]
        scored.sort(key=lambda item: item.score, reverse=True)

        best = scored[0]
        runner_up = scored[1].score if len(scored) > 1 else 0.0
        ambiguous = (
            not best.decisive
            and len(scored) > 1
            and (best.score - runner_up) < TIE_MARGIN
            and runner_up >= MIN_MATCH_SCORE
        )

        confidence = min(best.score, MAX_MATCH_SCORE) if not best.decisive else _DECISIVE
        if ambiguous:
            confidence = min(confidence, AMBIGUOUS_CEILING)

        evidence: dict[str, Any] = {
            "matched": best.score >= MIN_MATCH_SCORE,
            "kind": cls.kind.value,
            "score": round(best.score, 4),
            "signals": list(best.signals),
            "sender_domain": sig.sender_domain,
            "from_relay": sig.is_from_relay,
            "ambiguous": ambiguous,
            "runner_up_score": round(runner_up, 4),
            "candidates_considered": len(scored),
            **best.detail,
        }

        if best.score < MIN_MATCH_SCORE:
            evidence["reason"] = "no candidate application scored above the matching floor"
            return Match(application_id=None, confidence=round(best.score, 4), evidence=evidence)

        self.logger.debug(
            "tracking.matched",
            application_id=str(best.application_id),
            score=round(best.score, 4),
            ambiguous=ambiguous,
            **sig.log_context(),
        )
        return Match(
            application_id=best.application_id,
            confidence=round(confidence, 4),
            evidence=evidence,
        )

    # ----------------------------------------------------------------------------------
    # Candidates
    # ----------------------------------------------------------------------------------

    async def _candidates(self, user_id: uuid.UUID, sig: StatusSignal) -> list[Application]:
        """Load the applications a message could possibly be about.

        Two bounds, both from §17.5.5. The application must have been **submitted before**
        the message arrived — an employer cannot write about an application they do not have
        — and it must be inside the configured lookback window, because a rejection for a
        job applied to two years ago is not what this sync is for.

        Args:
            user_id: The applicant.
            sig: The message, whose ``received_at`` is the upper bound.

        Returns:
            Up to :data:`MAX_CANDIDATES` applications, most recently submitted first.
        """
        lookback = int(getattr(self._resolved_settings(), "status_sync_lookback_days", 120) or 120)
        ceiling = sig.received_at + timedelta(minutes=CLOCK_GRACE_MINUTES)
        floor = sig.received_at - timedelta(days=max(1, lookback))

        rows = await self._session.execute(
            select(Application)
            .where(
                Application.user_id == user_id,
                Application.submitted_at.is_not(None),
                Application.submitted_at <= ceiling,
                Application.submitted_at >= floor,
            )
            .order_by(Application.submitted_at.desc())
            .limit(MAX_CANDIDATES)
        )
        return list(rows.scalars().all())

    def _resolved_settings(self) -> Any:
        """Return the bound settings, or the process singleton.

        Returns:
            A settings-like object. Imported lazily so constructing a matcher in a unit test
            does not read the environment.
        """
        if self._settings is not None:
            return self._settings
        from app.config.settings import get_settings

        return get_settings()

    # ----------------------------------------------------------------------------------
    # Scoring
    # ----------------------------------------------------------------------------------

    def _score(
        self,
        sig: StatusSignal,
        application: Application,
        corpus: _Corpus,
    ) -> _Candidate:
        """Score one application against one message.

        The order of the tests is the order of §17.5, and the first of them can end the
        scoring outright.

        Args:
            sig: The message.
            application: The candidate.
            corpus: Pre-normalised views of the message, shared across candidates.

        Returns:
            The scored candidate, with every contributing signal named.
        """
        candidate = _Candidate(application_id=application.id)
        company: Company | None = application.company
        posting: JobPosting | None = application.posting

        # -- 6. employer-issued identifiers: decisive when present ----------------------
        for label, value in (
            ("external_application_id", application.external_application_id),
            ("confirmation_id", application.confirmation_id),
        ):
            token = (value or "").strip().lower()
            if len(token) >= MIN_EXTERNAL_ID_CHARS and token in corpus.raw:
                candidate.decisive = True
                candidate.score = _DECISIVE
                candidate.signals.append(label)
                candidate.detail[label] = token
                return candidate

        # -- 1./2. sender domain, unless the sender is an ATS relay ---------------------
        relay = _relay_of(sig.sender_domain)
        domain = (company.domain or "") if company is not None else ""
        if relay is None and _domain_matches(sig.sender_domain, domain):
            candidate.award("sender_domain")
            candidate.detail["company_domain"] = domain
        reply_to = str(sig.raw.get("reply_to_domain") or "").strip().lower()
        if reply_to and _domain_matches(reply_to, domain):
            candidate.award("reply_to_domain")
            candidate.detail["reply_to_domain"] = reply_to
        if relay is not None and posting is not None:
            expected = RELAY_TO_PROVIDER.get(relay)
            if expected is not None and posting.provider == expected:
                candidate.award("relay_provider")
                candidate.detail["relay"] = relay

        # -- 3. company name, fuzzy, via normalize_company -------------------------------
        self._score_company(candidate, company, corpus)

        # -- 4. posting title in the subject or the body ---------------------------------
        self._score_title(candidate, posting, corpus)

        # Naming the employer and the role in one message is qualitatively different from
        # doing either alone: it is how an ATS addresses one application rather than one
        # candidate, so it is worth more than the two contributions separately.
        named = set(candidate.signals)
        if named & _COMPANY_SIGNALS and named & _TITLE_SIGNALS:
            candidate.award("company_title_pair")

        # -- 5. recency inside the permitted window --------------------------------------
        self._score_recency(candidate, sig, application)

        return candidate

    def _score_company(
        self,
        candidate: _Candidate,
        company: Company | None,
        corpus: _Corpus,
    ) -> None:
        """Award the company-name evidence for one candidate.

        Deliberately inverted: rather than extracting an employer from the message, each
        candidate's own employer name is looked for *in* the message. That cannot invent a
        company the user never applied to, and it needs no entity recogniser.

        Args:
            candidate: The candidate being scored, mutated in place.
            company: The application's employer, when the row has one.
            corpus: Pre-normalised views of the message.
        """
        if company is None:
            return
        keys = {
            normalize_company(company.name),
            (company.normalized_name or "").strip().lower(),
        }
        domain_label = (company.domain or "").split(".")[0].strip().lower()
        if len(domain_label) >= MIN_COMPANY_TOKEN_CHARS:
            keys.add(domain_label)

        usable = [key for key in keys if len(key) >= MIN_COMPANY_TOKEN_CHARS]
        if not usable:
            return

        for key in usable:
            if _contains(corpus.hint, key):
                candidate.award("company_hint")
                candidate.detail["company"] = key
                return
        for key in usable:
            if _contains(corpus.subject, key):
                candidate.award("company_subject")
                candidate.detail["company"] = key
                return
        for key in usable:
            if _contains(corpus.body, key):
                candidate.award("company_body")
                candidate.detail["company"] = key
                return

        best = max(_token_overlap(key, corpus.all) for key in usable)
        if best >= 0.6:
            candidate.award("company_partial")
            candidate.detail["company_overlap"] = round(best, 3)

    def _score_title(
        self,
        candidate: _Candidate,
        posting: JobPosting | None,
        corpus: _Corpus,
    ) -> None:
        """Award the posting-title evidence for one candidate.

        Args:
            candidate: The candidate being scored, mutated in place.
            posting: The posting applied to.
            corpus: Pre-normalised views of the message.
        """
        if posting is None:
            return
        title = normalize_title(posting.title)
        if len(title) < MIN_COMPANY_TOKEN_CHARS:
            return

        if _contains(corpus.subject, title):
            candidate.award("title_subject")
            candidate.detail["title"] = title
            return
        if _contains(corpus.body, title):
            candidate.award("title_body")
            candidate.detail["title"] = title
            return

        overlap = _token_overlap(title, corpus.all)
        if overlap >= 0.75 and len(title.split()) >= 2:
            candidate.award("title_partial")
            candidate.detail["title_overlap"] = round(overlap, 3)

    def _score_recency(
        self,
        candidate: _Candidate,
        sig: StatusSignal,
        application: Application,
    ) -> None:
        """Award a continuous bonus for a message that arrived soon after submission.

        Job mail clusters immediately after a submission (the acknowledgement) and then
        again weeks later (the outcome), so this is a tiebreaker rather than a primary
        signal — which is why its weight is the smallest in :data:`WEIGHTS`.

        Args:
            candidate: The candidate being scored, mutated in place.
            sig: The message.
            application: The candidate application.
        """
        submitted = application.submitted_at
        if submitted is None:
            return
        moment = submitted if submitted.tzinfo is not None else submitted.replace(tzinfo=UTC)
        gap_days = max(0.0, (sig.received_at - moment).total_seconds() / 86_400.0)
        lookback = float(
            getattr(self._resolved_settings(), "status_sync_lookback_days", 120) or 120
        )
        decay = max(0.0, 1.0 - (gap_days / max(1.0, lookback)))
        candidate.award("recency", WEIGHTS["recency"] * decay)
        candidate.detail["gap_days"] = round(gap_days, 2)


class _Corpus:
    """Pre-normalised views of one message, computed once and reused per candidate.

    Normalising a 20,000-character body inside a loop over 500 candidates would be the only
    expensive thing in this module; doing it once makes matching linear in candidates with a
    tiny constant.

    Attributes:
        raw: The subject and body lowercased but otherwise untouched, for identifier
            containment — an ATS reference such as ``"4821-ax"`` must survive normalisation.
        subject: Normalised subject.
        body: Normalised body.
        hint: Normalised display name / reply-to name, where a relay puts the employer.
        all: Every view joined, for token-overlap scoring.
    """

    __slots__ = ("all", "body", "hint", "raw", "subject")

    def __init__(self, sig: StatusSignal) -> None:
        """Build every normalised view of *sig* in one pass.

        Args:
            sig: The message to normalise.
        """
        hint_source = " ".join(
            part
            for part in (
                sig.company_hint or "",
                str(sig.raw.get("sender_name") or ""),
                str(sig.raw.get("reply_to_name") or ""),
            )
            if part
        )
        self.raw = f"{sig.subject}\n{sig.body}".lower()
        self.subject = _normalize_corpus(sig.subject)
        self.body = _normalize_corpus(sig.body)
        self.hint = _normalize_corpus(hint_source)
        self.all = " ".join(part for part in (self.hint, self.subject, self.body) if part)
