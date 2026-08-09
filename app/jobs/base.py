"""The ATS provider extension point (``docs/CONTRACTS.md`` §9).

This module is the promise ApplicantOS makes about extensibility: **adding an applicant
tracking system requires zero changes to the core.** A new provider is one module in this
package that subclasses :class:`ATSProvider`, declares a
:class:`~app.plugins.base.PluginMeta`, and is decorated with
:func:`~app.plugins.registry.plugin`. Nothing else in the codebase learns its name — the
pipeline, the workers, the API and the desktop app all reach it through
:mod:`app.jobs.registry`, which reaches it through :mod:`app.plugins.registry` (golden rule
#5).

What a provider gets for free, and must therefore never reimplement:

**Typed transport.** :meth:`ATSProvider._request` is the only way a provider should touch the
network. It maps status codes onto this module's exception hierarchy — 429 to
:class:`ProviderRateLimitError` carrying the server's ``Retry-After``, 401/403 to
:class:`ProviderAuthError`, 404/410 to :class:`PostingUnavailableError`, 5xx to a transient
:class:`ProviderError` — and retries transient failures with exponential backoff and jitter.
Because every provider goes through it, the pipeline's error handling is uniform and a new
ATS inherits correct rate-limit behaviour on its first day.

**Pagination.** :meth:`ATSProvider.paginate` drives both offset- and cursor-paginated feeds
from a single ``fetch`` callback, enforcing the caller's result limit and a page ceiling so
that a feed which never reports exhaustion cannot spin forever.

**A shared HTTP client.** :attr:`ATSProvider._http` builds one ``httpx.AsyncClient`` per
provider instance, lazily, with a descriptive User-Agent and connection pooling. Providers
are registry singletons, so this is one pool per ATS for the process lifetime.

**Database-free data.** :class:`JobPostingDTO` and :class:`UserProfileDTO` are plain
dataclasses built from an ORM row by duck typing. ``app/jobs/`` therefore imports nothing
from ``app.models`` except :mod:`app.models.enums`, which means a provider can be unit-tested
with no database, no session, and no event loop bound to a greenlet.

**One place for the safety guards.** Providers that can really submit delegate to
:func:`app.jobs._apply.run_browser_apply` rather than driving Playwright themselves, so the
kill switch, the dry-run check and the review-escalation rules are written once
(``docs/CONTRACTS.md`` §12, golden rules #2 and #3).

Auto-apply posture is binding: Greenhouse, Lever and Ashby submit for real; Workday and
LinkedIn raise :class:`UnsupportedFlowError` and route to manual review. Each provider module
states its posture in its own docstring (golden rule #10).
"""

from __future__ import annotations

import abc
import asyncio
import random
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, TypeVar

import structlog

from app.jobs._parsing import clean_text, parse_date
from app.models.enums import (
    ApplicationStatus,
    ATSProviderName,
    EmploymentType,
    FieldKind,
    PostingStatus,
    ReviewReason,
    WorkArrangement,
    WorkAuthStatus,
)
from app.plugins.base import BasePlugin, PluginLoadError, PluginMeta

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    import httpx

    from app.config.settings import Settings

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_PAGES",
    "USER_AGENT",
    "ATSProvider",
    "ApplyContext",
    "ApplyResult",
    "FormField",
    "JobPostingDTO",
    "PostingUnavailableError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderRateLimitError",
    "RawPosting",
    "SearchQuery",
    "UnsupportedFlowError",
    "UserProfileDTO",
]

logger = structlog.get_logger(__name__)

#: Item type yielded by :meth:`ATSProvider.paginate`.
T = TypeVar("T")


# ======================================================================================
# Transport constants
# ======================================================================================

#: Sent on every outbound request. Honest and identifiable on purpose: an operator reading
#: their access log should be able to tell what this traffic is and why, and a provider who
#: wants to talk to us should be able to. Golden rule #10 is about the same posture.
USER_AGENT: Final[str] = "ApplicantOS/0.1 (+job-discovery)"

#: Fallback request timeout, used when settings expose no network timeout of their own.
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Lower and upper bounds applied to a timeout read from settings, so a mis-set value can
#: neither hang a discovery run nor abort every request before the first byte arrives.
MIN_HTTP_TIMEOUT_SECONDS: Final[float] = 1.0
MAX_HTTP_TIMEOUT_SECONDS: Final[float] = 300.0

#: Settings fields consulted for the HTTP timeout, in order of preference. ``§1`` of the
#: contract defines no provider-specific network timeout; ``playwright_timeout_ms`` is the
#: application's one "how long may a remote operation take" knob, so it is honoured here and
#: a dedicated field is picked up automatically if the contract ever grows one.
_TIMEOUT_SETTING_FIELDS: Final[tuple[tuple[str, float], ...]] = (
    ("http_timeout_seconds", 1.0),
    ("playwright_timeout_ms", 0.001),
)

#: Attempts per request, including the first. Three is the same budget the LLM layer uses.
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

#: First backoff delay; doubled per attempt.
RETRY_BASE_DELAY_SECONDS: Final[float] = 0.5

#: Ceiling on the computed exponential delay, before jitter.
RETRY_MAX_DELAY_SECONDS: Final[float] = 30.0

#: Jitter added to each delay, as a fraction of the delay itself. Decorrelates a fleet of
#: workers that were all rate-limited by the same board at the same instant.
RETRY_JITTER_RATIO: Final[float] = 0.25

#: Ceiling on an honoured ``Retry-After``. A board asking for ten minutes must not stall a
#: discovery run for ten minutes; the attempt budget runs out instead and the provider is
#: skipped for this cycle.
MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0

#: The header a server uses to request a cooldown.
RETRY_AFTER_HEADER: Final[str] = "Retry-After"

#: Status codes that mean "you are going too fast".
RATE_LIMIT_STATUS_CODES: Final[frozenset[int]] = frozenset({429})

#: Status codes that mean "we do not know who you are, or you may not do that".
AUTH_STATUS_CODES: Final[frozenset[int]] = frozenset({401, 403})

#: Status codes that mean "this posting is not here". 410 is the honest one; 404 is what most
#: boards return for a filled or withdrawn role.
UNAVAILABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({404, 410})

#: Non-5xx status codes worth retrying: request timeout, conflict, too-early.
TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 409, 425})

#: First status code of the server-error band.
SERVER_ERROR_STATUS: Final[int] = 500

#: First status code of the client-error band.
CLIENT_ERROR_STATUS: Final[int] = 400

#: Maximum simultaneous connections a single provider may open.
MAX_CONNECTIONS: Final[int] = 10

#: Maximum idle keep-alive connections a single provider retains.
MAX_KEEPALIVE_CONNECTIONS: Final[int] = 5

#: Postings requested per provider when a :class:`SearchQuery` does not say otherwise.
#: Mirrors ``app.schemas.posting.DEFAULT_DISCOVER_LIMIT``; kept as its own constant so that
#: ``app/jobs`` stays independent of the API schema layer.
DEFAULT_SEARCH_LIMIT: Final[int] = 100

#: Items requested per page by :meth:`ATSProvider.paginate` when the caller does not choose.
DEFAULT_PAGE_SIZE: Final[int] = 100

#: Hard ceiling on pages fetched in one :meth:`ATSProvider.paginate` call. A feed that never
#: reports exhaustion — because it echoes the last page forever, or because a cursor loops —
#: must not spin indefinitely.
MAX_PAGES: Final[int] = 200

#: Jitter source. Deliberately independent of the global :mod:`random` state, so backoff
#: never perturbs (or is perturbed by) a seeded generator in a test.
_JITTER_RNG: Final[random.SystemRandom] = random.SystemRandom()


# ======================================================================================
# Errors
# ======================================================================================


class ProviderError(Exception):
    """Base class for every failure originating in an ATS provider.

    Carries enough context for the pipeline to decide what to do without re-inspecting the
    HTTP response: which provider failed, what the server said, and whether trying again
    could plausibly work.

    Attributes:
        provider: The provider that raised, when known.
        status_code: The HTTP status behind the failure, when there was one.
        url: The URL that failed, when there was one.
        transient: Whether an identical retry could succeed.
        retry_after: Seconds the server asked the caller to wait, when it said so.
    """

    #: Whether instances of this class are retryable unless told otherwise.
    DEFAULT_TRANSIENT: ClassVar[bool] = False

    #: The review queue reason this failure maps to, or ``None`` when it does not warrant
    #: human attention on its own.
    REVIEW_REASON: ClassVar[ReviewReason | None] = None

    def __init__(
        self,
        message: str,
        *,
        provider: ATSProviderName | str | None = None,
        status_code: int | None = None,
        url: str | None = None,
        transient: bool | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Record the failure together with everything needed to decide about a retry.

        Args:
            message: Human-readable explanation, safe to log and to show an operator.
            provider: The provider involved, if known.
            status_code: The HTTP status behind the failure, if any.
            url: The URL that failed, if any.
            transient: Whether a retry could succeed; defaults to :attr:`DEFAULT_TRANSIENT`.
            retry_after: Server-requested cooldown in seconds, if any.
        """
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.url = url
        self.transient = self.DEFAULT_TRANSIENT if transient is None else transient
        self.retry_after = retry_after

    @property
    def review_reason(self) -> ReviewReason | None:
        """The review-queue reason this failure maps to, if any."""
        return self.REVIEW_REASON

    def __repr__(self) -> str:
        """Return a debugger-friendly summary including provider and status."""
        return (
            f"{type(self).__name__}({str(self)!r}, provider={self.provider!r}, "
            f"status_code={self.status_code!r}, transient={self.transient!r})"
        )


class ProviderAuthError(ProviderError):
    """The provider rejected the request's credentials or refused the operation.

    Never transient: repeating an unauthenticated request produces the same 401 and burns
    the attempt budget. The pipeline routes affected applications to manual review with
    :attr:`~app.models.enums.ReviewReason.LOGIN_REQUIRED`, because a human signing in is the
    only thing that can resolve it.
    """

    DEFAULT_TRANSIENT: ClassVar[bool] = False
    REVIEW_REASON: ClassVar[ReviewReason | None] = ReviewReason.LOGIN_REQUIRED


class ProviderRateLimitError(ProviderError):
    """The provider asked the caller to slow down.

    Transient by definition. When the response carried a ``Retry-After``,
    :attr:`retry_after` holds it in seconds and :meth:`ATSProvider._request` waits that long
    (clamped to :data:`MAX_RETRY_AFTER_SECONDS`) instead of using its own backoff curve.

    Attributes:
        retry_after: Seconds the server asked the caller to wait, or ``None``.
    """

    DEFAULT_TRANSIENT: ClassVar[bool] = True
    REVIEW_REASON: ClassVar[ReviewReason | None] = ReviewReason.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        provider: ATSProviderName | str | None = None,
        status_code: int | None = None,
        url: str | None = None,
        transient: bool | None = None,
    ) -> None:
        """Record the rate limit and the cooldown the server requested.

        Args:
            message: Human-readable explanation.
            retry_after: Seconds to wait before retrying, as advertised by the server.
            provider: The provider involved, if known.
            status_code: The HTTP status behind the failure; normally 429.
            url: The URL that was throttled, if any.
            transient: Override for the retry decision; almost never needed.
        """
        super().__init__(
            message,
            provider=provider,
            status_code=status_code,
            url=url,
            transient=transient,
            retry_after=retry_after,
        )


class UnsupportedFlowError(ProviderError):
    """The provider cannot perform this operation, by design rather than by accident.

    Raised by :meth:`ATSProvider.apply` on every provider that does not support automated
    submission — Workday's account-gated multi-step flow, LinkedIn's terms of service — and
    by :func:`app.jobs._apply.run_browser_apply` when the browser layer is unavailable. It is
    a terminal, non-retryable outcome that routes to manual review (golden rule #10).
    """

    DEFAULT_TRANSIENT: ClassVar[bool] = False
    REVIEW_REASON: ClassVar[ReviewReason | None] = ReviewReason.UNSUPPORTED_FLOW


class PostingUnavailableError(ProviderError):
    """The posting is gone: filled, withdrawn, expired, or never existed.

    Not a failure of the system. The pipeline marks the posting
    :attr:`~app.models.enums.PostingStatus.EXPIRED` and moves on, which is why this does not
    map to a review reason.
    """

    DEFAULT_TRANSIENT: ClassVar[bool] = False
    REVIEW_REASON: ClassVar[ReviewReason | None] = None


# ======================================================================================
# Data transfer objects
# ======================================================================================


def _read(source: Any, name: str, default: Any = None) -> Any:
    """Read one attribute from an ORM row, a dataclass, or a mapping.

    Duck typing rather than an import is what keeps ``app/jobs`` free of ORM classes
    (``docs/CONTRACTS.md`` §9): a DTO can be built from a :class:`~app.models.posting.
    JobPosting`, from a stub in a unit test, or from a raw provider payload, with no
    inheritance and no session in sight.

    Args:
        source: The object to read from.
        name: The attribute or key name.
        default: Returned when the attribute is absent or ``None``.

    Returns:
        The value, or *default*.
    """
    if isinstance(source, Mapping):
        value = source.get(name, default)
    else:
        value = getattr(source, name, default)
    return default if value is None else value


def _read_loaded(source: Any, name: str, default: Any = None) -> Any:
    """Read a relationship attribute **without ever emitting SQL**.

    A SQLAlchemy relationship that was not eager-loaded triggers a lazy load on attribute
    access, which raises ``MissingGreenlet`` inside an async session and silently issues an
    extra query outside one. Instance state lives in ``__dict__`` once loaded, so consulting
    that first turns "not loaded" into ``None`` instead of into a query.

    Args:
        source: The object to read from.
        name: The relationship name.
        default: Returned when the relationship is absent or not loaded.

    Returns:
        The related object, or *default*.
    """
    if isinstance(source, Mapping):
        value = source.get(name, default)
        return default if value is None else value
    state = getattr(source, "__dict__", None)
    if isinstance(state, dict):
        if name in state:
            value = state[name]
            return default if value is None else value
        # Not loaded, and the object has instance state: refuse to trigger a lazy load.
        if hasattr(type(source), name) and hasattr(source, "_sa_instance_state"):
            return default
    return _read(source, name, default)


def _as_str(value: Any) -> str | None:
    """Return *value* as a trimmed string, or ``None`` when it carries no content."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    trimmed = text.strip()
    return trimmed or None


def _as_int(value: Any) -> int | None:
    """Return *value* as an ``int``, or ``None`` when it is not a whole number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_uuid(value: Any) -> uuid.UUID | None:
    """Return *value* as a :class:`~uuid.UUID`, or ``None`` when it is not one."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _as_enum(enum_class: Any, value: Any, default: Any) -> Any:
    """Coerce *value* to a member of *enum_class*, falling back to *default*.

    Args:
        enum_class: A :class:`~app.models.enums.StrEnum` subclass.
        value: The raw value, typically a string from a provider feed.
        default: Returned when *value* resolves to nothing.

    Returns:
        The resolved member, or *default*.
    """
    if value is None:
        return default
    return enum_class.coerce(value, default)


@dataclass(slots=True)
class RawPosting:
    """One job advertisement exactly as a provider found it.

    The single currency of discovery: every provider's ``search`` and ``fetch_posting``
    produce these, and :meth:`app.services.pipeline.Pipeline.ingest` is the only thing that
    turns one into a :class:`~app.models.posting.JobPosting` row. Field names deliberately
    mirror that table (``docs/CONTRACTS.md`` §4 and §9) so ingestion is a straight mapping
    with no translation layer to drift.

    ``__post_init__`` normalises so that providers do not each have to: enum-shaped strings
    become enum members, dates of every shape become timezone-aware UTC, identity strings are
    cleaned, and a reversed salary range is put back in order. Passing raw feed values
    straight in is therefore correct.

    Attributes:
        provider: Which ATS this was discovered through.
        external_id: The provider's own identifier. Unique together with ``provider`` and
            the strongest available dedupe signal (see :func:`app.jobs.dedupe.dedupe_key`).
        url: Canonical human-facing URL of the posting.
        title: Role title as advertised.
        company_name: Employer name as the provider spelled it; normalised during ingestion.
        description: Full description, already converted to plain text by the provider
            (usually via :func:`app.jobs._parsing.html_to_text`).
        location: Location string exactly as advertised.
        work_arrangement: Remote/hybrid/onsite. ``UNKNOWN`` is honest, never a guess.
        employment_type: Full-time/internship/contract/… as advertised.
        salary_min: Bottom of the advertised range, annualised.
        salary_max: Top of the advertised range, annualised.
        salary_currency: ISO 4217 code for the range.
        posted_at: Publication instant, timezone-aware UTC.
        closes_at: Advertised closing instant, timezone-aware UTC.
        apply_url: Direct URL of the application form when it differs from :attr:`url`.
        raw: The provider's untouched payload, stored so a parser bug can be fixed and
            re-run without re-crawling.
    """

    provider: ATSProviderName
    external_id: str
    url: str
    title: str
    company_name: str
    description: str | None = None
    location: str | None = None
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    posted_at: datetime | None = None
    closes_at: datetime | None = None
    apply_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise loosely-typed provider input into the shape the pipeline expects.

        Raises:
            ValueError: If :attr:`provider` is not a recognised
                :class:`~app.models.enums.ATSProviderName`. Every other field degrades to a
                documented default rather than raising, because a feed is untrusted input
                and one malformed salary must not lose the posting.
        """
        self.provider = ATSProviderName(self.provider)
        self.external_id = clean_text(self.external_id)
        self.url = (self.url or "").strip()
        self.title = clean_text(self.title)
        self.company_name = clean_text(self.company_name)
        self.location = _as_str(clean_text(self.location)) if self.location else None
        self.apply_url = _as_str(self.apply_url)
        self.description = self.description or None

        self.work_arrangement = _as_enum(
            WorkArrangement, self.work_arrangement, WorkArrangement.UNKNOWN
        )
        self.employment_type = _as_enum(
            EmploymentType, self.employment_type, EmploymentType.UNKNOWN
        )

        self.posted_at = parse_date(self.posted_at)
        self.closes_at = parse_date(self.closes_at)

        self.salary_min = _as_int(self.salary_min)
        self.salary_max = _as_int(self.salary_max)
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            self.salary_min, self.salary_max = self.salary_max, self.salary_min
        currency = _as_str(self.salary_currency)
        self.salary_currency = currency.upper()[:3] if currency else None

        if not isinstance(self.raw, dict):
            self.raw = {}

    @property
    def target_url(self) -> str:
        """The URL to open in order to apply: :attr:`apply_url` when set, else :attr:`url`."""
        return self.apply_url or self.url

    def __repr__(self) -> str:
        """Return a compact summary that never dumps the whole description."""
        return (
            f"RawPosting(provider={self.provider.value!r}, external_id={self.external_id!r}, "
            f"title={self.title!r}, company_name={self.company_name!r})"
        )


@dataclass(slots=True)
class SearchQuery:
    """What to look for in one discovery run against one provider.

    Built by :meth:`app.services.pipeline.Pipeline.discover` from the user's preferences and
    the ``POST /postings/discover`` body. Every field is advisory: providers whose API cannot
    filter on something apply the filter client-side or ignore it, and the scorer catches
    what slips through. A provider must never *widen* a query.

    Attributes:
        keywords: Search terms, OR-ed by convention.
        locations: Location filters, OR-ed by convention.
        remote_only: Restrict to roles advertised as remote.
        posted_within_days: Freshness window; ``None`` means no limit.
        limit: Maximum postings to yield. Providers stop early rather than over-fetching.
        extra: Provider-specific options passed through untouched — board tokens, company
            slugs, tenant names. See :func:`app.jobs.seeds.boards_from_query`.
    """

    keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_only: bool = False
    posted_within_days: int | None = None
    limit: int = DEFAULT_SEARCH_LIMIT
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise the collections so providers can iterate them without guarding."""
        self.keywords = [clean_text(item) for item in (self.keywords or []) if clean_text(item)]
        self.locations = [clean_text(item) for item in (self.locations or []) if clean_text(item)]
        self.remote_only = bool(self.remote_only)
        window = _as_int(self.posted_within_days)
        self.posted_within_days = window if window and window > 0 else None
        limit = _as_int(self.limit)
        self.limit = limit if limit and limit > 0 else DEFAULT_SEARCH_LIMIT
        if not isinstance(self.extra, dict):
            self.extra = {}

    @property
    def cutoff(self) -> datetime | None:
        """The oldest ``posted_at`` this query accepts, or ``None`` when unbounded.

        Returns:
            A timezone-aware UTC instant, computed from :attr:`posted_within_days`.
        """
        if self.posted_within_days is None:
            return None
        return datetime.now(UTC) - timedelta(days=self.posted_within_days)

    def matches_freshness(self, posted_at: datetime | None) -> bool:
        """Return whether a posting is recent enough for this query.

        Args:
            posted_at: The posting's publication instant, or ``None`` when the provider did
                not supply one.

        Returns:
            ``True`` when the query is unbounded, when the provider gave no date (absence of
            a date is not evidence of staleness), or when the date is inside the window.
        """
        cutoff = self.cutoff
        if cutoff is None or posted_at is None:
            return True
        return posted_at >= cutoff


@dataclass(slots=True)
class FormField:
    """One input discovered on an application form.

    Produced by :meth:`app.browser.autofill.AutoFiller.discover_fields` and consumed by
    :class:`app.ai.field_answer.FieldAnswerer`. A field this system cannot answer with
    confidence is returned in :attr:`ApplyResult.unanswered_fields` and the application goes
    to a human — it is never filled speculatively (golden rule #2).

    Attributes:
        selector: CSS or accessibility selector that uniquely locates the control.
        label: The visible label, used both to answer the field and to explain it to a human.
        kind: How the control behaves, which decides the interaction strategy.
        required: Whether the form refuses to submit without it. A required field with no
            confident answer is an automatic escalation.
        options: Allowed values for select, multiselect, radio and checkbox groups.
        max_length: Character limit the form enforces, when it advertises one.
        hint: Helper or placeholder text shown beside the control.
    """

    selector: str
    label: str
    kind: FieldKind = FieldKind.UNKNOWN
    required: bool = False
    options: list[str] = field(default_factory=list)
    max_length: int | None = None
    hint: str | None = None

    def __post_init__(self) -> None:
        """Normalise the label and coerce a loosely-typed ``kind``."""
        self.label = clean_text(self.label)
        self.kind = _as_enum(FieldKind, self.kind, FieldKind.UNKNOWN)
        self.required = bool(self.required)
        self.options = [clean_text(option) for option in (self.options or [])]
        self.max_length = _as_int(self.max_length)
        self.hint = _as_str(self.hint)

    @property
    def is_essay(self) -> bool:
        """Whether this field is a free-text essay question.

        Essays are counted against ``settings.max_essay_questions_before_review``: past that
        many, the application goes to a human rather than being auto-written.
        """
        return self.kind is FieldKind.TEXTAREA


@dataclass(frozen=True, slots=True)
class JobPostingDTO:
    """A posting, detached from the database.

    Handed to providers, the browser layer, the resume engine and the cover-letter writer so
    that none of them holds a live ORM instance or a session. Immutable, because every
    consumer is a reader.

    Build one with :meth:`from_model` from a :class:`~app.models.posting.JobPosting`, or with
    :meth:`from_raw` from a :class:`RawPosting` that has not been persisted yet.

    Attributes:
        id: Database identity, or ``None`` for a posting that has not been ingested.
        provider: Which ATS this came from.
        external_id: The provider's own identifier.
        url: Canonical human-facing URL.
        title: Role title as advertised.
        company_name: Employer name, or ``None`` when company resolution has not run.
        apply_url: Direct application URL when it differs from :attr:`url`.
        description: Full advertised text, already plain.
        location: Location string as advertised.
        work_arrangement: Remote/hybrid/onsite.
        employment_type: Full-time/internship/contract/…
        salary_min: Bottom of the advertised annual range.
        salary_max: Top of the advertised annual range.
        salary_currency: ISO 4217 code for the range.
        posted_at: Publication instant, UTC.
        closes_at: Advertised closing instant, UTC.
        status: Pipeline lifecycle state.
        company_domain: Employer web domain, when enrichment has run.
        company_industry: Free-text industry label, when enrichment has run.
        company_is_defense: Whether the employer is a defence contractor.
        company_is_startup: Whether the employer is early-stage.
        company_employee_count: Best known headcount.
        content_hash: Hash of the advertised text at ingestion.
        dedupe_key: Cross-provider identity key.
    """

    provider: ATSProviderName
    external_id: str
    url: str
    title: str
    id: uuid.UUID | None = None
    company_name: str | None = None
    apply_url: str | None = None
    description: str | None = None
    location: str | None = None
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    posted_at: datetime | None = None
    closes_at: datetime | None = None
    status: PostingStatus = PostingStatus.DISCOVERED
    company_domain: str | None = None
    company_industry: str | None = None
    company_is_defense: bool = False
    company_is_startup: bool = False
    company_employee_count: int | None = None
    content_hash: str | None = None
    dedupe_key: str | None = None

    def __post_init__(self) -> None:
        """Coerce loosely-typed enum fields, mirroring :meth:`RawPosting.__post_init__`.

        A DTO is not always built by :meth:`from_model` or :meth:`from_raw`. Because
        :class:`~app.models.enums.ATSProviderName` and its siblings are string enums, a DTO
        that has been round-tripped through JSON — a checkpoint written before a crash, a
        Celery task argument, an API payload — comes back with plain ``str`` in these fields.
        Without this, the first attribute access that expects an enum raises a bare
        ``AttributeError`` on the *resume* path, and because it is not a
        :class:`ProviderError` the pipeline cannot classify it as terminal, so the retry
        loops instead of landing in the review queue (golden rule #8).

        The class is frozen, so assignment goes through ``object.__setattr__``.

        Raises:
            ValueError: If :attr:`provider` is not a recognised ``ATSProviderName``. That one
                is fatal because every downstream routing decision keys off it; the remaining
                enums degrade to their documented defaults instead.
        """
        setattr_ = object.__setattr__
        setattr_(self, "provider", ATSProviderName(self.provider))
        setattr_(
            self,
            "work_arrangement",
            _as_enum(WorkArrangement, self.work_arrangement, WorkArrangement.UNKNOWN),
        )
        setattr_(
            self,
            "employment_type",
            _as_enum(EmploymentType, self.employment_type, EmploymentType.UNKNOWN),
        )
        setattr_(self, "status", _as_enum(PostingStatus, self.status, PostingStatus.DISCOVERED))
        if self.id is not None and not isinstance(self.id, uuid.UUID):
            try:
                setattr_(self, "id", uuid.UUID(str(self.id)))
            except (ValueError, AttributeError, TypeError):
                setattr_(self, "id", None)

    @property
    def target_url(self) -> str:
        """The URL to open in order to apply."""
        return self.apply_url or self.url

    @property
    def salary_range(self) -> tuple[int | None, int | None]:
        """The advertised range as a ``(min, max)`` pair; neither end is ever inferred."""
        return (self.salary_min, self.salary_max)

    @classmethod
    def from_model(cls, posting: Any) -> JobPostingDTO:
        """Build a DTO from a :class:`~app.models.posting.JobPosting` by duck typing.

        Nothing is imported from ``app.models`` other than the enums, and no relationship is
        traversed in a way that could emit SQL: the employer is read from already-loaded
        instance state, so a posting whose ``company`` was not eager-loaded yields a DTO with
        ``company_name=None`` rather than a ``MissingGreenlet``.

        Args:
            posting: A ``JobPosting`` row, or any object (including a mapping) exposing the
                same attribute names.

        Returns:
            The detached snapshot.

        Raises:
            ValueError: If ``provider`` is missing or is not a recognised
                :class:`~app.models.enums.ATSProviderName`.
        """
        company = _read_loaded(posting, "company")
        return cls(
            id=_as_uuid(_read(posting, "id")),
            provider=ATSProviderName(_read(posting, "provider")),
            external_id=_as_str(_read(posting, "external_id")) or "",
            url=_as_str(_read(posting, "url")) or "",
            title=clean_text(_read(posting, "title")),
            company_name=_as_str(_read(company, "name")) if company is not None else None,
            apply_url=_as_str(_read(posting, "apply_url")),
            description=_read(posting, "description"),
            location=_as_str(_read(posting, "location")),
            work_arrangement=_as_enum(
                WorkArrangement, _read(posting, "work_arrangement"), WorkArrangement.UNKNOWN
            ),
            employment_type=_as_enum(
                EmploymentType, _read(posting, "employment_type"), EmploymentType.UNKNOWN
            ),
            salary_min=_as_int(_read(posting, "salary_min")),
            salary_max=_as_int(_read(posting, "salary_max")),
            salary_currency=_as_str(_read(posting, "salary_currency")),
            posted_at=parse_date(_read(posting, "posted_at")),
            closes_at=parse_date(_read(posting, "closes_at")),
            status=_as_enum(PostingStatus, _read(posting, "status"), PostingStatus.DISCOVERED),
            company_domain=_as_str(_read(company, "domain")) if company is not None else None,
            company_industry=_as_str(_read(company, "industry")) if company is not None else None,
            company_is_defense=bool(_read(company, "is_defense", False)),
            company_is_startup=bool(_read(company, "is_startup", False)),
            company_employee_count=_as_int(_read(company, "employee_count")),
            content_hash=_as_str(_read(posting, "content_hash")),
            dedupe_key=_as_str(_read(posting, "dedupe_key")),
        )

    @classmethod
    def from_raw(cls, raw: RawPosting) -> JobPostingDTO:
        """Build a DTO from a freshly discovered :class:`RawPosting`.

        Used when a posting must be scored, rendered or previewed before it has been
        persisted — for example by ``POST /resumes/preview``.

        Args:
            raw: The discovered posting.

        Returns:
            The detached snapshot, with ``id`` unset.
        """
        return cls(
            provider=raw.provider,
            external_id=raw.external_id,
            url=raw.url,
            title=raw.title,
            company_name=raw.company_name or None,
            apply_url=raw.apply_url,
            description=raw.description,
            location=raw.location,
            work_arrangement=raw.work_arrangement,
            employment_type=raw.employment_type,
            salary_min=raw.salary_min,
            salary_max=raw.salary_max,
            salary_currency=raw.salary_currency,
            posted_at=raw.posted_at,
            closes_at=raw.closes_at,
        )


@dataclass(frozen=True, slots=True)
class UserProfileDTO:
    """The user's answer sheet, detached from the database.

    Mirrors :meth:`app.models.profile.UserProfile.to_dto` field for field, so
    ``UserProfileDTO.from_model(profile)`` and ``UserProfileDTO(**profile.to_dto())`` agree.
    The browser autofiller resolves each discovered :class:`FormField` against this before
    involving the LLM, which is what makes most of an application fill deterministically.

    EEO fields are nullable and never inferred. ``None`` means "not stated"; the string
    ``"decline_to_self_identify"`` means the user chose not to say. Only the latter may ever
    be written into a form.

    Attributes:
        user_id: Owning user.
        full_name: Display name, printed on generated documents.
        email: Contact address.
        phone: Contact number, kept exactly as the user typed it.
        pronouns: Stated pronouns.
        location: Human-readable "City, Region, Country".
        address: Postal address components.
        links: Profile URLs keyed by ``github``/``linkedin``/``portfolio``/``website``/``other``.
        citizenship: Stated citizenship.
        work_authorization: Right-to-work status; drives the sponsorship rules.
        requires_sponsorship: Whether the user needs employer visa sponsorship.
        veteran_status: EEO self-identification.
        gender: EEO self-identification.
        race_ethnicity: EEO self-identification.
        disability_status: EEO self-identification.
        clearance: Security clearance level, if any.
        salary_min: Bottom of the user's acceptable band.
        salary_max: Top of the user's acceptable band.
        salary_currency: ISO 4217 code for that band.
        remote_preference: Preferred work arrangement.
        willing_to_relocate: Whether the user would move for a role.
        relocation_targets: Places the user would move to.
        desired_roles: Role titles the user is targeting.
        desired_industries: Industries the user is targeting.
        excluded_companies: Employers never to apply to.
        excluded_industries: Industries never to apply in.
        education: Structured education entries.
        start_date_availability: Free-text availability, as forms accept it.
        notice_period_weeks: Notice the user must give a current employer.
        extra: Provider-specific answers learned from previous manual resolutions.
    """

    user_id: uuid.UUID | None = None
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    pronouns: str | None = None
    location: str | None = None
    address: dict[str, Any] = field(default_factory=dict)
    links: dict[str, Any] = field(default_factory=dict)
    citizenship: str | None = None
    work_authorization: WorkAuthStatus = WorkAuthStatus.UNKNOWN
    requires_sponsorship: bool = False
    veteran_status: str | None = None
    gender: str | None = None
    race_ethnicity: str | None = None
    disability_status: str | None = None
    clearance: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    remote_preference: WorkArrangement = WorkArrangement.UNKNOWN
    willing_to_relocate: bool = False
    relocation_targets: list[str] = field(default_factory=list)
    desired_roles: list[str] = field(default_factory=list)
    desired_industries: list[str] = field(default_factory=list)
    excluded_companies: list[str] = field(default_factory=list)
    excluded_industries: list[str] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    start_date_availability: str | None = None
    notice_period_weeks: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def link(self, name: str) -> str | None:
        """Return one profile URL by canonical key.

        Args:
            name: ``"github"``, ``"linkedin"``, ``"portfolio"`` or ``"website"``.

        Returns:
            The URL, or ``None`` when the user has not supplied it.
        """
        return _as_str(self.links.get(name)) if isinstance(self.links, dict) else None

    @classmethod
    def from_model(cls, profile: Any) -> UserProfileDTO:
        """Build a DTO from a :class:`~app.models.profile.UserProfile` by duck typing.

        Three shapes are accepted, so callers do not have to unwrap:

        * a ``UserProfile`` row — its :meth:`~app.models.profile.UserProfile.to_dto` is used
          when present, since that method already resolves ``links`` and the owning user's
          name and email without emitting SQL;
        * a ``User`` row — its already-loaded ``profile`` is used, and identity fields are
          taken from the user itself;
        * any mapping or object exposing the same field names.

        Args:
            profile: The source object.

        Returns:
            The detached snapshot. Absent fields take their documented defaults rather than
            raising: an empty profile is valid and simply has nothing to say, which the
            autofiller correctly reports as "unanswerable" and routes to review.
        """
        source: Any = profile
        owner: Any = None

        nested = _read_loaded(profile, "profile")
        if nested is not None and not isinstance(profile, Mapping):
            owner, source = profile, nested

        flattener = getattr(source, "to_dto", None)
        if callable(flattener):
            try:
                flattened = flattener()
            except Exception as exc:
                logger.debug("jobs.profile_to_dto_failed", error=str(exc))
            else:
                if isinstance(flattened, Mapping):
                    source = dict(flattened)

        full_name = _as_str(_read(source, "full_name")) or (
            _as_str(_read(owner, "full_name")) if owner is not None else None
        )
        email = _as_str(_read(source, "email")) or (
            _as_str(_read(owner, "email")) if owner is not None else None
        )
        user_id = _as_uuid(_read(source, "user_id")) or (
            _as_uuid(_read(owner, "id")) if owner is not None else None
        )

        return cls(
            user_id=user_id,
            full_name=full_name,
            email=email,
            phone=_as_str(_read(source, "phone")),
            pronouns=_as_str(_read(source, "pronouns")),
            location=_as_str(_read(source, "location")),
            address=dict(_read(source, "address", {}) or {}),
            links=dict(_read(source, "links", {}) or {}),
            citizenship=_as_str(_read(source, "citizenship")),
            work_authorization=_as_enum(
                WorkAuthStatus, _read(source, "work_authorization"), WorkAuthStatus.UNKNOWN
            ),
            requires_sponsorship=bool(_read(source, "requires_sponsorship", False)),
            veteran_status=_as_str(_read(source, "veteran_status")),
            gender=_as_str(_read(source, "gender")),
            race_ethnicity=_as_str(_read(source, "race_ethnicity")),
            disability_status=_as_str(_read(source, "disability_status")),
            clearance=_as_str(_read(source, "clearance")),
            salary_min=_as_int(_read(source, "salary_min")),
            salary_max=_as_int(_read(source, "salary_max")),
            salary_currency=_as_str(_read(source, "salary_currency")),
            remote_preference=_as_enum(
                WorkArrangement, _read(source, "remote_preference"), WorkArrangement.UNKNOWN
            ),
            willing_to_relocate=bool(_read(source, "willing_to_relocate", False)),
            relocation_targets=list(_read(source, "relocation_targets", []) or []),
            desired_roles=list(_read(source, "desired_roles", []) or []),
            desired_industries=list(_read(source, "desired_industries", []) or []),
            excluded_companies=list(_read(source, "excluded_companies", []) or []),
            excluded_industries=list(_read(source, "excluded_industries", []) or []),
            education=list(_read(source, "education", []) or []),
            start_date_availability=_as_str(_read(source, "start_date_availability")),
            notice_period_weeks=_as_int(_read(source, "notice_period_weeks")),
            extra=dict(_read(source, "extra", {}) or {}),
        )


@dataclass(slots=True)
class ApplyContext:
    """Everything an :meth:`ATSProvider.apply` call needs, and nothing it does not.

    Constructed by :meth:`app.services.pipeline.Pipeline.submit` once the documents are
    rendered and the answers are planned. Notably it carries no session, no ORM row and no
    settings object: a provider decides what to do from this alone, which is what makes an
    apply flow reproducible from a checkpoint.

    Attributes:
        application_id: The :class:`~app.models.application.Application` being submitted,
            used to correlate every log line and artifact.
        posting: The job being applied to.
        user: The applicant's answer sheet.
        resume_path: Rendered resume on disk, deleted after submission when
            ``settings.delete_temp_resume_after_submit`` is on.
        cover_letter_path: Rendered cover letter on disk, when one was written.
        answers: Pre-planned answers keyed by field label or selector, produced by
            :class:`app.ai.field_answer.FieldAnswerer`.
        dry_run: When ``True``, fill everything and stop short of clicking submit. Combined
            with ``settings.auto_apply_enabled`` this is golden rule #3, and the guard is
            re-applied in :func:`app.jobs._apply.run_browser_apply`.
        knowledge: Optional :class:`app.knowledge.retrieval.KnowledgeRetriever`. Typed
            ``Any`` for the same reason as *recorder*: ``app/jobs`` must not import the
            knowledge package. Without it a free-text form answer is generated from the
            profile alone -- no knowledge grounding, and no prior correction from the
            user's memory, so the same question escalates to review a second time.
        recorder: Optional :class:`app.browser.recorder.ArtifactRecorder` collecting
            screenshots, HTML and a manifest. Untyped here so that ``app/jobs`` never imports
            the browser package.
    """

    application_id: uuid.UUID
    posting: JobPostingDTO
    user: UserProfileDTO
    resume_path: Path | None = None
    cover_letter_path: Path | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True
    recorder: Any | None = None
    knowledge: Any | None = None

    def __post_init__(self) -> None:
        """Coerce path-like arguments and guarantee ``answers`` is a dictionary."""
        if self.resume_path is not None and not isinstance(self.resume_path, Path):
            self.resume_path = Path(str(self.resume_path))
        if self.cover_letter_path is not None and not isinstance(self.cover_letter_path, Path):
            self.cover_letter_path = Path(str(self.cover_letter_path))
        if not isinstance(self.answers, dict):
            self.answers = {}
        self.dry_run = bool(self.dry_run)

    def log_context(self) -> dict[str, Any]:
        """Return the structlog bindings that identify this attempt.

        Returns:
            ``application_id``, ``posting_id`` and ``provider`` as plain strings, ready to
            pass to ``logger.bind``.
        """
        return {
            "application_id": str(self.application_id),
            "posting_id": str(self.posting.id) if self.posting.id else None,
            # ``str()`` rather than ``.value``: ATSProviderName is a string enum, so this
            # yields the same "greenhouse" for the enum and for a bare string. Defence in
            # depth — a telemetry line must never be the thing that kills a submission
            # attempt, and this one is on the kill-switch path.
            "provider": str(self.posting.provider),
        }


@dataclass(slots=True)
class ApplyResult:
    """The outcome of one submission attempt.

    Defaults are the *safe* position, not the optimistic one: an :class:`ApplyResult` that
    nobody filled in reports ``ok=False`` and
    :attr:`~app.models.enums.ApplicationStatus.NEEDS_REVIEW`, so a provider bug produces a
    question for a human rather than a false claim of success (golden rule #2).

    Attributes:
        ok: Whether the application was genuinely submitted. ``False`` in dry-run mode even
            when everything else worked, because nothing was sent.
        status: The status to write to the :class:`~app.models.application.Application`.
        review_reason: Why a human is needed, when :attr:`status` is ``needs_review``.
        confirmation_text: Success text scraped from the confirmation page.
        confirmation_id: Reference number the employer displayed, when they displayed one.
        screenshot_paths: Proof-of-submission artifacts. §12 requires a capture before and
            after every submit attempt.
        external_application_id: The employer's own identifier for this application.
        unanswered_fields: Fields that could not be answered with confidence — the evidence
            behind a ``needs_review`` outcome.
        duration_seconds: Wall-clock time the attempt took, fed to
            ``applicantos_apply_duration_seconds``.
        browser_log: Structured trace of what the browser did, retained for auditing.
        error: Human-readable failure description, when something went wrong.
    """

    ok: bool = False
    status: ApplicationStatus = ApplicationStatus.NEEDS_REVIEW
    review_reason: ReviewReason | None = None
    confirmation_text: str | None = None
    confirmation_id: str | None = None
    screenshot_paths: list[Path] = field(default_factory=list)
    external_application_id: str | None = None
    unanswered_fields: list[FormField] = field(default_factory=list)
    duration_seconds: float = 0.0
    browser_log: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def __post_init__(self) -> None:
        """Coerce loosely-typed fields so consumers need no defensive checks."""
        self.ok = bool(self.ok)
        self.status = _as_enum(ApplicationStatus, self.status, ApplicationStatus.NEEDS_REVIEW)
        if self.review_reason is not None:
            self.review_reason = _as_enum(ReviewReason, self.review_reason, None)
        self.screenshot_paths = [
            path if isinstance(path, Path) else Path(str(path))
            for path in (self.screenshot_paths or [])
        ]
        self.unanswered_fields = list(self.unanswered_fields or [])
        self.browser_log = list(self.browser_log or [])
        try:
            self.duration_seconds = float(self.duration_seconds)
        except (TypeError, ValueError):
            self.duration_seconds = 0.0

    @classmethod
    def submitted(
        cls,
        *,
        confirmation_text: str | None = None,
        confirmation_id: str | None = None,
        external_application_id: str | None = None,
        screenshot_paths: Sequence[Path] | None = None,
        duration_seconds: float = 0.0,
        browser_log: Sequence[dict[str, Any]] | None = None,
    ) -> ApplyResult:
        """Build the result of a genuine submission.

        Args:
            confirmation_text: Success text scraped from the confirmation page.
            confirmation_id: Reference number the employer displayed.
            external_application_id: The employer's identifier for this application.
            screenshot_paths: Proof-of-submission captures.
            duration_seconds: Wall-clock duration of the attempt.
            browser_log: Structured trace of the browser session.

        Returns:
            A result with ``ok=True`` and status
            :attr:`~app.models.enums.ApplicationStatus.SUBMITTED`.
        """
        return cls(
            ok=True,
            status=ApplicationStatus.SUBMITTED,
            confirmation_text=confirmation_text,
            confirmation_id=confirmation_id,
            external_application_id=external_application_id,
            screenshot_paths=list(screenshot_paths or []),
            duration_seconds=duration_seconds,
            browser_log=list(browser_log or []),
        )

    @classmethod
    def needs_review(
        cls,
        reason: ReviewReason,
        *,
        error: str | None = None,
        unanswered_fields: Sequence[FormField] | None = None,
        screenshot_paths: Sequence[Path] | None = None,
        duration_seconds: float = 0.0,
        browser_log: Sequence[dict[str, Any]] | None = None,
    ) -> ApplyResult:
        """Build the result of an attempt that stopped and asked for a human.

        Args:
            reason: Why the attempt stopped.
            error: Optional detail shown alongside the reason.
            unanswered_fields: The fields that could not be answered confidently.
            screenshot_paths: Whatever was captured before stopping.
            duration_seconds: Wall-clock duration of the attempt.
            browser_log: Structured trace of the browser session.

        Returns:
            A result with ``ok=False`` and status
            :attr:`~app.models.enums.ApplicationStatus.NEEDS_REVIEW`.
        """
        return cls(
            ok=False,
            status=ApplicationStatus.NEEDS_REVIEW,
            review_reason=reason,
            error=error,
            unanswered_fields=list(unanswered_fields or []),
            screenshot_paths=list(screenshot_paths or []),
            duration_seconds=duration_seconds,
            browser_log=list(browser_log or []),
        )

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        screenshot_paths: Sequence[Path] | None = None,
        duration_seconds: float = 0.0,
        browser_log: Sequence[dict[str, Any]] | None = None,
    ) -> ApplyResult:
        """Build the result of an attempt that broke rather than stopped deliberately.

        Args:
            error: What went wrong.
            screenshot_paths: Whatever was captured before the failure.
            duration_seconds: Wall-clock duration of the attempt.
            browser_log: Structured trace of the browser session.

        Returns:
            A result with ``ok=False`` and status
            :attr:`~app.models.enums.ApplicationStatus.FAILED`.
        """
        return cls(
            ok=False,
            status=ApplicationStatus.FAILED,
            error=error,
            screenshot_paths=list(screenshot_paths or []),
            duration_seconds=duration_seconds,
            browser_log=list(browser_log or []),
        )


# ======================================================================================
# Helpers shared by the base class
# ======================================================================================


def parse_retry_after(value: Any) -> float | None:
    """Interpret a ``Retry-After`` header as a number of seconds.

    Args:
        value: The raw header value: either delta-seconds or an HTTP-date (RFC 9110
            §10.2.3). Both forms occur in the wild.

    Returns:
        A non-negative number of seconds, or ``None`` when the value is absent or unusable.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max((deadline - datetime.now(UTC)).total_seconds(), 0.0)


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Return how long to wait before retry number *attempt* + 1.

    Args:
        attempt: Zero-based index of the attempt that just failed.
        retry_after: Server-requested cooldown in seconds, when it sent one.

    Returns:
        The delay in seconds: the server's request when present (clamped to
        :data:`MAX_RETRY_AFTER_SECONDS`), otherwise capped exponential backoff — plus up to
        :data:`RETRY_JITTER_RATIO` of that value as jitter.
    """
    if retry_after is not None and retry_after > 0:
        base = min(retry_after, MAX_RETRY_AFTER_SECONDS)
    else:
        base = min(RETRY_BASE_DELAY_SECONDS * (2**attempt), RETRY_MAX_DELAY_SECONDS)
    return base + base * RETRY_JITTER_RATIO * _JITTER_RNG.random()


def _resolve_timeout(settings: Any) -> float:
    """Determine the outbound HTTP timeout from application settings.

    Args:
        settings: The application settings object.

    Returns:
        A timeout in seconds, clamped to :data:`MIN_HTTP_TIMEOUT_SECONDS` …
        :data:`MAX_HTTP_TIMEOUT_SECONDS`, or :data:`DEFAULT_HTTP_TIMEOUT_SECONDS` when
        settings expose nothing usable.
    """
    for field_name, scale in _TIMEOUT_SETTING_FIELDS:
        raw = getattr(settings, field_name, None)
        if raw is None:
            continue
        try:
            seconds = float(raw) * scale
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return min(max(seconds, MIN_HTTP_TIMEOUT_SECONDS), MAX_HTTP_TIMEOUT_SECONDS)
    return DEFAULT_HTTP_TIMEOUT_SECONDS


# ======================================================================================
# The provider base class
# ======================================================================================


class ATSProvider(BasePlugin, abc.ABC):
    """Base class for every applicant-tracking-system integration.

    Subclasses implement two methods — :meth:`search` and :meth:`fetch_posting` — and
    declare four class attributes. Everything else (HTTP, retries, error typing, pagination,
    URL detection, health) is inherited, which is what keeps a new provider to roughly a
    page of code and keeps error handling identical across all of them.

    A minimal provider::

        @plugin
        class ExampleProvider(ATSProvider):
            meta = PluginMeta(kind=PluginKind.PROVIDER, name="example",
                              display_name="Example", capabilities=frozenset({"search"}))
            name = ATSProviderName.MANUAL
            URL_PATTERNS = [re.compile(r"jobs\\.example\\.com/")]

            async def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
                response = await self._request("GET", "https://jobs.example.com/api/postings")
                for item in response.json()["items"][: q.limit]:
                    yield self._to_raw(item)

            async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
                ...

    Class attributes:
        meta: Plugin identity; ``kind`` must be
            :attr:`~app.models.enums.PluginKind.PROVIDER`.
        name: The provider's :class:`~app.models.enums.ATSProviderName`. Declared by every
            concrete provider and validated at construction, because it is what gets written
            into ``job_postings.provider`` and must never silently default.
        supports_auto_apply: Whether :meth:`apply` really submits. Binding per
            ``docs/CONTRACTS.md`` §9 and golden rule #10.
        requires_login: Whether discovery or applying needs an authenticated session.
        URL_PATTERNS: Compiled patterns that identify a URL as belonging to this provider,
            used by :meth:`detect` and by
            :func:`app.jobs.registry.provider_for_url`.
    """

    meta: ClassVar[PluginMeta]
    name: ClassVar[ATSProviderName]
    supports_auto_apply: ClassVar[bool] = False
    requires_login: ClassVar[bool] = False
    URL_PATTERNS: ClassVar[list[re.Pattern[str]]] = []

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Construct the provider and validate that it declared a provider name.

        Args:
            settings: Application settings, supplied by the plugin registry.
            **kw: Extra construction options, kept on ``self.options``.

        Raises:
            PluginLoadError: If the class did not declare
                ``name: ClassVar[ATSProviderName]``. Failing here — at registry
                instantiation — is deliberate: a provider whose name defaulted would write
                the wrong value into every posting it discovered.
        """
        super().__init__(settings, **kw)
        declared = type(self).__dict__.get("name", getattr(type(self), "name", None))
        if not isinstance(declared, ATSProviderName):
            raise PluginLoadError(
                f"{type(self).__module__}.{type(self).__qualname__} must declare "
                f"`name: ClassVar[ATSProviderName]`; got {declared!r}"
            )
        self._client: httpx.AsyncClient | None = None
        self._client_lock: asyncio.Lock = asyncio.Lock()

    # -- identity ----------------------------------------------------------------------

    @property
    def provider_name(self) -> ATSProviderName:
        """This provider's :class:`~app.models.enums.ATSProviderName`.

        An explicitly typed alias for :attr:`name`, so call sites that need the enum do not
        have to reason about the ``str``-typed ``name`` property inherited from
        :class:`~app.plugins.base.BasePlugin`.
        """
        return type(self).name

    # -- HTTP ---------------------------------------------------------------------------

    @property
    def _http(self) -> httpx.AsyncClient:
        """The provider's shared ``httpx.AsyncClient``, built on first use.

        Lazily constructed for two reasons: ``httpx`` is a third-party import and must not be
        required to import this package, and a provider that is registered but never polled
        should not own a connection pool. Providers are registry singletons, so this is one
        pool per ATS for the lifetime of the process; :meth:`aclose` releases it.

        Returns:
            A client with the ApplicantOS User-Agent, redirect following enabled (job boards
            redirect constantly between apex and ``www``, and from short links to canonical
            posting URLs), the timeout resolved from settings, and bounded pooling.

        Raises:
            ProviderError: If ``httpx`` is not installed.
        """
        client = self._client
        if client is not None:
            return client

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a declared dependency
            raise ProviderError(
                "httpx is required for network access but is not installed",
                provider=self.provider_name,
            ) from exc

        timeout = _resolve_timeout(self.settings)
        created = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
            ),
        )
        self._client = created
        self.logger.debug("provider.http_client_created", timeout_seconds=timeout)
        return created

    async def aclose(self) -> None:
        """Close the shared HTTP client, if one was ever created.

        Idempotent and safe to call on a provider that never made a request. Called by the
        FastAPI lifespan shutdown hook and by the Celery worker teardown.
        """
        async with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()
            self.logger.debug("provider.http_client_closed")

    async def __aenter__(self) -> ATSProvider:
        """Enter an ``async with`` block; returns ``self``."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Leave an ``async with`` block, closing the HTTP client."""
        await self.aclose()

    def _classify_status(self, status_code: int, url: str, body: str) -> ProviderError:
        """Map an HTTP status onto this module's exception hierarchy.

        The single place where a status code becomes a typed failure, so that every provider
        — and therefore the pipeline — treats "throttled", "unauthenticated", "gone" and
        "broken" the same way.

        Args:
            status_code: The response status.
            url: The URL that produced it.
            body: A short excerpt of the response body, for the message.

        Returns:
            The exception to raise; it is *returned* rather than raised so the caller can
            decide whether to retry it first.
        """
        detail = f"{status_code} from {url}"
        if body:
            detail = f"{detail}: {body}"

        if status_code in RATE_LIMIT_STATUS_CODES:
            return ProviderRateLimitError(
                f"rate limited by {self.provider_name}: {detail}",
                provider=self.provider_name,
                status_code=status_code,
                url=url,
            )
        if status_code in AUTH_STATUS_CODES:
            return ProviderAuthError(
                f"{self.provider_name} refused the request: {detail}",
                provider=self.provider_name,
                status_code=status_code,
                url=url,
            )
        if status_code in UNAVAILABLE_STATUS_CODES:
            return PostingUnavailableError(
                f"{self.provider_name} has no such posting: {detail}",
                provider=self.provider_name,
                status_code=status_code,
                url=url,
            )
        if status_code >= SERVER_ERROR_STATUS:
            return ProviderError(
                f"{self.provider_name} server error: {detail}",
                provider=self.provider_name,
                status_code=status_code,
                url=url,
                transient=True,
            )
        return ProviderError(
            f"{self.provider_name} rejected the request: {detail}",
            provider=self.provider_name,
            status_code=status_code,
            url=url,
            transient=status_code in TRANSIENT_STATUS_CODES,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> httpx.Response:
        """Perform one HTTP request with typed errors and bounded retries.

        **Every provider must go through this.** It is what makes error handling uniform:
        a 429 always becomes a :class:`ProviderRateLimitError` carrying the server's
        ``Retry-After``, a 401 always becomes a :class:`ProviderAuthError`, a 404 always
        becomes a :class:`PostingUnavailableError`, and a 5xx or a transport failure is
        always retried with exponential backoff plus jitter before it becomes a
        :class:`ProviderError`.

        Retries are attempted only for failures where an identical retry could plausibly
        succeed. An authentication failure and a missing posting are raised immediately —
        retrying them wastes the budget and delays the honest answer.

        Args:
            method: HTTP method, e.g. ``"GET"``.
            url: Absolute URL to request.
            params: Query parameters.
            json: JSON body.
            data: Form body.
            headers: Extra headers, merged over the client defaults.
            timeout: Per-request timeout override in seconds.
            max_attempts: Total attempts including the first.

        Returns:
            The successful response, with its body already read.

        Raises:
            ProviderRateLimitError: On a 429 that survived the retry budget.
            ProviderAuthError: On 401 or 403.
            PostingUnavailableError: On 404 or 410.
            ProviderError: On any other failure, including transport errors and timeouts.
        """
        import httpx

        attempts = max(1, int(max_attempts))
        last_error: ProviderError | None = None

        for attempt in range(attempts):
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=json,
                    data=data,
                    headers=dict(headers) if headers else None,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = ProviderError(
                    f"{self.provider_name} request timed out: {method} {url}",
                    provider=self.provider_name,
                    url=url,
                    transient=True,
                )
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = ProviderError(
                    f"{self.provider_name} transport failure: {method} {url}: {exc}",
                    provider=self.provider_name,
                    url=url,
                    transient=True,
                )
                last_error.__cause__ = exc
            else:
                if response.status_code < CLIENT_ERROR_STATUS:
                    return response

                excerpt = response.text[:200].replace("\n", " ").strip()
                error = self._classify_status(response.status_code, url, excerpt)
                if isinstance(error, ProviderRateLimitError):
                    error.retry_after = parse_retry_after(response.headers.get(RETRY_AFTER_HEADER))
                if not error.transient:
                    raise error
                last_error = error

            if attempt + 1 >= attempts:
                break
            delay = _backoff_delay(attempt, last_error.retry_after if last_error else None)
            self.logger.info(
                "provider.request_retrying",
                method=method,
                url=url,
                attempt=attempt + 1,
                of=attempts,
                delay_seconds=round(delay, 3),
                error=str(last_error),
            )
            await asyncio.sleep(delay)

        if last_error is None:  # pragma: no cover - the loop cannot exit without one
            raise ProviderError(
                f"{self.provider_name} exhausted its retry budget with no recorded "
                f"failure: {method} {url}",
                provider=self.provider_name,
                url=url,
                transient=True,
            )
        self.logger.warning(
            "provider.request_failed",
            method=method,
            url=url,
            attempts=attempts,
            status_code=last_error.status_code,
            error=str(last_error),
        )
        raise last_error

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Any:
        """``GET`` a URL and decode the response as JSON.

        Args:
            url: Absolute URL to request.
            params: Query parameters.
            headers: Extra headers.
            timeout: Per-request timeout override in seconds.
            max_attempts: Total attempts including the first.

        Returns:
            The decoded JSON document.

        Raises:
            ProviderError: On any transport or status failure (see :meth:`_request`), or
                when the response body is not valid JSON — a board that answers 200 with an
                HTML error page is a provider failure, not a parsing bug at the call site.
        """
        response = await self._request(
            "GET", url, params=params, headers=headers, timeout=timeout, max_attempts=max_attempts
        )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.provider_name} returned a non-JSON body from {url}: {exc}",
                provider=self.provider_name,
                status_code=response.status_code,
                url=url,
            ) from exc

    # -- pagination ---------------------------------------------------------------------

    async def paginate(
        self,
        fetch: Callable[[int, str | None], Awaitable[tuple[Sequence[T], str | None]]],
        *,
        limit: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> AsyncGenerator[T, None]:
        """Drive an offset- or cursor-paginated feed from a single callback.

        Declared as an :class:`~collections.abc.AsyncGenerator` rather than the weaker
        :class:`~collections.abc.AsyncIterator` because that is what it is, and callers that
        stop early rely on it: :class:`contextlib.aclosing` needs ``aclose`` in the type to
        release a suspended page deterministically.

        *fetch* is called as ``fetch(offset, cursor)`` and returns
        ``(items, next_cursor)``. The two pagination styles fall out of what it returns:

        * **Cursor** — return the provider's continuation token as ``next_cursor``. It is
          passed back on the next call and iteration stops when it is ``None``.
        * **Offset** — return ``None`` as ``next_cursor``. ``offset`` advances by the number
          of items yielded, and iteration stops when a page comes back shorter than
          *page_size*, which is how every offset API signals exhaustion.

        Iteration also stops at *limit* items, at *max_pages* pages, on an empty page, and on
        a repeated cursor — a feed that loops must not spin forever.

        Args:
            fetch: Coroutine returning one page and the cursor for the next.
            limit: Maximum items to yield; ``None`` means "until the feed is exhausted".
            page_size: Page size the caller asked *fetch* to use, used for the offset-mode
                exhaustion test.
            max_pages: Hard ceiling on pages fetched.

        Yields:
            Items in feed order, at most *limit* of them.

        Example::

            async def fetch(offset: int, cursor: str | None):
                payload = await self._get_json(url, params={"offset": offset, "limit": 100})
                return payload["jobs"], payload.get("next")

            async for job in self.paginate(fetch, limit=q.limit):
                yield self._to_raw(job)
        """
        yielded = 0
        offset = 0
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for page in range(max_pages):
            items, next_cursor = await fetch(offset, cursor)
            if not items:
                return

            for item in items:
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

            if next_cursor is None:
                if len(items) < page_size:
                    return
                offset += len(items)
            else:
                if next_cursor in seen_cursors:
                    self.logger.warning(
                        "provider.pagination_cursor_repeated", page=page + 1, cursor=next_cursor
                    )
                    return
                seen_cursors.add(next_cursor)
                cursor = next_cursor

        self.logger.warning(
            "provider.pagination_page_limit_reached", max_pages=max_pages, yielded=yielded
        )

    # -- the provider contract ----------------------------------------------------------

    @abc.abstractmethod
    def search(self, q: SearchQuery) -> AsyncIterator[RawPosting]:
        """Yield postings matching *q*, newest first where the provider allows it.

        Implemented as an ``async def`` containing ``yield``, which makes it an async
        generator function: it is therefore declared here without ``async`` so that the
        signature matches what subclasses actually write.

        Implementations must respect ``q.limit`` and stop rather than over-fetch, must never
        widen the query, and must let :class:`ProviderError` propagate — the pipeline logs
        the failure, records it against the provider, and continues with the others.

        Args:
            q: What to look for.

        Yields:
            One :class:`RawPosting` per discovered advertisement.

        Raises:
            ProviderError: On any provider failure that survived :meth:`_request`'s retries.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch_posting(self, id_or_url: str) -> RawPosting | None:
        """Fetch one posting by provider-native identifier or by URL.

        Both forms are accepted because both occur: the pipeline re-fetches by
        ``external_id`` when re-scoring, and the user pastes a URL into the desktop app.

        Args:
            id_or_url: A provider-native identifier or a posting URL.

        Returns:
            The posting, or ``None`` when the identifier is well-formed but unknown to this
            provider — which is different from the posting having been withdrawn.

        Raises:
            PostingUnavailableError: When the posting existed and is now gone.
            ProviderError: On any other provider failure.
        """
        raise NotImplementedError

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """Return whether *url* belongs to this provider, without constructing it.

        The synchronous, class-level half of :meth:`detect`. Used by
        :func:`app.jobs.registry.provider_class_for_url` so that URL routing costs no plugin
        instantiation and no HTTP client.

        Args:
            url: The URL to test.

        Returns:
            ``True`` when any pattern in :attr:`URL_PATTERNS` matches anywhere in *url*.
        """
        if not url:
            return False
        return any(pattern.search(url) for pattern in cls.URL_PATTERNS)

    async def detect(self, url: str) -> bool:
        """Return whether this provider can handle *url*.

        The default answer comes from :attr:`URL_PATTERNS`, which is right for every provider
        with a recognisable domain. Override it when identification needs a request — for
        example a careers page that only reveals its embedded ATS in the page source — and
        keep the pattern check as the cheap first pass.

        Args:
            url: The URL to test.

        Returns:
            ``True`` when this provider claims the URL.
        """
        return self.matches_url(url)

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        """Submit an application through this provider.

        The default refuses. A provider supports automated submission only by overriding
        this *and* declaring ``supports_auto_apply = True``, and its module docstring must
        state the terms-of-service posture that justifies it (golden rule #10). Overrides
        should delegate to :func:`app.jobs._apply.run_browser_apply` rather than driving
        Playwright directly, so the kill switch and the review-escalation rules stay in one
        place.

        Args:
            ctx: Everything needed for the attempt.

        Returns:
            The outcome of the attempt.

        Raises:
            UnsupportedFlowError: Always, unless a subclass overrides this. The pipeline
                converts it into a ``needs_review`` application with
                :attr:`~app.models.enums.ReviewReason.UNSUPPORTED_FLOW`, which is the correct
                and honest outcome for Workday and LinkedIn.
        """
        raise UnsupportedFlowError(
            f"{self.provider_name} does not support automated submission; "
            f"apply manually at {ctx.posting.target_url}",
            provider=self.provider_name,
            url=ctx.posting.target_url,
        )

    async def healthcheck(self) -> bool:
        """Report whether this provider can reach its ATS right now.

        The default is ``True``: a provider with no credential to validate and no status
        endpoint to poll is healthy until a request proves otherwise, and a readiness probe
        must not itself hammer every job board. Providers with a cheap, non-rate-limited
        probe should override this; ``GET /ready`` aggregates the answers.

        Returns:
            ``True`` when the provider is usable.
        """
        return True

    def __repr__(self) -> str:
        """Return ``<ClassName provider auto_apply=…>`` for logs and error messages."""
        declared = type(self).__dict__.get("name", None)
        rendered = declared.value if isinstance(declared, ATSProviderName) else "?"
        return (
            f"<{type(self).__name__} {rendered} "
            f"auto_apply={self.supports_auto_apply} login={self.requires_login}>"
        )
