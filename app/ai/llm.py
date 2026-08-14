"""Language-model access — the one door every LLM call in ApplicantOS goes through.

``docs/CONTRACTS.md`` §10 defines the shape: a :class:`ModelPlugin` (``PluginKind.MODEL``)
exposing :meth:`~ModelPlugin.complete`, :meth:`~ModelPlugin.complete_json` and
:meth:`~ModelPlugin.count_tokens`, resolved through :func:`get_llm`. Concrete clients live
in :mod:`app.ai.models` and are never imported from outside that package (golden rule #5).

Four cross-cutting concerns are implemented here, once, rather than in each client:

**Deterministic caching.** When ``settings.cache_llm_responses`` is on *and* the call is
deterministic (``temperature == 0``), the completion is read through
:mod:`app.cache` in the ``llm`` namespace, keyed on model, system prompt, user prompt,
``max_tokens`` and the JSON schema. A served entry comes back with ``cached=True`` and costs
nothing — neither money nor daily budget. The read goes through
:meth:`~app.cache.base.Cache.get_or_set`, so a burst of identical requests produces one
upstream call rather than N.

**A daily token budget.** :class:`TokenBudget` tracks usage per UTC day against
``settings.llm_daily_token_budget`` and refuses a call that would exceed it, *before* it is
made, by raising :class:`TokenBudgetExceeded`. The counter rolls over automatically at
midnight UTC.

**Retries.** Rate limits, timeouts and transient upstream failures are retried with
exponential backoff plus jitter, bounded by ``settings.llm_max_retries`` and honouring a
``Retry-After`` header when the provider sends one. Provider SDK exceptions are normalised
into this module's hierarchy by :func:`classify_error`, which duck-types the exception
rather than importing any SDK.

**Defensive JSON.** Models wrap JSON in prose and markdown fences, however firmly they were
asked not to. :meth:`ModelPlugin.complete_json` appends the schema to the system prompt,
strips fences, extracts the outermost balanced JSON value from surrounding text, and — if
that still fails — retries exactly once with a repair instruction quoting the parser error,
before raising :class:`LLMParseError` with the offending text attached.

Everything degrades to an offline path: when the configured provider's API key is absent,
:func:`get_llm` logs one warning and returns the deterministic ``null`` model, so the whole
product runs end to end with zero API keys.
"""

from __future__ import annotations

import abc
import asyncio
import datetime as dt
import json
import random
import threading
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal

import structlog

from app.cache import NAMESPACES, get_cache, make_key
from app.models.enums import PluginKind
from app.plugins import (
    BasePlugin,
    PluginDisabled,
    PluginLoadError,
    PluginMeta,
    PluginNotFound,
    is_loaded,
    load_all,
    registry,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = [
    "ARRAY_RESULT_KEY",
    "CHARACTERS_PER_TOKEN",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIER",
    "DETERMINISTIC_TEMPERATURE",
    "MODEL_TIERS",
    "NULL_MODEL_NAME",
    "PROVIDER_API_KEY_FIELDS",
    "UNLIMITED_TOKEN_BUDGET",
    "BudgetSnapshot",
    "GuardedModelPlugin",
    "LLMError",
    "LLMParseError",
    "LLMRateLimited",
    "LLMResponse",
    "LLMTimeout",
    "ModelPlugin",
    "ModelTier",
    "TokenBudget",
    "TokenBudgetExceeded",
    "classify_error",
    "extract_json_block",
    "get_budget",
    "get_llm",
    "parse_json_payload",
    "record_llm_request",
    "record_llm_tokens",
    "reset_budget",
    "reset_llm_cache",
    "resolve_provider_name",
    "strip_code_fences",
]

logger = structlog.get_logger(__name__)

# ======================================================================================
# Constants
# ======================================================================================

#: Which of the two configured models a caller wants. ``reasoning`` is the careful, more
#: expensive model (resume tailoring, fact extraction); ``fast`` is the cheap one used for
#: high-volume classification and short rewrites.
ModelTier = Literal["reasoning", "fast"]

#: The valid values of :data:`ModelTier`, for runtime validation.
MODEL_TIERS: Final[tuple[str, ...]] = ("reasoning", "fast")

#: Tier assumed when a caller does not ask for one.
DEFAULT_TIER: Final[str] = "reasoning"

#: Characters per token in the provider-independent heuristic used by
#: :meth:`ModelPlugin.count_tokens`. Roughly right for English prose and code across every
#: BPE tokenizer in use; clients with a real tokenizer override the method.
CHARACTERS_PER_TOKEN: Final[int] = 4

#: Default completion length, per ``docs/CONTRACTS.md`` §10.
DEFAULT_MAX_TOKENS: Final[int] = 4096

#: Default sampling temperature, per ``docs/CONTRACTS.md`` §10.
DEFAULT_TEMPERATURE: Final[float] = 0.2

#: The only temperature at which a response may be cached: a sampled response is not a
#: function of its inputs, so caching one would silently freeze a random draw forever.
DETERMINISTIC_TEMPERATURE: Final[float] = 0.0

#: TTL for cached completions. ``None`` defers to ``settings.cache_default_ttl``.
LLM_CACHE_TTL_SECONDS: Final[int | None] = None

#: First backoff delay; doubled per attempt.
RETRY_BASE_DELAY_SECONDS: Final[float] = 0.5

#: Ceiling on the computed exponential delay, before jitter.
RETRY_MAX_DELAY_SECONDS: Final[float] = 30.0

#: Jitter added to each delay, as a fraction of the delay itself. Decorrelates a fleet of
#: workers that all got rate-limited by the same upstream at the same instant.
RETRY_JITTER_RATIO: Final[float] = 0.25

#: Ceiling on an honoured ``Retry-After``. A provider asking for ten minutes should not
#: block a pipeline run for ten minutes; the attempt budget runs out instead.
MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0

#: HTTP status codes worth retrying. 429 is handled separately as a rate limit; 529 is
#: Anthropic's "overloaded".
TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {408, 409, 425, 429, 500, 502, 503, 504, 529}
)

#: HTTP status code meaning "rate limited".
RATE_LIMIT_STATUS_CODE: Final[int] = 429

#: Substrings of an exception's class name that mark it transient, used when the exception
#: carries no status code (connection resets, DNS blips, provider "overloaded" errors).
TRANSIENT_ERROR_NAME_TOKENS: Final[tuple[str, ...]] = (
    "connection",
    "unavailable",
    "overloaded",
    "temporar",
    "serviceunavailable",
    "internalserver",
)

#: Substrings of an exception's class name that mark it a rate limit.
RATE_LIMIT_ERROR_NAME_TOKENS: Final[tuple[str, ...]] = ("ratelimit", "toomanyrequests", "quota")

#: Substrings of an exception's class name that mark it a timeout.
TIMEOUT_ERROR_NAME_TOKENS: Final[tuple[str, ...]] = ("timeout", "timedout", "deadlineexceeded")

#: Header carrying a provider's requested cooldown.
RETRY_AFTER_HEADER: Final[str] = "retry-after"

#: A daily token budget of zero (or less) means "no limit".
UNLIMITED_TOKEN_BUDGET: Final[int] = 0

#: Key under which a bare JSON array is returned by :meth:`ModelPlugin.complete_json`, which
#: must return a mapping. Only used when the caller's schema declares a top-level array.
ARRAY_RESULT_KEY: Final[str] = "items"

#: How much of an unparseable reply is echoed back to the model in the repair round.
RAW_ECHO_LIMIT: Final[int] = 4000

#: How much of an unparseable reply is written to the log line.
REPLY_PREVIEW_LIMIT: Final[int] = 200

#: ``settings`` field holding the API key for each provider that needs one. A provider
#: absent from this mapping (``local``, ``null``) needs no credential.
PROVIDER_API_KEY_FIELDS: Final[dict[str, str]] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
}

#: Registered name of the offline model every failure path falls back to.
NULL_MODEL_NAME: Final[str] = "null"

#: ``outcome`` label values on ``applicantos_llm_requests_total``.
OUTCOME_SUCCESS: Final[str] = "success"
OUTCOME_CACHE_HIT: Final[str] = "cache_hit"
OUTCOME_ERROR: Final[str] = "error"
OUTCOME_RATE_LIMITED: Final[str] = "rate_limited"
OUTCOME_TIMEOUT: Final[str] = "timeout"
OUTCOME_BUDGET_EXCEEDED: Final[str] = "budget_exceeded"

#: ``kind`` label values on ``applicantos_llm_tokens_total``.
TOKEN_KIND_INPUT: Final[str] = "input"
TOKEN_KIND_OUTPUT: Final[str] = "output"

#: Opening bracket to its matching closer, for :func:`extract_json_block`.
_JSON_OPENERS: Final[dict[str, str]] = {"{": "}", "[": "]"}

#: Markdown fence delimiter.
_FENCE: Final[str] = "```"

#: Instruction appended to the system prompt by :meth:`ModelPlugin.complete_json`.
SCHEMA_INSTRUCTION_TEMPLATE: Final[str] = (
    "## Response format\n\n"
    "Reply with a single JSON value that validates against this JSON Schema:\n\n"
    "```json\n{schema}\n```\n\n"
    "Output the JSON document and nothing else — no preamble, no explanation, no markdown "
    "code fence, no trailing commentary. Every required property must be present. Use "
    "`null` only where the schema allows it. Never introduce a property the schema does "
    "not declare. Escape quotes and newlines inside strings."
)

#: Prompt used for the single repair round after an unparseable reply.
REPAIR_INSTRUCTION_TEMPLATE: Final[str] = (
    "Your previous reply could not be parsed as JSON.\n\n"
    "Parser error: {error}\n\n"
    "Your previous reply, verbatim:\n"
    "<<<PREVIOUS\n{raw}\nPREVIOUS>>>\n\n"
    "Return the same information as a single raw JSON document that validates against the "
    "schema in the system prompt. Do not apologise, do not explain, do not wrap the "
    "document in a code fence. Output JSON only."
)

#: Jitter source. Deliberately unseeded and independent of the global :mod:`random` state,
#: so that decorrelating backoff never perturbs — or is perturbed by — a seeded generator
#: elsewhere (notably the deterministic null model).
_JITTER_RNG: Final[random.SystemRandom] = random.SystemRandom()


# ======================================================================================
# Errors
# ======================================================================================


class LLMError(Exception):
    """Base class for every language-model failure.

    Attributes:
        model: The model identifier the call was made against, when known.
        transient: Whether retrying the identical call could plausibly succeed.
        status_code: The upstream HTTP status, when the failure came from an HTTP response.
        retry_after: Seconds the provider asked the caller to wait, when it said so.
    """

    #: Default for ``transient`` when the caller does not state one. Subclasses that are
    #: transient by nature (timeouts, rate limits) override this.
    DEFAULT_TRANSIENT: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        transient: bool | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Record the failure together with everything needed to decide about a retry.

        Args:
            message: Human-readable explanation.
            model: The model the call targeted.
            transient: Whether a retry could succeed; defaults to :attr:`DEFAULT_TRANSIENT`.
            status_code: Upstream HTTP status, when there was one.
            retry_after: Provider-requested cooldown in seconds, when there was one.
        """
        super().__init__(message)
        self.model = model
        self.transient = self.DEFAULT_TRANSIENT if transient is None else transient
        self.status_code = status_code
        self.retry_after = retry_after


class LLMTimeout(LLMError):
    """The provider did not answer within ``settings.llm_timeout_seconds``."""

    DEFAULT_TRANSIENT: ClassVar[bool] = True


class LLMRateLimited(LLMError):
    """The provider rejected the call because the account is over its rate limit."""

    DEFAULT_TRANSIENT: ClassVar[bool] = True


class LLMParseError(LLMError):
    """The model's reply could not be parsed as JSON, even after one repair round.

    Attributes:
        raw: The unparseable reply, kept so the failure is diagnosable from the log alone.
    """

    def __init__(self, message: str, *, raw: str = "", model: str | None = None) -> None:
        """Record the parse failure and the text that caused it.

        Args:
            message: What went wrong, including the underlying parser error.
            raw: The model's reply, verbatim.
            model: The model that produced it.
        """
        super().__init__(message, model=model, transient=False)
        self.raw = raw


class TokenBudgetExceeded(LLMError):
    """The call would push today's token usage past ``settings.llm_daily_token_budget``.

    Raised *before* the request is sent. Not transient: retrying the same call on the same
    day cannot succeed, and the pipeline must degrade — to the null model, to a cached
    result, or to the deterministic fallback path — rather than spin.
    """


# ======================================================================================
# Metrics (optional — app.observability.metrics may not exist yet)
# ======================================================================================

#: Sentinel meaning "the metrics module was looked up and is not importable".
_METRICS_ABSENT: Final[object] = object()

#: Memoised result of importing ``app.observability.metrics``: the module, the sentinel, or
#: ``None`` before the first lookup.
_metrics_module: Any = None

#: Attribute names searched on the metrics module for the token counter, in priority order.
_TOKEN_COUNTER_ATTRIBUTES: Final[tuple[str, ...]] = (
    "LLM_TOKENS",
    "llm_tokens",
    "applicantos_llm_tokens_total",
)

#: Attribute names searched on the metrics module for the request counter.
_REQUEST_COUNTER_ATTRIBUTES: Final[tuple[str, ...]] = (
    "LLM_REQUESTS",
    "llm_requests",
    "applicantos_llm_requests_total",
)


def _metrics() -> Any | None:
    """Return ``app.observability.metrics``, or ``None`` when it is not importable.

    The observability package is optional here on purpose: this module must import — and
    every LLM call must work — in a bare test process, in a script, and during the phased
    build in which the metrics module does not exist yet.

    Returns:
        The metrics module, or ``None``.
    """
    global _metrics_module
    if _metrics_module is None:
        try:
            from app.observability import metrics as metrics_module
        except ImportError:
            _metrics_module = _METRICS_ABSENT
        else:
            _metrics_module = metrics_module
    return None if _metrics_module is _METRICS_ABSENT else _metrics_module


def _emit(
    recorder_name: str,
    counter_attributes: tuple[str, ...],
    labels: dict[str, str],
    amount: float,
) -> None:
    """Increment one metric, tolerating every way that could fail.

    Two shapes are supported: a module-level recorder function (preferred), or a
    prometheus-style counter object exposing ``labels(**labels).inc(amount)``.

    Args:
        recorder_name: Name of a recorder function to try first.
        counter_attributes: Counter attribute names to try, in priority order.
        labels: Metric labels.
        amount: Value to add.
    """
    module = _metrics()
    if module is None:
        return
    try:
        recorder = getattr(module, recorder_name, None)
        if callable(recorder):
            try:
                recorder(**labels, amount=amount)
            except TypeError:
                # A recorder that takes only the labels (implicit increment of one).
                recorder(**labels)
            return
        for attribute in counter_attributes:
            counter = getattr(module, attribute, None)
            if counter is not None:
                counter.labels(**labels).inc(amount)
                return
    except Exception as exc:
        logger.debug("llm.metric_emit_failed", metric=recorder_name, error=str(exc))


def record_llm_tokens(model: str, kind: str, count: int) -> None:
    """Add *count* to ``applicantos_llm_tokens_total{model,kind}``.

    Args:
        model: The model identifier.
        kind: :data:`TOKEN_KIND_INPUT` or :data:`TOKEN_KIND_OUTPUT`.
        count: Number of tokens; non-positive values are ignored.
    """
    if count > 0:
        _emit("record_llm_tokens", _TOKEN_COUNTER_ATTRIBUTES, {"model": model, "kind": kind}, count)


def record_llm_request(model: str, outcome: str) -> None:
    """Increment ``applicantos_llm_requests_total{model,outcome}``.

    Args:
        model: The model identifier.
        outcome: One of the ``OUTCOME_*`` constants.
    """
    _emit(
        "record_llm_request", _REQUEST_COUNTER_ATTRIBUTES, {"model": model, "outcome": outcome}, 1
    )


# ======================================================================================
# Response
# ======================================================================================


@dataclass(slots=True)
class LLMResponse:
    """One completion, plus everything the caller needs to account for it.

    Attributes:
        text: The model's reply as plain text. For a schema-constrained call this is the
            JSON document, already extracted from whatever container the provider used.
        input_tokens: Tokens the provider billed for the prompt.
        output_tokens: Tokens the provider billed for the completion.
        model: The model identifier that produced this reply.
        cached: Whether the reply was served from :mod:`app.cache` rather than the provider.
            A cached reply costs nothing and is not charged to the daily budget.
        raw: The provider's own response object, for debugging. ``None`` on a cache hit —
            SDK objects are not JSON-serialisable and are deliberately not stored.
    """

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cached: bool = False
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        """Total tokens billed for this call."""
        return self.input_tokens + self.output_tokens

    def as_cache_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable form stored in the cache.

        Returns:
            The text and token counts. :attr:`raw` is dropped: provider SDK objects do not
            survive a JSON round trip, and nothing downstream needs them after the fact.
        """
        return {
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
        }

    @classmethod
    def from_cache_payload(cls, payload: Any, *, model: str) -> LLMResponse:
        """Rebuild a response from a cached payload.

        Args:
            payload: Whatever the cache returned — normally a mapping produced by
                :meth:`as_cache_payload`, but a bare string is tolerated so that a legacy
                or hand-written entry cannot break a pipeline run.
            model: Model identifier to attribute the reply to when the payload omits one.

        Returns:
            The response, always with ``cached=True`` and ``raw=None``.
        """
        if isinstance(payload, dict):
            return cls(
                text=str(payload.get("text", "")),
                input_tokens=int(payload.get("input_tokens", 0) or 0),
                output_tokens=int(payload.get("output_tokens", 0) or 0),
                model=str(payload.get("model") or model),
                cached=True,
            )
        return cls(text=str(payload), model=model, cached=True)


# ======================================================================================
# Daily token budget
# ======================================================================================


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """A point-in-time reading of the daily token budget.

    Attributes:
        day: The UTC date the counters belong to, ISO-8601.
        used: Tokens consumed so far today.
        limit: The configured ceiling, or :data:`UNLIMITED_TOKEN_BUDGET` when there is none.
        remaining: Tokens left before the ceiling; ``None`` when unlimited.
        unlimited: Whether the budget is switched off.
    """

    day: str
    used: int
    limit: int
    remaining: int | None
    unlimited: bool


def _utc_day() -> str:
    """Return today's UTC date as an ISO-8601 string."""
    return dt.datetime.now(dt.UTC).date().isoformat()


class TokenBudget:
    """Tokens spent per UTC day, guarded against ``settings.llm_daily_token_budget``.

    The counter resets automatically when the UTC date changes, so a long-lived worker
    process needs no scheduled reset. Thread-safe: Celery runs tasks on a thread pool while
    this object is a process-wide singleton.

    A limit of :data:`UNLIMITED_TOKEN_BUDGET` (zero or less) disables the guard entirely
    while still counting usage, which is what :meth:`snapshot` reports to the settings API.
    """

    def __init__(self, limit: int | None = None) -> None:
        """Create a budget.

        Args:
            limit: Explicit daily ceiling. ``None`` (the normal case) reads
                ``settings.llm_daily_token_budget`` on every check, so changing the setting
                takes effect without rebuilding anything.
        """
        self._lock = threading.Lock()
        self._limit_override = limit
        self._day = _utc_day()
        self._used = 0

    # -- state -------------------------------------------------------------------------

    @property
    def limit(self) -> int:
        """The daily ceiling in tokens, or :data:`UNLIMITED_TOKEN_BUDGET` when unlimited."""
        if self._limit_override is not None:
            return max(self._limit_override, UNLIMITED_TOKEN_BUDGET)
        from app.config.settings import get_settings

        return max(int(get_settings().llm_daily_token_budget), UNLIMITED_TOKEN_BUDGET)

    @property
    def unlimited(self) -> bool:
        """Whether the guard is switched off."""
        return self.limit <= UNLIMITED_TOKEN_BUDGET

    @property
    def day(self) -> str:
        """The UTC date the current counters belong to."""
        with self._lock:
            self._roll_locked()
            return self._day

    @property
    def used(self) -> int:
        """Tokens consumed so far today."""
        with self._lock:
            self._roll_locked()
            return self._used

    @property
    def remaining(self) -> int | None:
        """Tokens left before the ceiling, or ``None`` when unlimited."""
        limit = self.limit
        if limit <= UNLIMITED_TOKEN_BUDGET:
            return None
        return max(limit - self.used, 0)

    def _roll_locked(self) -> None:
        """Reset the counter when the UTC date has advanced. Caller holds the lock."""
        today = _utc_day()
        if today != self._day:
            logger.info("llm.budget_rolled", previous_day=self._day, day=today, used=self._used)
            self._day = today
            self._used = 0

    # -- guard -------------------------------------------------------------------------

    def check(self, tokens: int, *, model: str | None = None) -> None:
        """Refuse a call that would push today's usage past the ceiling.

        Args:
            tokens: Estimated cost of the call about to be made — prompt tokens plus the
                requested completion length, which is the worst case.
            model: The model the call targets, for the error message.

        Raises:
            TokenBudgetExceeded: When the projected total exceeds the daily limit.
        """
        limit = self.limit
        if limit <= UNLIMITED_TOKEN_BUDGET:
            return
        with self._lock:
            self._roll_locked()
            projected = self._used + max(tokens, 0)
            if projected <= limit:
                return
            used, day = self._used, self._day
        logger.warning(
            "llm.budget_exceeded",
            model=model,
            day=day,
            used=used,
            requested=max(tokens, 0),
            limit=limit,
        )
        raise TokenBudgetExceeded(
            f"daily token budget exhausted for {day}: {used} used + {max(tokens, 0)} "
            f"requested exceeds the limit of {limit}",
            model=model,
        )

    def record(self, input_tokens: int, output_tokens: int, *, model: str | None = None) -> int:
        """Charge a completed call against today's usage.

        Args:
            input_tokens: Prompt tokens the provider billed.
            output_tokens: Completion tokens the provider billed.
            model: The model that was called, for the debug log.

        Returns:
            Today's total usage after the charge.
        """
        spent = max(input_tokens, 0) + max(output_tokens, 0)
        with self._lock:
            self._roll_locked()
            self._used += spent
            total = self._used
        logger.debug("llm.budget_charged", model=model, spent=spent, used=total, day=self._day)
        return total

    # -- administration -----------------------------------------------------------------

    def reset(self, *, limit: int | None = None) -> None:
        """Zero today's usage, optionally replacing the explicit limit.

        Args:
            limit: New explicit ceiling, or ``None`` to defer to settings again.
        """
        with self._lock:
            self._limit_override = limit
            self._day = _utc_day()
            self._used = 0

    def snapshot(self) -> BudgetSnapshot:
        """Return a consistent reading of the current state."""
        limit = self.limit
        used = self.used
        unlimited = limit <= UNLIMITED_TOKEN_BUDGET
        return BudgetSnapshot(
            day=self.day,
            used=used,
            limit=limit,
            remaining=None if unlimited else max(limit - used, 0),
            unlimited=unlimited,
        )

    def __repr__(self) -> str:
        """Return ``<TokenBudget day used/limit>``."""
        limit = self.limit
        rendered = "unlimited" if limit <= UNLIMITED_TOKEN_BUDGET else str(limit)
        return f"<TokenBudget {self.day} {self.used}/{rendered}>"


#: Process-wide budget. Module-level so every model client, in every task, shares one
#: counter — a per-instance budget would be trivially bypassed by asking for another tier.
_budget: TokenBudget = TokenBudget()


def get_budget() -> TokenBudget:
    """Return the process-wide daily token budget."""
    return _budget


def reset_budget(*, limit: int | None = None) -> TokenBudget:
    """Zero the process-wide budget and return it.

    Args:
        limit: Explicit ceiling for the reset budget, or ``None`` to defer to settings.

    Returns:
        The (same) process-wide :class:`TokenBudget`.
    """
    _budget.reset(limit=limit)
    return _budget


# ======================================================================================
# Error normalisation
# ======================================================================================


def _status_code_of(exc: BaseException) -> int | None:
    """Extract an HTTP status code from a provider SDK exception, if it carries one.

    Duck-typed on purpose: both the Anthropic and OpenAI SDKs expose ``status_code``
    directly or through ``.response``, and this module must not import either.

    Args:
        exc: The exception raised by a provider SDK.

    Returns:
        The status code, or ``None``.
    """
    for candidate in (exc, getattr(exc, "response", None)):
        value = getattr(candidate, "status_code", None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _headers_of(exc: BaseException) -> Any | None:
    """Return the response headers attached to a provider SDK exception, if any."""
    for candidate in (exc, getattr(exc, "response", None)):
        headers = getattr(candidate, "headers", None)
        if headers is not None:
            return headers
    return None


def _parse_retry_after(value: Any) -> float | None:
    """Interpret a ``Retry-After`` header value as a number of seconds.

    Args:
        value: The raw header value: either delta-seconds or an HTTP-date (RFC 9110 §10.2.3).

    Returns:
        A non-negative number of seconds, or ``None`` when the value is unusable.
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
        deadline = deadline.replace(tzinfo=dt.UTC)
    return max((deadline - dt.datetime.now(dt.UTC)).total_seconds(), 0.0)


def _retry_after_of(exc: BaseException) -> float | None:
    """Extract the provider-requested cooldown from an exception's headers."""
    headers = _headers_of(exc)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    try:
        raw = getter(RETRY_AFTER_HEADER) or getter(RETRY_AFTER_HEADER.title())
    except Exception:
        return None
    return _parse_retry_after(raw)


def classify_error(exc: BaseException, *, model: str | None = None) -> LLMError:
    """Normalise any provider exception into this module's hierarchy.

    Classification is duck-typed — status code, response headers, class name — so no
    provider SDK is imported here and an unknown SDK is still handled sensibly.

    Args:
        exc: The exception a provider client raised.
        model: The model the call targeted.

    Returns:
        The exception itself when it is already an :class:`LLMError`; otherwise a new
        :class:`LLMTimeout`, :class:`LLMRateLimited` or :class:`LLMError` whose
        :attr:`~LLMError.transient` flag drives the retry decision.
    """
    if isinstance(exc, LLMError):
        return exc

    message = str(exc) or type(exc).__name__
    status = _status_code_of(exc)
    retry_after = _retry_after_of(exc)
    name = type(exc).__name__.lower()

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or any(
        token in name for token in TIMEOUT_ERROR_NAME_TOKENS
    ):
        return LLMTimeout(message, model=model, status_code=status, retry_after=retry_after)

    if status == RATE_LIMIT_STATUS_CODE or any(
        token in name for token in RATE_LIMIT_ERROR_NAME_TOKENS
    ):
        return LLMRateLimited(message, model=model, status_code=status, retry_after=retry_after)

    transient = (status in TRANSIENT_STATUS_CODES) or any(
        token in name for token in TRANSIENT_ERROR_NAME_TOKENS
    )
    return LLMError(
        message,
        model=model,
        transient=transient,
        status_code=status,
        retry_after=retry_after,
    )


def _outcome_of(error: LLMError) -> str:
    """Return the metric ``outcome`` label describing *error*."""
    if isinstance(error, TokenBudgetExceeded):
        return OUTCOME_BUDGET_EXCEEDED
    if isinstance(error, LLMRateLimited):
        return OUTCOME_RATE_LIMITED
    if isinstance(error, LLMTimeout):
        return OUTCOME_TIMEOUT
    return OUTCOME_ERROR


# ======================================================================================
# Defensive JSON parsing
# ======================================================================================


def strip_code_fences(text: str) -> str:
    """Remove a wrapping markdown code fence from *text*.

    Handles the shapes models actually emit: ```` ```json ... ``` ````, ```` ``` ... ``` ````,
    and a fence that was opened but never closed because the completion hit its token limit.

    Args:
        text: The model's reply.

    Returns:
        The fence's contents when *text* is a single fenced block, otherwise *text*
        unchanged (with surrounding whitespace trimmed).
    """
    stripped = text.strip()
    if not stripped.startswith(_FENCE):
        return stripped
    body = stripped[len(_FENCE) :]
    # A fence may carry an info string ("json", "JSON5", …) on its opening line.
    newline = body.find("\n")
    if newline != -1 and _FENCE not in body[:newline]:
        body = body[newline + 1 :]
    end = body.rfind(_FENCE)
    if end != -1:
        body = body[:end]
    return body.strip()


def extract_json_block(text: str) -> str | None:
    """Return the outermost balanced JSON object or array embedded in *text*.

    Models prepend "Here is the JSON you asked for:" and append "Let me know if…". This
    walks the text tracking string and escape state, so a brace inside a string literal
    cannot unbalance the scan.

    Args:
        text: Arbitrary text that may contain a JSON value.

    Returns:
        The substring spanning the first opening bracket to its matching closer, or ``None``
        when no balanced value is present.
    """
    start = -1
    opener = ""
    for index, char in enumerate(text):
        if char in _JSON_OPENERS:
            start, opener = index, char
            break
    if start < 0:
        return None

    closer = _JSON_OPENERS[opener]
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _drop_trailing_commas(text: str) -> str:
    """Remove trailing commas before a closing bracket — the commonest JSON-ish mistake.

    Args:
        text: A JSON-like document.

    Returns:
        The document with ``,}`` and ``,]`` collapsed. Applied only as a last resort,
        because it operates on the raw text and would also rewrite such a sequence inside a
        string literal.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    pending_comma = -1
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            pending_comma = -1
        elif char == ",":
            pending_comma = len(out)
        elif char in "}]" and pending_comma >= 0:
            del out[pending_comma]
            pending_comma = -1
        elif not char.isspace():
            pending_comma = -1
        out.append(char)
    return "".join(out)


def parse_json_payload(text: str) -> Any:
    """Parse a model reply into JSON, tolerating the containers models wrap it in.

    Four attempts, cheapest first: the text as-is, the text with its markdown fence
    removed, the outermost balanced value extracted from surrounding prose, and finally
    that value with trailing commas dropped.

    Args:
        text: The model's reply.

    Returns:
        The decoded JSON value.

    Raises:
        ValueError: When no attempt yields valid JSON. The message quotes the decoder's
            complaint, which is what the repair round shows the model.
    """
    candidates: list[str] = []
    for candidate in (text, strip_code_fences(text)):
        cleaned = candidate.strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    for source in list(candidates):
        block = extract_json_block(source)
        if block and block not in candidates:
            candidates.append(block)
    for source in list(candidates):
        repaired = _drop_trailing_commas(source)
        if repaired != source and repaired not in candidates:
            candidates.append(repaired)

    if not candidates:
        raise ValueError("the model returned an empty reply")

    first_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
    raise ValueError(
        f"no JSON value could be decoded from the reply: {first_error}"
        if first_error is not None
        else "no JSON value could be decoded from the reply"
    )


def _schema_expects_array(schema: dict[str, Any] | None) -> bool:
    """Return whether *schema* declares a top-level array."""
    if not isinstance(schema, dict):
        return False
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared == "array"
    if isinstance(declared, (list, tuple)):
        return "array" in declared
    return "items" in schema or "prefixItems" in schema


def _as_json_object(value: Any, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a decoded JSON value into the mapping ``complete_json`` must return.

    Args:
        value: The decoded value.
        schema: The schema the caller asked for.

    Returns:
        *value* itself when it is an object, or a single-key wrapper under
        :data:`ARRAY_RESULT_KEY` when the caller's schema declared a top-level array.

    Raises:
        ValueError: When *value* is neither, which sends the call into the repair round.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and _schema_expects_array(schema):
        return {ARRAY_RESULT_KEY: list(value)}
    raise ValueError(f"expected a JSON object at the top level, got {type(value).__name__}")


# ======================================================================================
# The model plugin
# ======================================================================================


class ModelPlugin(BasePlugin, abc.ABC):
    """A language-model client (``PluginKind.MODEL``), per ``docs/CONTRACTS.md`` §10.

    Subclasses implement :meth:`complete`; everything else — schema-constrained JSON,
    defensive parsing, the repair round, token estimation — is provided here so that all
    four built-in clients behave identically. In practice a client subclasses
    :class:`GuardedModelPlugin`, which additionally supplies caching, the budget guard,
    retries and metrics.

    An instance is bound to one :data:`ModelTier` at construction (``tier="fast"``), which
    is what selects between ``settings.llm_model_reasoning`` and ``settings.llm_model_fast``.
    Use :func:`get_llm` rather than constructing one.

    Attributes:
        tier: The tier this instance resolves its model name from.
    """

    meta: ClassVar[PluginMeta]

    def __init__(self, settings: Settings, **kw: Any) -> None:
        """Bind this client to a settings object and a model tier.

        Args:
            settings: Application settings.
            **kw: Extra options; ``tier`` selects the model tier and defaults to
                :data:`DEFAULT_TIER`.

        Raises:
            ValueError: If ``tier`` is not one of :data:`MODEL_TIERS`.
        """
        super().__init__(settings, **kw)
        tier = str(kw.get("tier", DEFAULT_TIER))
        if tier not in MODEL_TIERS:
            raise ValueError(f"unknown model tier {tier!r}; expected one of {MODEL_TIERS}")
        self.tier: str = tier

    # -- model identity -----------------------------------------------------------------

    def model_for_tier(self, tier: str) -> str:
        """Return the model identifier this client uses for *tier*.

        Args:
            tier: One of :data:`MODEL_TIERS`.

        Returns:
            ``settings.llm_model_fast`` for the fast tier, ``settings.llm_model_reasoning``
            otherwise. Clients with a single model (local, null) override this.
        """
        if tier == "fast":
            return self.settings.llm_model_fast
        return self.settings.llm_model_reasoning

    @property
    def model(self) -> str:
        """The model identifier this instance calls."""
        return self.model_for_tier(self.tier)

    # -- completion ----------------------------------------------------------------------

    @abc.abstractmethod
    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Produce one completion.

        Args:
            system: The system prompt — instructions, role, constraints.
            prompt: The user message.
            max_tokens: Maximum completion length.
            temperature: Sampling temperature. ``0`` makes the call deterministic and is the
                only value at which the reply may be cached.
            json_schema: When given, the reply must be a JSON value matching this schema;
                clients use whatever structured-output mechanism their provider offers.

        Returns:
            The completion and its token accounting.

        Raises:
            LLMError: On any provider failure that survived the retry policy.
            TokenBudgetExceeded: When the call would exceed the daily token budget.
        """

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        **kw: Any,
    ) -> dict[str, Any]:
        """Produce one completion and decode it as a JSON object.

        The schema is appended to the system prompt *and* passed to :meth:`complete` as
        ``json_schema``, so a provider with real structured output uses it while a provider
        without one is still told exactly what to produce. The reply is parsed defensively
        (:func:`parse_json_payload`). On failure the call is retried exactly once with a
        repair instruction quoting the parser error and echoing the previous reply.

        Args:
            system: The system prompt; the schema block is appended to it.
            prompt: The user message.
            schema: JSON Schema the reply must satisfy.
            **kw: Forwarded to :meth:`complete` (``max_tokens``, ``temperature``, and
                ``json_schema`` to override what is derived from *schema*).

        Returns:
            The decoded object. A schema declaring a top-level array yields
            ``{"items": [...]}``.

        Raises:
            LLMParseError: When the reply is still unparseable after the repair round; the
                offending text is attached as :attr:`LLMParseError.raw`.
            LLMError: On any provider failure.
            TokenBudgetExceeded: When a call would exceed the daily token budget.
        """
        options = dict(kw)
        options.setdefault("json_schema", schema)
        system_with_schema = self._system_with_schema(system, schema)

        response = await self.complete(system=system_with_schema, prompt=prompt, **options)
        try:
            return _as_json_object(parse_json_payload(response.text), schema)
        except ValueError as exc:
            # Bind outside the handler: Python unbinds `exc` when the block ends.
            first_error: ValueError = exc
            logger.warning(
                "llm.json_parse_failed",
                model=self.model,
                attempt=1,
                error=str(first_error),
                reply_preview=response.text[:REPLY_PREVIEW_LIMIT],
            )

        repair = REPAIR_INSTRUCTION_TEMPLATE.format(
            error=first_error, raw=response.text[:RAW_ECHO_LIMIT]
        )
        repaired = await self.complete(
            system=system_with_schema,
            prompt=f"{prompt}\n\n{repair}",
            **options,
        )
        try:
            result = _as_json_object(parse_json_payload(repaired.text), schema)
        except ValueError as second_error:
            logger.error(
                "llm.json_parse_failed",
                model=self.model,
                attempt=2,
                error=str(second_error),
                reply_preview=repaired.text[:REPLY_PREVIEW_LIMIT],
            )
            raise LLMParseError(
                f"model reply is not valid JSON after one repair attempt: {second_error}",
                raw=repaired.text,
                model=self.model,
            ) from second_error
        logger.info("llm.json_repaired", model=self.model)
        return result

    @staticmethod
    def _system_with_schema(system: str, schema: dict[str, Any]) -> str:
        """Append the response-format block describing *schema* to a system prompt.

        Args:
            system: The caller's system prompt.
            schema: The JSON Schema to advertise.

        Returns:
            The combined system prompt.
        """
        rendered = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False)
        block = SCHEMA_INSTRUCTION_TEMPLATE.format(schema=rendered)
        return f"{system.rstrip()}\n\n{block}" if system.strip() else block

    # -- accounting ------------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """Estimate how many tokens *text* occupies.

        The default is the provider-independent ``len(text) // 4`` heuristic — close enough
        for budget pre-flight checks and prompt-size guards, and free. A client with a real
        tokenizer available offline should override this.

        Args:
            text: The text to measure.

        Returns:
            A non-negative token estimate.
        """
        return len(text) // CHARACTERS_PER_TOKEN


class GuardedModelPlugin(ModelPlugin, abc.ABC):
    """A :class:`ModelPlugin` wrapped in the policies every real provider call needs.

    Subclasses implement one method, :meth:`_invoke`, which performs a single unguarded
    request. This class turns that into :meth:`~ModelPlugin.complete` by adding, in order:

    1. **Cache lookup** — for deterministic calls only, through
       :meth:`~app.cache.base.Cache.get_or_set` so concurrent identical calls collapse into
       one upstream request.
    2. **Budget check** — the projected cost is charged against :func:`get_budget` before
       anything is sent.
    3. **Retries** — bounded by ``settings.llm_max_retries``, with exponential backoff,
       jitter, and ``Retry-After`` honoured when the provider supplies it.
    4. **Metrics** — ``applicantos_llm_tokens_total`` and ``applicantos_llm_requests_total``.
    """

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Produce one completion, cached, budgeted, retried and measured.

        See :meth:`ModelPlugin.complete` for the argument contract.

        Args:
            system: The system prompt.
            prompt: The user message.
            max_tokens: Maximum completion length.
            temperature: Sampling temperature; ``0`` enables caching.
            json_schema: Schema the reply must match, when structured output is wanted.

        Returns:
            The completion, with ``cached=True`` when it was served from the cache.

        Raises:
            LLMError: On any provider failure that survived the retry policy.
            TokenBudgetExceeded: When the call would exceed the daily token budget.
        """
        model = self.model
        if not self._caching_enabled(temperature):
            return await self._call(
                model=model,
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                json_schema=json_schema,
            )

        key = make_key(NAMESPACES.LLM, model, system, prompt, max_tokens, json_schema)
        fresh: LLMResponse | None = None

        async def factory() -> dict[str, Any]:
            nonlocal fresh
            fresh = await self._call(
                model=model,
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                json_schema=json_schema,
            )
            return fresh.as_cache_payload()

        payload = await get_cache().get_or_set(key, factory, ttl=LLM_CACHE_TTL_SECONDS)
        if fresh is not None:
            if not fresh.text.strip():
                # An empty reply is a failure wearing a success's clothes, and caching one
                # makes a transient failure permanent: every retry, for the whole TTL, is
                # served the same empty string and cannot possibly parse. Seen on 2026-08-14
                # — a reasoning model spent its entire completion allowance on hidden
                # reasoning, returned nothing, and the empty result was cached, so the same
                # question failed identically on every later run and the application
                # escalated to a human each time. Rule #9 says cache aggressively and
                # invalidate precisely; a non-answer is not a thing to keep.
                await get_cache().delete(key)
                logger.warning("llm.empty_reply_not_cached", model=model, key=key)
            return fresh
        served = LLMResponse.from_cache_payload(payload, model=model)
        if not served.text.strip():
            # A poisoned entry written before the check above, or by an older build. Drop it
            # and answer for real rather than replaying the emptiness.
            await get_cache().delete(key)
            logger.warning("llm.empty_reply_evicted", model=model, key=key)
            return await self._call(
                model=model,
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                json_schema=json_schema,
            )
        record_llm_request(model, OUTCOME_CACHE_HIT)
        logger.debug("llm.cache_hit", model=model, key=key)
        return served

    def _caching_enabled(self, temperature: float) -> bool:
        """Return whether a call at *temperature* may be served from, and written to, cache.

        Args:
            temperature: The sampling temperature of the call.

        Returns:
            ``True`` only when ``settings.cache_llm_responses`` is on and the call is
            deterministic — caching a sampled reply would freeze one random draw forever.
        """
        return bool(self.settings.cache_llm_responses) and temperature == DETERMINISTIC_TEMPERATURE

    async def _call(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
    ) -> LLMResponse:
        """Check the budget, invoke with retries, then account for the result.

        Args:
            model: The resolved model identifier.
            system: The system prompt.
            prompt: The user message.
            max_tokens: Maximum completion length.
            temperature: Sampling temperature.
            json_schema: Schema for structured output, when requested.

        Returns:
            The fresh completion.

        Raises:
            LLMError: On any provider failure that survived the retry policy.
            TokenBudgetExceeded: When the call would exceed the daily token budget.
        """
        budget = get_budget()
        estimate = self.count_tokens(system) + self.count_tokens(prompt) + max(max_tokens, 0)
        try:
            budget.check(estimate, model=model)
        except TokenBudgetExceeded:
            record_llm_request(model, OUTCOME_BUDGET_EXCEEDED)
            raise

        response = await self._invoke_with_retries(
            model=model,
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
        )

        budget.record(response.input_tokens, response.output_tokens, model=model)
        record_llm_tokens(model, TOKEN_KIND_INPUT, response.input_tokens)
        record_llm_tokens(model, TOKEN_KIND_OUTPUT, response.output_tokens)
        record_llm_request(model, OUTCOME_SUCCESS)
        return response

    async def _invoke_with_retries(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
    ) -> LLMResponse:
        """Call :meth:`_invoke`, retrying transient failures with backoff and jitter.

        Args:
            model: The resolved model identifier.
            system: The system prompt.
            prompt: The user message.
            max_tokens: Maximum completion length.
            temperature: Sampling temperature.
            json_schema: Schema for structured output, when requested.

        Returns:
            The completion from the first attempt that succeeded.

        Raises:
            LLMError: The normalised error from the final attempt, or the first
                non-transient error encountered.
        """
        attempts = max(int(self.settings.llm_max_retries), 0) + 1
        last: LLMError | None = None

        for attempt in range(attempts):
            try:
                return await self._invoke(
                    model=model,
                    system=system,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    json_schema=json_schema,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = classify_error(exc, model=model)

            is_final = attempt == attempts - 1
            if not last.transient or is_final:
                record_llm_request(model, _outcome_of(last))
                logger.warning(
                    "llm.call_failed",
                    model=model,
                    attempt=attempt + 1,
                    attempts=attempts,
                    transient=last.transient,
                    status_code=last.status_code,
                    error=str(last),
                )
                raise last

            delay = self._backoff_delay(attempt, last.retry_after)
            logger.warning(
                "llm.retrying",
                model=model,
                attempt=attempt + 1,
                attempts=attempts,
                delay_seconds=round(delay, 3),
                status_code=last.status_code,
                error=str(last),
            )
            await asyncio.sleep(delay)

        # Unreachable: the loop either returns or raises on its final iteration.
        raise last if last is not None else LLMError("no attempt was made", model=model)

    @staticmethod
    def _backoff_delay(attempt: int, retry_after: float | None) -> float:
        """Return how long to wait before retry number *attempt* + 1.

        Args:
            attempt: Zero-based index of the attempt that just failed.
            retry_after: Provider-requested cooldown in seconds, when it sent one.

        Returns:
            The delay in seconds: the provider's request when present (clamped to
            :data:`MAX_RETRY_AFTER_SECONDS`), otherwise capped exponential backoff — plus
            up to :data:`RETRY_JITTER_RATIO` of that value as jitter.
        """
        if retry_after is not None and retry_after > 0:
            base = min(retry_after, MAX_RETRY_AFTER_SECONDS)
        else:
            base = min(RETRY_BASE_DELAY_SECONDS * (2**attempt), RETRY_MAX_DELAY_SECONDS)
        return base + base * RETRY_JITTER_RATIO * _JITTER_RNG.random()

    @abc.abstractmethod
    async def _invoke(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
    ) -> LLMResponse:
        """Perform one unguarded request against the provider.

        Implementations translate the arguments into their SDK's shape and the SDK's reply
        into an :class:`LLMResponse`. They must not retry, cache, or account for tokens —
        :class:`GuardedModelPlugin` does all three. Provider exceptions may propagate as-is;
        :func:`classify_error` normalises them.

        Args:
            model: The resolved model identifier.
            system: The system prompt.
            prompt: The user message.
            max_tokens: Maximum completion length.
            temperature: Sampling temperature.
            json_schema: Schema the reply must match, when structured output is wanted.

        Returns:
            The provider's reply.
        """


# ======================================================================================
# Resolution
# ======================================================================================

#: Memoised model instances, keyed by ``(plugin name, tier)``. The plugin registry caches
#: one instance per name, but a tier is a construction argument, so the two tiers of one
#: provider are two instances and are memoised here instead.
_llm_instances: dict[tuple[str, str], ModelPlugin] = {}

#: Guards :data:`_llm_instances` and :data:`_warned_providers`.
_llm_lock: Final[threading.RLock] = threading.RLock()

#: Providers already warned about, so the "no API key, falling back to null" message is
#: logged exactly once per process rather than on every call.
_warned_providers: set[str] = set()


def _warn_once(provider: str, event: str, **fields: Any) -> None:
    """Log *event* for *provider* at most once per process.

    Args:
        provider: The configured provider name.
        event: The structlog event name.
        **fields: Additional bound fields.
    """
    with _llm_lock:
        if provider in _warned_providers:
            return
        _warned_providers.add(provider)
    logger.warning(event, provider=provider, fallback=NULL_MODEL_NAME, **fields)


def resolve_provider_name(settings: Settings) -> str:
    """Return the model plugin name to use, honouring the offline fallback.

    ``settings.llm_provider`` names the intent; this function answers what is actually
    usable. A provider that needs an API key and has not been given one resolves to
    :data:`NULL_MODEL_NAME`, with a single warning — the product is required to run end to
    end with zero API keys.

    Args:
        settings: The settings to read.

    Returns:
        A registered model plugin name.
    """
    provider = str(settings.llm_provider)
    key_field = PROVIDER_API_KEY_FIELDS.get(provider)
    if key_field is None:
        return provider
    api_key = getattr(settings, key_field, None)
    if isinstance(api_key, str) and api_key.strip():
        return provider
    _warn_once(
        provider,
        "llm.api_key_missing",
        setting=key_field.upper(),
        detail="running offline on the deterministic null model",
    )
    return NULL_MODEL_NAME


def _resolve_class(name: str) -> type[Any]:
    """Return the registered model class for *name*, falling back to the null model.

    Args:
        name: A model plugin name.

    Returns:
        The plugin class.

    Raises:
        PluginNotFound: When neither *name* nor the null model is registered, which means
            :func:`app.plugins.load_all` did not import :mod:`app.ai.models`.
        PluginDisabled: When the null model itself has been switched off.
    """
    if not is_loaded():
        load_all()
    try:
        if not registry.is_enabled(PluginKind.MODEL, name):
            raise PluginDisabled(
                f"model plugin {name!r} is disabled", kind=PluginKind.MODEL, name=name
            )
        return registry.get_class(PluginKind.MODEL, name)
    except (PluginNotFound, PluginDisabled):
        if name == NULL_MODEL_NAME:
            raise
        _warn_once(name, "llm.plugin_unavailable", detail="model plugin missing or disabled")
        return registry.get_class(PluginKind.MODEL, NULL_MODEL_NAME)


def get_llm(tier: ModelTier = "reasoning") -> ModelPlugin:
    """Return the model client for *tier*, per ``docs/CONTRACTS.md`` §10.

    Resolution order: ``settings.llm_provider`` names the provider; a missing API key or an
    unavailable plugin degrades to the deterministic ``null`` model with one warning. The
    resulting instance is memoised per ``(provider, tier)`` for the life of the process.

    Args:
        tier: ``"reasoning"`` for the careful model, ``"fast"`` for the cheap one.

    Returns:
        A ready-to-use :class:`ModelPlugin`.

    Raises:
        ValueError: If *tier* is not one of :data:`MODEL_TIERS`.
        PluginNotFound: If not even the null model is registered.
        PluginLoadError: If the client's constructor failed.
    """
    if tier not in MODEL_TIERS:
        raise ValueError(f"unknown model tier {tier!r}; expected one of {MODEL_TIERS}")

    from app.config.settings import get_settings

    settings = get_settings()
    name = resolve_provider_name(settings)

    with _llm_lock:
        cached = _llm_instances.get((name, tier))
        if cached is not None:
            return cached

    cls = _resolve_class(name)
    try:
        instance = cls(settings, tier=tier)
    except Exception as exc:
        raise PluginLoadError(
            f"failed to construct model plugin {name!r}: {exc}",
            kind=PluginKind.MODEL,
            name=name,
        ) from exc
    if not isinstance(instance, ModelPlugin):
        raise PluginLoadError(
            f"model plugin {name!r} is not a ModelPlugin (got {type(instance).__name__})",
            kind=PluginKind.MODEL,
            name=name,
        )

    with _llm_lock:
        _llm_instances.setdefault((name, tier), instance)
        return _llm_instances[(name, tier)]


def reset_llm_cache() -> None:
    """Discard memoised model clients and the once-only warning state.

    For tests and for a settings reload: the next :func:`get_llm` re-resolves the provider
    and rebuilds the client against current settings.
    """
    with _llm_lock:
        _llm_instances.clear()
        _warned_providers.clear()
