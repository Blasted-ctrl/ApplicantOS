"""Request middleware: correlation ids, timing, and the HTTP duration histogram.

One middleware does three things that all have the same lifetime — a single request — and
splitting them would mean three passes over the same scope:

1. **Correlation.** Every request gets an id: the client's ``X-Request-ID`` when it sent
   one (so the desktop app can correlate its own trace with the backend's), otherwise a
   fresh uuid4. The id is bound into structlog's contextvars, which propagate across
   ``await`` boundaries, so every log line emitted anywhere under this request carries it
   without a single call site passing it along. It is echoed back in the response header
   and stashed on ``request.state`` so :mod:`app.api.errors` can put it in the error body —
   which is what turns a user's screenshot of an error toast into a log query.
2. **Timing.** Wall-clock duration, measured with :func:`time.perf_counter`.
3. **The metric.** ``applicantos_http_request_duration_seconds{route,method,status}``.

**Contextvars are cleared in a ``finally``.** ASGI servers reuse tasks and threads. A
correlation id left bound after a response is written leaks into the *next* request's logs,
which is worse than having no id at all: it attributes one user's failure to another's
request. Clearing unconditionally is the only version of this that is correct.

**The ``route`` label is the matched template, never the raw path.** ``/api/v1/applications/
{id}`` is one series; the raw path is one series per application, which is an unbounded
cardinality explosion that kills a Prometheus server. When no route matched (a 404), the
path is normalised by replacing anything that looks like an identifier — so a scan for
``/api/v1/<uuid>`` produces one series and not a million.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config.logging import bind_context, clear_context
from app.observability.metrics import observe_http_request

__all__ = [
    "CORRELATION_ID_HEADER",
    "REQUEST_ID_HEADER",
    "CorrelationIdMiddleware",
    "ObservabilityMiddleware",
    "correlation_id_of",
    "normalize_path",
]

logger = structlog.get_logger(__name__)

#: Request header carrying a client-supplied correlation id, and the response header the
#: server's id is echoed in. ``X-Request-ID`` is the de-facto standard and is what proxies
#: and the Tauri shell already understand.
REQUEST_ID_HEADER: Final[str] = "X-Request-ID"

#: Second response header carrying the same value, under the name the error body uses. Both
#: are emitted because clients in the wild look for one or the other.
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-ID"

#: Attribute name the id is stashed under on ``request.state``.
STATE_ATTRIBUTE: Final[str] = "correlation_id"

#: Longest client-supplied id accepted. A header is attacker-controlled and ends up in
#: every log line and in the ``log_entries.correlation_id`` column, which is bounded.
MAX_CORRELATION_ID_LENGTH: Final[int] = 128

#: Characters permitted in a client-supplied id. Anything else and the header is discarded
#: in favour of a generated uuid4, because a correlation id containing a newline is a log
#: injection.
_SAFE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:@+/=-]+$")

#: Path segments replaced by ``{id}`` when no route template is available. Matches UUIDs,
#: bare integers, and long hex digests — the three shapes an identifier takes in this API.
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\d+"
    r"|[0-9a-fA-F]{16,})$"
)

#: Route label used when the request matched nothing and the path is empty.
_ROOT_ROUTE_LABEL: Final[str] = "/"

#: Status label recorded when the downstream application raised instead of responding.
_ERROR_STATUS_LABEL: Final[str] = "500"


def normalize_path(path: str) -> str:
    """Reduce a raw request path to a bounded-cardinality metric label.

    Only used when Starlette could not attach a matched route — chiefly 404s, which are
    exactly the requests an attacker or a broken client generates in volume.

    Args:
        path: The raw request path.

    Returns:
        The path with every identifier-shaped segment replaced by ``{id}``.
    """
    if not path or path == _ROOT_ROUTE_LABEL:
        return _ROOT_ROUTE_LABEL
    segments = [
        "{id}" if _IDENTIFIER_PATTERN.match(segment) else segment for segment in path.split("/")
    ]
    return "/".join(segments) or _ROOT_ROUTE_LABEL


def _route_label(scope: Any, fallback_path: str) -> str:
    """Return the metric ``route`` label for a finished request.

    Args:
        scope: The ASGI scope, after routing has run.
        fallback_path: The raw path, used when no route matched.

    Returns:
        The matched route template, or a normalised path.
    """
    route = scope.get("route") if isinstance(scope, dict) else None
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return normalize_path(fallback_path)


def _sanitize_client_id(raw: str | None) -> str | None:
    """Validate a client-supplied correlation id.

    Args:
        raw: The raw header value, or ``None``.

    Returns:
        The id when it is short and character-safe, otherwise ``None`` so the caller
        generates one instead.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_CORRELATION_ID_LENGTH:
        return None
    if not _SAFE_ID_PATTERN.match(candidate):
        return None
    return candidate


def correlation_id_of(request: Any) -> str | None:
    """Return the correlation id bound to *request*, if the middleware has run.

    Used by :mod:`app.api.errors` to stamp :class:`~app.schemas.common.ErrorResponse`.

    Args:
        request: The Starlette/FastAPI request.

    Returns:
        The id, or ``None`` when the middleware is not installed (a bare test client) or
        the failure happened before it ran.
    """
    state = getattr(request, "state", None)
    identifier = getattr(state, STATE_ATTRIBUTE, None)
    if isinstance(identifier, str) and identifier:
        return identifier
    headers = getattr(request, "headers", None)
    if headers is not None:
        return _sanitize_client_id(headers.get(REQUEST_ID_HEADER))
    return None


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id, time the request, and emit the duration histogram.

    Installed by :func:`app.main.create_app` as the outermost application middleware, so
    that the id is bound before routing and still bound while an exception handler renders
    an error body.

    Args:
        app: The next ASGI application in the chain.
        header_name: Request header to read a client-supplied id from.
    """

    def __init__(self, app: ASGIApp, *, header_name: str = REQUEST_ID_HEADER) -> None:
        """Store the chain and the header name."""
        super().__init__(app)
        self._header_name = header_name

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """Run one request with correlation context, timing and metrics.

        Args:
            request: The incoming request.
            call_next: Callable running the rest of the stack.

        Returns:
            The downstream response, with the correlation id added to its headers.

        Raises:
            Exception: Whatever the downstream stack raised, re-raised unchanged after the
                failure has been timed and recorded.
        """
        correlation_id = (
            _sanitize_client_id(request.headers.get(self._header_name)) or uuid.uuid4().hex
        )
        setattr(request.state, STATE_ATTRIBUTE, correlation_id)

        # Cleared before binding as well as after: an ASGI server that reuses a task can
        # hand us context left behind by a request whose teardown was skipped.
        clear_context()
        bind_context(correlation_id=correlation_id)

        started = time.perf_counter()
        status_label = _ERROR_STATUS_LABEL
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - started
            self._record(request, status_label, elapsed)
            logger.warning(
                "http.request_failed",
                http_method=request.method,
                path=normalize_path(request.url.path),
                duration_ms=round(elapsed * 1000, 2),
            )
            raise
        else:
            elapsed = time.perf_counter() - started
            status_label = str(response.status_code)
            self._record(request, status_label, elapsed)
            response.headers[REQUEST_ID_HEADER] = correlation_id
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            logger.debug(
                "http.request",
                http_method=request.method,
                path=normalize_path(request.url.path),
                status=response.status_code,
                duration_ms=round(elapsed * 1000, 2),
            )
            return response
        finally:
            # Unconditional: a correlation id surviving into the next request on a reused
            # worker task would attribute one user's failure to another's request.
            clear_context()

    @staticmethod
    def _record(request: Request, status_label: str, elapsed: float) -> None:
        """Emit the duration histogram for one finished request.

        Args:
            request: The request, whose scope now carries the matched route.
            status_label: The response status as a string.
            elapsed: Wall-clock seconds.
        """
        observe_http_request(
            route=_route_label(request.scope, request.url.path),
            method=request.method,
            status=status_label,
            seconds=elapsed,
        )


#: Historical alias. The middleware started life as a correlation-id-only component and
#: grew the timing responsibility; both names refer to the same class so an existing
#: ``add_middleware(CorrelationIdMiddleware)`` keeps working.
CorrelationIdMiddleware = ObservabilityMiddleware
