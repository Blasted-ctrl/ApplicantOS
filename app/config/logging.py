"""Structured logging configuration — structlog wired to the standard library.

Every log line in ApplicantOS is a structured event: a short dotted ``event`` name plus
typed key/value context. This module owns the whole pipeline and is called exactly once,
during application/worker startup, by :func:`configure_logging`.

The pipeline, in order:

1. ``filter_by_level`` — drop records below ``settings.log_level`` as early as possible.
2. ``merge_contextvars`` — splice in the ambient request/session context bound via
   :func:`bind_context` (``correlation_id``, ``user_id``, ``session_id``, ``posting_id``,
   ``application_id``, ``provider``). Context propagates across ``await`` boundaries.
3. ``add_log_level`` / ``add_logger_name`` — normalise metadata.
4. ``TimeStamper(fmt="iso", utc=True)`` — RFC 3339 timestamps in UTC.
5. ``StackInfoRenderer`` / ``UnicodeDecoder`` — normalise tracebacks and byte payloads.
6. :func:`redact_secrets` — **the security-critical step.** Recursively walks the event
   dict (through nested dicts *and* sequences) and replaces the value of any key whose
   name contains a sensitive token with :data:`REDACTED`. This is golden rule #4 in
   ``docs/CONTRACTS.md`` §18: no secrets in logs, ever.
7. Exception formatting, then either a JSON renderer (``settings.log_json``) or structlog's
   colourised console renderer for local development.

Records emitted through the plain :mod:`logging` API by third-party libraries are routed
through the *same* processor chain via :class:`structlog.stdlib.ProcessorFormatter`, so a
stray ``requests`` warning is redacted and rendered identically to a first-party event.

Redaction is applied to keys, not values: it is a cheap, total defence that cannot be
forgotten at a call site. Matching is by substring, so it over-redacts slightly (``token``
also scrubs ``oauth_token`` and ``refresh_token``) — a false positive costs a debugging
round-trip while a false negative leaks a credential. The patterns are nonetheless kept
narrow enough that no contract-mandated field name collides with one; see
:data:`SENSITIVE_KEY_PATTERNS`.

Because redaction walks only the event dict, the exception renderer runs with frame-locals
capture **disabled** — otherwise every local variable in the failing frame would be
serialised after the scrubber had already finished. See :func:`_build_renderer_chain`.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Final

import structlog

from app.config.settings import Settings
from app.config.settings import settings as _default_settings

__all__ = [
    "MAX_REDACTION_DEPTH",
    "NOISY_LOGGER_LEVELS",
    "REDACTED",
    "SENSITIVE_KEY_PATTERNS",
    "bind_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "redact_secrets",
]

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Replacement written in place of any value whose key looks sensitive.
REDACTED: Final[str] = "***redacted***"

#: Case-insensitive substrings that mark a key as sensitive. This is the scrub vocabulary
#: frozen by ``docs/CONTRACTS.md`` §16 (password / token / api_key / secret / authorization
#: / cookie / ssn / dob) plus a few spelling variants of those same secrets.
#:
#: Two omissions are deliberate, because substring matching makes an over-broad pattern
#: destructive rather than merely noisy:
#:
#: * A bare ``auth`` is not in §16 and adds nothing: ``authorization`` already covers the
#:   header, ``oauth_token`` is caught by ``token``, and ``x-auth`` is listed explicitly.
#:   All it added was collateral damage on unrelated keys such as ``authored_by``.
#: * ``session_id`` is a **bound context key** in §16, not a redaction target: scrubbing it
#:   makes a ``RunSession`` untraceable through its own logs and feeds ``***redacted***``
#:   into the ``log_entries.session_id`` GUID column (§4). Web/auth session credentials are
#:   covered by ``token`` and ``cookie``.
#:
#: Known residual over-redaction: ``work_authorization`` (§4, and a key emitted by
#: ``UserProfile.to_dto()``) contains ``authorization``, which §16 mandates, so it is still
#: scrubbed. Resolving that needs a contract decision — see ``docs/OPEN_QUESTIONS.md`` §2.
SENSITIVE_KEY_PATTERNS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "api_key",
        "apikey",
        "secret",
        "authorization",
        "x-auth",
        "cookie",
        "ssn",
        "dob",
        "credit_card",
    }
)

#: Hard recursion ceiling for :func:`redact_secrets`. Log payloads are never legitimately
#: this deep; the limit protects against pathological or self-referential structures.
MAX_REDACTION_DEPTH: Final[int] = 12

#: Fallback level used when ``settings.log_level`` is not a recognised level name.
DEFAULT_LOG_LEVEL: Final[int] = logging.INFO

#: Third-party loggers that are far too chatty at INFO. Raised to these levels unless the
#: application is running in debug mode, where full verbosity is the point.
NOISY_LOGGER_LEVELS: Final[dict[str, int]] = {
    "asyncio": logging.WARNING,
    "botocore": logging.WARNING,
    "urllib3": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "hpack": logging.WARNING,
    "playwright": logging.INFO,
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "celery": logging.INFO,
    "kombu": logging.WARNING,
    "watchfiles": logging.WARNING,
}


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------


def _is_sensitive_key(key: object) -> bool:
    """Return whether *key* names a value that must never reach a log sink.

    Args:
        key: A mapping key of any type; non-string keys are never sensitive.

    Returns:
        ``True`` when the lowercased key contains any entry of
        :data:`SENSITIVE_KEY_PATTERNS` as a substring.
    """
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(pattern in lowered for pattern in SENSITIVE_KEY_PATTERNS)


def _redact_value(value: Any, depth: int, seen: set[int]) -> Any:
    """Recursively redact *value*, descending through mappings and sequences.

    Args:
        value: The value to scrub. Mappings and non-string sequences are rebuilt; every
            other type is returned untouched.
        depth: Current recursion depth, compared against :data:`MAX_REDACTION_DEPTH`.
        seen: Identity set of containers already visited on this path, which makes the
            walk safe against self-referential structures.

    Returns:
        A scrubbed copy of *value*, or *value* itself when nothing can be nested inside it.
    """
    if depth >= MAX_REDACTION_DEPTH:
        return value

    if isinstance(value, MutableMapping):
        identity = id(value)
        if identity in seen:
            return value
        seen.add(identity)
        try:
            return {
                key: REDACTED if _is_sensitive_key(key) else _redact_value(item, depth + 1, seen)
                for key, item in value.items()
            }
        finally:
            seen.discard(identity)

    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return value
        seen.add(identity)
        try:
            scrubbed = [_redact_value(item, depth + 1, seen) for item in value]
        finally:
            seen.discard(identity)
        if isinstance(value, tuple):
            return tuple(scrubbed)
        if isinstance(value, set):
            return set(scrubbed)
        if isinstance(value, frozenset):
            return frozenset(scrubbed)
        return scrubbed

    return value


def redact_secrets(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor that strips credentials and PII from an event.

    Walks *event_dict* recursively — through nested dictionaries **and** through lists,
    tuples and sets — replacing the value of every key that matches
    :data:`SENSITIVE_KEY_PATTERNS` with :data:`REDACTED`. The original structures are never
    mutated; scrubbed copies are substituted, so the caller's objects are untouched.

    Args:
        _logger: The bound logger (unused; part of the structlog processor signature).
        _method_name: The log method invoked (unused; part of the processor signature).
        event_dict: The mutable event dictionary to scrub.

    Returns:
        The same ``event_dict`` instance with sensitive entries replaced in place.
    """
    seen: set[int] = {id(event_dict)}
    for key in list(event_dict.keys()):
        if _is_sensitive_key(key):
            event_dict[key] = REDACTED
            continue
        event_dict[key] = _redact_value(event_dict[key], 1, seen)
    return event_dict


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def _resolve_level(level_name: str) -> int:
    """Translate a textual log level into its :mod:`logging` integer.

    Args:
        level_name: A level name such as ``"INFO"`` or ``"debug"``.

    Returns:
        The matching integer level, or :data:`DEFAULT_LOG_LEVEL` when unrecognised.
    """
    candidate = logging.getLevelName(level_name.strip().upper())
    return candidate if isinstance(candidate, int) else DEFAULT_LOG_LEVEL


def _json_serializer(payload: Any, **kwargs: Any) -> str:
    """Serialise an event dictionary to JSON without ever raising on exotic types.

    Args:
        payload: The rendered event dictionary.
        **kwargs: Extra keyword arguments passed through by structlog's JSON renderer.

    Returns:
        A single-line JSON document. Values that are not JSON-native are coerced with
        ``str`` rather than aborting the log call.
    """
    kwargs.pop("default", None)
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(payload, default=str, **kwargs)


def _supports_color() -> bool:
    """Return whether the console renderer should emit ANSI colour codes."""
    stream = sys.stdout
    return hasattr(stream, "isatty") and bool(stream.isatty())


def _build_renderer_chain(log_json: bool) -> list[Any]:
    """Build the tail of the processor chain: exception formatting plus a renderer.

    Args:
        log_json: ``True`` for machine-readable JSON output, ``False`` for the
            human-friendly development console renderer.

    Returns:
        The ordered list of processors that turn an event dict into a string.
    """
    if log_json:
        return [
            # ``show_locals=False`` is security-critical, not a formatting preference.
            # This renderer runs *after* :func:`redact_secrets`, which only walks event-dict
            # keys — at that point the traceback is still an ``exc_info`` tuple, so nothing
            # scrubs it. With locals capture on (structlog's default) every frame's
            # variables are expanded into ``exception[].frames[].locals``, which is how an
            # API key, cookie, or storage-state path held in a local escapes redaction
            # entirely. Golden rule #4, ``docs/CONTRACTS.md`` §18.4.
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            structlog.processors.JSONRenderer(serializer=_json_serializer),
        ]
    return [
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(
            colors=_supports_color(),
            exception_formatter=structlog.dev.plain_traceback,
        ),
    ]


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog and the standard library logging module.

    Call once at process startup — from the FastAPI lifespan, the Celery worker bootstrap,
    or a CLI entry point — before any log call is made. Calling it again reconfigures
    cleanly: existing root handlers are removed first, so repeated calls (common in tests)
    never duplicate output.

    Args:
        settings: Configuration to read ``log_level``, ``log_json`` and ``debug`` from.
            Defaults to the process-wide settings singleton.
    """
    active = settings if settings is not None else _default_settings
    level = _resolve_level(active.log_level)

    # Shared by first-party structlog events and foreign ``logging`` records alike, so a
    # third-party warning is timestamped, context-enriched and redacted identically.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        redact_secrets,
    ]

    structlog.configure(
        processors=[
            # Only valid for first-party events: it needs a real stdlib logger, which
            # ``ProcessorFormatter`` does not supply when re-processing foreign records.
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=not active.debug,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *_build_renderer_chain(active.log_json),
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    if not active.debug:
        for logger_name, logger_level in NOISY_LOGGER_LEVELS.items():
            logging.getLogger(logger_name).setLevel(logger_level)

    structlog.get_logger(__name__).debug(
        "logging.configured",
        level=logging.getLevelName(level),
        json=active.log_json,
        environment=active.environment,
    )


# --------------------------------------------------------------------------------------
# Context helpers
# --------------------------------------------------------------------------------------


def bind_context(**kwargs: Any) -> None:
    """Bind ambient key/value context onto every subsequent log line in this task.

    Backed by :mod:`contextvars`, so the context follows ``await`` boundaries and is
    isolated between concurrent requests, Celery tasks, and pipeline runs. The canonical
    keys are ``correlation_id``, ``user_id``, ``session_id``, ``posting_id``,
    ``application_id`` and ``provider``.

    Args:
        **kwargs: Key/value pairs to merge into the ambient logging context.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Drop all ambient logging context bound via :func:`bind_context`.

    Must be called when a unit of work finishes — request teardown, task completion — so
    that a pooled worker thread or reused event loop does not leak one job's identifiers
    into the next job's logs.
    """
    structlog.contextvars.clear_contextvars()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    Args:
        name: Logger name, conventionally the calling module's ``__name__``. When omitted
            structlog infers a name from the caller's module.

    Returns:
        A :class:`structlog.stdlib.BoundLogger` ready for structured event calls.
    """
    if name is None:
        return structlog.get_logger()
    return structlog.get_logger(name)
