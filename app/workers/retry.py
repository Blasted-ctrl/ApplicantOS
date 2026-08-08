"""When a task may be run again, and when running it again would be harmful.

Retrying is not free and it is not always safe. This module splits every failure into three
kinds and treats them differently, because conflating them is how automation applies to the
same job twice or re-drives a browser at a form a human is already filling in.

**Transient.** A rate-limited provider, a dropped socket, a database that briefly went away.
Nothing is wrong with the request; the world was busy. These retry with exponential backoff
and jitter — :data:`RETRYABLE_EXCEPTIONS`.

**Terminal by decision.** ``needs_review``, ``blocked``, ``already_applied``, ``skipped``.
These are *answers*, not failures. ``docs/CONTRACTS.md`` §15 states it plainly: "``NEEDS_REVIEW``
and policy blocks are terminal and never retried." The consequence of getting this wrong is
concrete — an escalated application is parked in front of a person, and a retry would open a
second browser against the same form while they are looking at it, or race the daily cap
guard that just refused. So the check is **explicit**: :func:`is_terminal_outcome` inspects
the verdict a service returned, and :func:`retryable` consults it before it considers any
further attempt. It is not left to the fact that a terminal verdict happens not to raise.

**Terminal by nature.** A malformed id, a provider whose ToS forbids automation, an exhausted
token budget, a missing Playwright install. Re-running these produces the identical failure
and burns a worker slot — :data:`TERMINAL_EXCEPTIONS`.

The classification order matters: a terminal exception that happens to *subclass* a retryable
one (``BrowserAutomationUnavailable`` is a ``BrowserSessionError``) must be caught by the
terminal test first, which is why :func:`is_retryable` checks it that way round.
"""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable, Mapping
from typing import Any, Final, TypeVar, cast

import structlog

__all__ = [
    "DEFAULT_ATTEMPTS",
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "RETRYABLE_EXCEPTIONS",
    "TERMINAL_EXCEPTIONS",
    "NonRetryable",
    "backoff",
    "is_retryable",
    "is_terminal_exception",
    "is_terminal_outcome",
    "retry_after_of",
    "retryable",
    "terminal_verdicts",
]

logger = structlog.get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

#: Attempts a decorated body gets in total, including the first. Three is the point where
#: another try stops being evidence of transience.
DEFAULT_ATTEMPTS: Final[int] = 3

#: Base of the exponential backoff curve. ``base ** attempt`` seconds.
DEFAULT_BACKOFF_BASE: Final[float] = 2.0

#: Ceiling on any single wait. Five minutes is longer than every rate-limit window the
#: supported providers publish, and short enough that a worker slot is not parked for an
#: hour on one posting.
DEFAULT_BACKOFF_CAP_SECONDS: Final[float] = 300.0

#: Key a service result carries its verdict under, when the result is a mapping.
_VERDICT_KEY: Final[str] = "verdict"

#: Attribute a service result carries its verdict under, when the result is an object
#: (:class:`~app.services.pipeline.PipelineResult`).
_VERDICT_ATTR: Final[str] = "verdict"


class NonRetryable(RuntimeError):
    """Raised to state that a failure is final, whatever its cause looks like.

    An escape hatch for a task that has learned something the exception type cannot express
    — for instance that the underlying record was deleted between the enqueue and the run.
    Always classified terminal.
    """


# ======================================================================================
# Classification
# ======================================================================================


def _collect(*candidates: tuple[str, str]) -> tuple[type[BaseException], ...]:
    """Import exception classes by ``(module, name)``, skipping any that are unavailable.

    Every optional dependency in this project degrades rather than fails, so a class that
    cannot be imported simply drops out of the classification instead of taking the worker
    down with it.

    Args:
        *candidates: ``(module path, class name)`` pairs.

    Returns:
        The classes that resolved, in the order given, deduplicated.
    """
    import importlib

    resolved: list[type[BaseException]] = []
    for module_name, class_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            logger.debug("workers.retry_class_unavailable", module=module_name)
            continue
        candidate = getattr(module, class_name, None)
        if (
            isinstance(candidate, type)
            and issubclass(candidate, BaseException)
            and candidate not in resolved
        ):
            resolved.append(candidate)
    return tuple(resolved)


#: Failures worth another attempt: provider rate limits, transient network and model
#: failures, and the database operational errors that mean "the connection went away".
#:
#: ``ProviderError`` itself is deliberately **absent** — it is the base of
#: ``UnsupportedFlowError``, which must never be retried. Only the rate-limit subclass is
#: transient.
RETRYABLE_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    ConnectionError,
    TimeoutError,
) + _collect(
    ("app.jobs.base", "ProviderRateLimitError"),
    ("app.ai.llm", "LLMRateLimited"),
    ("app.ai.llm", "LLMTimeout"),
    ("app.browser.playwright_runner", "BrowserSessionError"),
    ("sqlalchemy.exc", "OperationalError"),
    ("sqlalchemy.exc", "InterfaceError"),
    ("sqlalchemy.exc", "DisconnectionError"),
    ("sqlalchemy.exc", "TimeoutError"),
)

#: Failures that will reproduce exactly, and outcomes that are decisions rather than errors.
#: Checked **before** :data:`RETRYABLE_EXCEPTIONS`, because some of these subclass one of
#: those (``BrowserAutomationUnavailable`` is a ``BrowserSessionError``: Playwright is not
#: installed, and it will still not be installed in thirty seconds).
TERMINAL_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    NonRetryable,
    LookupError,
    ValueError,
    NotImplementedError,
) + _collect(
    ("app.jobs.base", "UnsupportedFlowError"),
    ("app.jobs.base", "ProviderAuthError"),
    ("app.ai.llm", "TokenBudgetExceeded"),
    ("app.ai.llm", "LLMParseError"),
    ("app.browser.playwright_runner", "BrowserAutomationUnavailable"),
    ("sqlalchemy.exc", "IntegrityError"),
    ("sqlalchemy.exc", "ProgrammingError"),
)


@functools.lru_cache(maxsize=1)
def terminal_verdicts() -> frozenset[str]:
    """Return the :class:`~app.services.pipeline.PipelineResult` verdicts that end a task.

    Read from the pipeline's own constants rather than restated as literals, so renaming a
    verdict cannot silently make an escalation retryable. Imported lazily because most task
    modules that use the backoff helpers never look at a pipeline verdict.

    Returns:
        ``needs_review``, ``blocked``, ``already_applied`` and ``skipped``. Only ``failed``
        is left out, and only ``failed`` may be tried again.
    """
    from app.services.pipeline import (
        VERDICT_ALREADY_APPLIED,
        VERDICT_BLOCKED,
        VERDICT_NEEDS_REVIEW,
        VERDICT_SKIPPED,
    )

    return frozenset(
        {
            VERDICT_NEEDS_REVIEW,
            VERDICT_BLOCKED,
            VERDICT_ALREADY_APPLIED,
            VERDICT_SKIPPED,
        }
    )


def is_terminal_outcome(value: Any) -> bool:
    """Return whether a service result says "do not run this again".

    The explicit half of the §15 rule. Accepts either a
    :class:`~app.services.pipeline.PipelineResult` or the JSON mapping a task returns, so the
    same check works before and after serialisation.

    Args:
        value: A result object, a result mapping, or anything else.

    Returns:
        ``True`` when the value carries a verdict in :func:`terminal_verdicts`. ``False`` for
        values that carry no verdict at all — silence is not consent, and a task that returns
        a plain counter is judged on its exceptions instead.
    """
    if isinstance(value, Mapping):
        verdict = value.get(_VERDICT_KEY)
    else:
        verdict = getattr(value, _VERDICT_ATTR, None)
    if not isinstance(verdict, str):
        return False
    return verdict in terminal_verdicts()


def is_terminal_exception(exc: BaseException) -> bool:
    """Return whether *exc* will reproduce identically on another attempt.

    Args:
        exc: The exception raised by a task body.

    Returns:
        ``True`` for the classes in :data:`TERMINAL_EXCEPTIONS`.
    """
    return isinstance(exc, TERMINAL_EXCEPTIONS)


def is_retryable(exc: BaseException) -> bool:
    """Return whether *exc* is transient enough to justify another attempt.

    Three tests, in order:

    1. :data:`TERMINAL_EXCEPTIONS` wins outright — the terminal classes include subclasses
       of retryable ones, so this must come first.
    2. A ``transient`` attribute, if the exception carries one, is believed. Every
       :class:`~app.jobs.base.ProviderError` sets it from the response it was built from, and
       a provider that has already decided "a retry could work" knows more than a class-name
       lookup does.
    3. Otherwise, membership in :data:`RETRYABLE_EXCEPTIONS`.

    Args:
        exc: The exception raised by a task body.

    Returns:
        Whether another attempt is worth making.
    """
    if is_terminal_exception(exc):
        return False
    declared = getattr(exc, "transient", None)
    if isinstance(declared, bool):
        return declared
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


def retry_after_of(exc: BaseException) -> float | None:
    """Return the cooldown a failure explicitly asked for, in seconds.

    A ``429`` that carried ``Retry-After`` is telling the caller exactly when it may come
    back. Guessing an exponential delay instead either wastes time or earns a second rate
    limit, so the server's own number wins when there is one.

    Args:
        exc: The exception raised by a task body.

    Returns:
        The requested wait in seconds, or ``None`` when the failure named none.
    """
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


# ======================================================================================
# Backoff
# ======================================================================================


def backoff(
    attempt: int,
    base: float = DEFAULT_BACKOFF_BASE,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
    jitter: bool = True,
) -> float:
    """Return how long to wait before retry number *attempt*.

    Exponential with a hard ceiling, and jittered by default. The jitter is not decoration:
    without it, twenty workers that all hit the same rate-limited provider retry in lockstep
    and rate-limit it again in a synchronised thundering herd. Full jitter spreads them
    across the whole window.

    Args:
        attempt: 1-based retry number. Values below one are treated as one.
        base: Exponential base. ``base ** attempt`` seconds before capping.
        cap: Ceiling on the returned delay, in seconds.
        jitter: When true, return a uniform sample from ``[delay / 2, delay]`` instead of the
            bare curve.

    Returns:
        A non-negative delay in seconds.

    Example:
        >>> backoff(1, jitter=False)
        2.0
        >>> backoff(4, jitter=False)
        16.0
        >>> backoff(50, jitter=False)
        300.0
    """
    step = max(1, int(attempt))
    ceiling = max(0.0, float(cap))
    try:
        raw = float(base) ** step
    except OverflowError:  # pragma: no cover - only for absurd bases
        raw = ceiling
    delay = min(ceiling, raw)
    if jitter and delay > 0.0:
        delay = random.uniform(delay / 2.0, delay)
    return max(0.0, delay)


# ======================================================================================
# The decorator
# ======================================================================================


def retryable(
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base: float = DEFAULT_BACKOFF_BASE,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
    jitter: bool = True,
    exceptions: tuple[type[BaseException], ...] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[F], F]:
    """Retry a synchronous task body on transient failure, never on a terminal one.

    Applied **under** ``@celery_app.task`` so that retries happen in-process. That is the
    right place for them here: the delays are bounded by *cap*, the worker holds one message
    at a time (``worker_prefetch_multiplier=1``), and an in-process retry keeps the whole
    attempt inside a single ``acks_late`` delivery instead of multiplying queue traffic.

    Two guarantees, in this order:

    1. **A terminal outcome is returned immediately.** If the body returns a value whose
       verdict is in :func:`terminal_verdicts` — ``needs_review``, ``blocked``,
       ``already_applied``, ``skipped`` — it is handed straight back and logged. Nothing
       further is attempted, ever. This is the explicit check §15 requires.
    2. **A terminal exception is re-raised immediately.** Only :func:`is_retryable` failures
       consume an attempt.

    Args:
        attempts: Total attempts including the first. Clamped to at least one.
        base: Backoff base, passed to :func:`backoff`.
        cap: Backoff ceiling in seconds, passed to :func:`backoff`.
        jitter: Whether to jitter the delay.
        exceptions: Override the retryable set entirely. The terminal set still wins.
        sleep: Blocking sleep function. Injected so tests do not wait.

    Returns:
        A decorator preserving the wrapped function's name, signature metadata and docstring
        — Celery derives a task's default name from ``__name__``, so a decorator that lost it
        would rename the task.
    """
    budget = max(1, int(attempts))

    def decorate(func: F) -> F:
        """Wrap *func* with the retry loop."""

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            """Run the task body, retrying only transient failures."""
            log = logger.bind(task_body=func.__name__)
            last: BaseException | None = None

            for attempt in range(1, budget + 1):
                try:
                    outcome = func(*args, **kwargs)
                except Exception as exc:
                    last = exc
                    if exceptions is None:
                        retry_allowed = is_retryable(exc)
                    else:
                        retry_allowed = isinstance(exc, exceptions) and not is_terminal_exception(
                            exc
                        )
                    if not retry_allowed:
                        log.info(
                            "workers.retry_refused",
                            attempt=attempt,
                            error_type=type(exc).__name__,
                            reason="terminal",
                        )
                        raise
                    if attempt >= budget:
                        log.warning(
                            "workers.retry_exhausted",
                            attempts=budget,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                        raise
                    requested = retry_after_of(exc)
                    delay = (
                        min(float(cap), requested)
                        if requested is not None
                        else backoff(attempt, base=base, cap=cap, jitter=jitter)
                    )
                    log.warning(
                        "workers.retrying",
                        attempt=attempt,
                        of=budget,
                        delay_seconds=round(delay, 3),
                        retry_after_honored=requested is not None,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    sleep(delay)
                    continue

                # Golden rule #2, enforced rather than assumed: an escalation or a policy
                # block is an answer. Returning here — and saying so in the log — is what
                # stops a later change from folding these into the retry path.
                if is_terminal_outcome(outcome):
                    log.info(
                        "workers.terminal_outcome",
                        attempt=attempt,
                        verdict=(
                            outcome.get(_VERDICT_KEY)
                            if isinstance(outcome, Mapping)
                            else getattr(outcome, _VERDICT_ATTR, None)
                        ),
                    )
                return outcome

            # Unreachable: every iteration returns, raises, or continues, and the final
            # iteration cannot continue. Present so a future edit to the loop cannot make
            # the function silently return ``None``.
            raise NonRetryable(  # pragma: no cover
                f"{func.__name__} exhausted {budget} attempts without an outcome"
            ) from last

        return cast(F, wrapper)

    return decorate
