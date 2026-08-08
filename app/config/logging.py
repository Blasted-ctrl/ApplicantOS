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
   dict (through nested dicts *and* sequences) and runs two passes over it: a key pass that
   replaces the value of any key whose name contains a sensitive token with
   :data:`REDACTED`, and a *value* pass (:func:`scrub_text`) that rewrites secrets and
   personal data found **inside** string values. This is golden rule #4 in
   ``docs/CONTRACTS.md`` §18: no secrets in logs, ever.
7. Exception formatting, then either a JSON renderer (``settings.log_json``) or structlog's
   colourised console renderer for local development.

Records emitted through the plain :mod:`logging` API by third-party libraries are routed
through the *same* processor chain via :class:`structlog.stdlib.ProcessorFormatter`, so a
stray ``requests`` warning is redacted and rendered identically to a first-party event.

Why two passes
--------------

The key pass is a cheap, total defence that cannot be forgotten at a call site. Matching is
by substring, so it over-redacts slightly (``token`` also scrubs ``oauth_token`` and
``refresh_token``) — a false positive costs a debugging round-trip while a false negative
leaks a credential. The patterns are nonetheless kept narrow enough that no
contract-mandated field name collides with one; see :data:`SENSITIVE_KEY_PATTERNS`.

A key pass alone is not sufficient, and the counter-example is ordinary: an exception from
a mailbox adapter logged as ``error=str(exc)``. ``error`` is not a sensitive *name*, so the
key pass waves it through, yet the string can carry the user's email address, a URL with
``access_token=`` in its query, or an API key echoed back by a provider. :func:`scrub_text`
is therefore applied to every string value anywhere in the event dict — including strings
nested inside lists, tuples and sets — after the key pass has run. It masks email addresses,
credential-shaped tokens and secret-bearing query parameters, and truncates opaque
base64-ish blobs. It runs on every log line, so it short-circuits on any string that is
short and contains neither ``@`` nor ``=``, and every pattern is compiled once at import.

Because redaction walks only the event dict, the exception renderer runs with frame-locals
capture **disabled** — otherwise every local variable in the failing frame would be
serialised after the scrubber had already finished. See :func:`_build_renderer_chain`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, Final

import structlog

from app.config.settings import Settings
from app.config.settings import settings as _default_settings

__all__ = [
    "BLOB_HEAD_CHARS",
    "EMAIL_MASK",
    "LOG_SAFE_MAIL_DOMAINS",
    "MAX_LOGGED_BLOB_CHARS",
    "MAX_REDACTION_DEPTH",
    "NOISY_LOGGER_LEVELS",
    "REDACTED",
    "SENSITIVE_KEY_PATTERNS",
    "VALUE_SCAN_MIN_CHARS",
    "bind_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "redact_secrets",
    "scrub_text",
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

#: Written in place of an email address whose domain is not operationally useful.
EMAIL_MASK: Final[str] = "***@***"

#: Mail domains whose *domain half* survives :func:`scrub_text`, subdomains included.
#:
#: An address at one of these is an ATS relay or a job board — ``no-reply@greenhouse.io``,
#: ``jobs-noreply@linkedin.com`` — so the domain identifies *which system* mailed the user
#: and is exactly what an operator needs to debug a status-sync failure. The local part is
#: still masked, because that is the half that can identify a person.
#:
#: Deliberately a private copy of the value list rather than an import of
#: ``app.tracking.base.ATS_RELAY_DOMAINS``: ``app.config`` is the bottom layer of the
#: application and every other package imports it, so reaching upward into ``app.tracking``
#: here would make the logging processor — the one component that must always import — a
#: hostage of a package that imports settings itself.
LOG_SAFE_MAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "greenhouse.io",
        "greenhouse-mail.io",
        "lever.co",
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

#: Below this length, a string containing neither ``@`` nor ``=`` is passed straight
#: through by :func:`scrub_text`. Nothing the value patterns look for is shorter: the
#: stubbiest credential shapes (``AKIA`` + 12, ``sk-`` + 6) are longer than this, and an
#: address always carries an ``@``. This is the fast path for the overwhelming majority of
#: logged values — event names, statuses, provider slugs, counts rendered as text.
VALUE_SCAN_MIN_CHARS: Final[int] = 12

#: Length at which an unbroken base64-ish run is treated as an opaque blob and truncated.
#: Chosen above any real word, slug or identifier that carries meaning in a log line, and
#: below the length of a token or a serialised key.
MAX_LOGGED_BLOB_CHARS: Final[int] = 64

#: Characters kept from the head of a truncated blob — enough to correlate two log lines
#: about the same opaque id (a Graph message id, for instance) without carrying the rest.
BLOB_HEAD_CHARS: Final[int] = 8

#: An email address. The domain is captured so :func:`_mask_email` can decide whether to
#: keep it.
_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+'-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
)

#: Credential shapes that identify themselves. Every branch is anchored on a literal prefix
#: that no prose contains, so this cannot fire on an ordinary sentence: an ``Authorization``
#: header value, an Anthropic/OpenAI/Stripe key, a GitHub token, a Slack token, an AWS
#: access key id, a Google API key, or a JWT.
_CREDENTIAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:sk|pk|rk)[_-][A-Za-z0-9._-]{6,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{8,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{8,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{8,}"
    r"|\bAKIA[0-9A-Z]{12,}"
    r"|\bAIza[A-Za-z0-9_-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
)

#: ``name=value`` where *name* names a secret. Covers a URL query string
#: (``?access_token=…``), a form body, and a connection string echoed into an exception
#: message. The name is kept and only the value is replaced, because knowing *which*
#: parameter was present is diagnostic and the name itself is not a secret. Longer
#: alternatives precede their own suffixes so ``access_token`` never matches as ``token``.
_SECRET_PARAM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|client_secret|private_key|api[_-]?key"
    r"|apikey|authorization|password|passwd|secret|token|signature)\s*=\s*[^&\s\"'<>]+"
)

#: An unbroken run of base64/base64url characters long enough to be a payload rather than
#: an identifier. Matched last, so anything the earlier patterns recognised is already gone.
_BLOB_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z0-9+/_-]{{{MAX_LOGGED_BLOB_CHARS},}}={{0,2}}"
)

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


def _mask_email(match: re.Match[str]) -> str:
    """Replace one email address, keeping the domain only when it is operationally useful.

    Args:
        match: A match of :data:`_EMAIL_PATTERN`, whose first group is the domain.

    Returns:
        ``***@***`` for an ordinary address, or ``***@<domain>`` when the domain is an ATS
        relay or job board listed in :data:`LOG_SAFE_MAIL_DOMAINS` (subdomains included).
    """
    domain = match.group(1).lower().rstrip(".")
    for safe in LOG_SAFE_MAIL_DOMAINS:
        if domain == safe or domain.endswith(f".{safe}"):
            return f"***@{domain}"
    return EMAIL_MASK


def _mask_parameter(match: re.Match[str]) -> str:
    """Replace the value of one secret-bearing ``name=value`` pair, keeping the name.

    Args:
        match: A match of :data:`_SECRET_PARAM_PATTERN`, whose first group is the name.

    Returns:
        ``<name>=***redacted***``.
    """
    return f"{match.group(1)}={REDACTED}"


def _truncate_blob(match: re.Match[str]) -> str:
    """Shorten one long opaque run, or leave it alone when it is merely a long word.

    A run of 64+ characters that mixes case *and* digits is a payload — a serialised key, a
    signature, an encoded body. A run that does not is a hyphenated slug, a file stem or a
    path segment, and truncating it would cost readability for no privacy gain.

    Args:
        match: A match of :data:`_BLOB_PATTERN`.

    Returns:
        The first :data:`BLOB_HEAD_CHARS` characters plus a length marker, or the original
        run when it does not look encoded.
    """
    blob = match.group(0)
    has_digit = has_upper = has_lower = False
    for character in blob:
        has_digit = has_digit or character.isdigit()
        has_upper = has_upper or character.isupper()
        has_lower = has_lower or character.islower()
    if not (has_digit and has_upper and has_lower):
        return blob
    return f"{blob[:BLOB_HEAD_CHARS]}...[{len(blob)} chars truncated]"


def scrub_text(value: str) -> str:
    """Remove credentials and personal data from *inside* one logged string.

    The companion to the key-based pass, and the reason a value logged under a harmless key
    such as ``error`` is safe. Four rewrites, applied in this order because each one narrows
    what the next can see:

    1. **Credential shapes** (:data:`_CREDENTIAL_PATTERN`) — ``Bearer …``, ``sk-…``,
       ``ghp_…``, ``AKIA…``, ``AIza…``, a JWT — become :data:`REDACTED` outright.
    2. **Secret-bearing parameters** (:data:`_SECRET_PARAM_PATTERN`) — ``access_token=``,
       ``refresh_token=``, ``client_secret=``, … — keep their name and lose their value.
    3. **Email addresses** — masked by :func:`_mask_email`, which keeps an ATS relay domain
       because it says which system mailed the user and identifies nobody.
    4. **Opaque blobs** — truncated by :func:`_truncate_blob` with a length marker.

    Every pass is gated on a condition its own pattern makes mandatory — an ``@`` for an
    address, an ``=`` for a parameter, :data:`MAX_LOGGED_BLOB_CHARS` characters for a blob —
    so the common case (an event name, a status, a provider slug) pays one substring scan
    per pass instead of one regex traversal. That matters: this runs on every value of every
    log line, and the address pattern alone costs three times what the gate does.

    Args:
        value: One logged string, of any length.

    Returns:
        The scrubbed string, or *value* itself when nothing matched. Never raises.
    """
    has_address = "@" in value
    has_assignment = "=" in value
    if not has_address and not has_assignment and len(value) < VALUE_SCAN_MIN_CHARS:
        return value

    scrubbed = _CREDENTIAL_PATTERN.sub(REDACTED, value)
    if has_assignment:
        scrubbed = _SECRET_PARAM_PATTERN.sub(_mask_parameter, scrubbed)
    if has_address:
        scrubbed = _EMAIL_PATTERN.sub(_mask_email, scrubbed)
    if len(scrubbed) >= MAX_LOGGED_BLOB_CHARS:
        scrubbed = _BLOB_PATTERN.sub(_truncate_blob, scrubbed)
    return scrubbed


def _redact_value(value: Any, depth: int, seen: set[int]) -> Any:
    """Recursively redact *value*, descending through mappings and sequences.

    Args:
        value: The value to scrub. Strings go through :func:`scrub_text`; mappings and
            non-string sequences are rebuilt; every other type is returned untouched.
        depth: Current recursion depth, compared against :data:`MAX_REDACTION_DEPTH`.
        seen: Identity set of containers already visited on this path, which makes the
            walk safe against self-referential structures.

    Returns:
        A scrubbed copy of *value*, or *value* itself when nothing can be nested inside it.
    """
    # Before the depth guard: scrubbing a string neither recurses nor allocates a container,
    # so a string that happens to sit twelve levels down is still worth cleaning.
    if isinstance(value, str):
        return scrub_text(value)

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
    tuples and sets — and applies two defences at every level:

    * **By key.** The value of every key matching :data:`SENSITIVE_KEY_PATTERNS` is replaced
      with :data:`REDACTED` outright, whatever it holds.
    * **By value.** Every surviving string, at any depth and inside any sequence, goes
      through :func:`scrub_text`, which catches the secrets a key name cannot predict —
      an exception message logged under ``error`` carrying an address, an
      ``access_token=`` URL, or an API key.

    The original structures are never mutated; scrubbed copies are substituted, so the
    caller's objects are untouched.

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
