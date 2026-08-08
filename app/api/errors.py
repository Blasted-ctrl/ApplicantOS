"""Exception handlers — one error shape for the whole API.

Every failing request returns :class:`~app.schemas.common.ErrorResponse`: a stable machine
code, a human-readable detail, and the request's correlation id. Clients branch on ``error``;
they never parse ``detail``.

Two rules govern what may appear in a response body, and both fail closed:

**No traceback ever reaches a client.** Not in production, not in debug. A traceback names
internal modules, file paths and, through frame locals, values that
:func:`~app.config.logging.redact_secrets` has no opportunity to scrub — the redaction
processor works on the *log* event dict, not on an HTTP body. The traceback goes to the log,
where it belongs; the client gets the correlation id that finds it.

**No settings value reaches a client.** ``str(exc)`` on a configuration error routinely
contains a connection URL or a key prefix. So the generic handler emits a fixed sentence,
and the handlers that do quote a message quote only messages the raising module authored for
an operator. :class:`~app.documents.renderer.DocumentRenderError` is the pointed example: its
``__str__`` appends the LaTeX engine's stderr, which carries absolute filesystem paths, so
this module reads ``exc.message`` instead.

Validation failures get the same treatment. Pydantic's error list includes an ``input`` key
echoing the offending value — which, on ``PUT /settings``, is an API key. :func:`_field_errors`
keeps ``loc``, ``msg`` and ``type`` and drops everything else.

Handler selection walks the exception's MRO, so a specific handler always beats a general
one: :class:`~app.services.application_service.InvalidTransition` is a :class:`ValueError`
but returns 409, and :class:`~app.documents.renderer.DocumentRenderError` is a
:class:`~app.plugins.base.PluginError` but returns 500 rather than 404.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.documents.renderer import DocumentRenderError
from app.jobs.base import (
    PostingUnavailableError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    UnsupportedFlowError,
)
from app.observability.middleware import correlation_id_of
from app.plugins.base import PluginDisabled, PluginLoadError, PluginNotFound
from app.schemas.common import ErrorResponse
from app.services.application_service import InvalidTransition

__all__ = [
    "ERROR_CONFLICT",
    "ERROR_INTERNAL",
    "ERROR_INVALID_REQUEST",
    "ERROR_NOT_FOUND",
    "ERROR_VALIDATION",
    "INTERNAL_ERROR_DETAIL",
    "error_response",
    "install_exception_handlers",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Stable machine codes — part of the published contract; do not rename.
# ======================================================================================

ERROR_NOT_FOUND: Final[str] = "not_found"
ERROR_INVALID_REQUEST: Final[str] = "invalid_request"
ERROR_VALIDATION: Final[str] = "validation_error"
ERROR_CONFLICT: Final[str] = "conflict"
ERROR_INTERNAL: Final[str] = "internal_error"
ERROR_PROVIDER: Final[str] = "provider_error"
ERROR_PROVIDER_AUTH: Final[str] = "provider_auth_required"
ERROR_RATE_LIMITED: Final[str] = "rate_limited"
ERROR_POSTING_UNAVAILABLE: Final[str] = "posting_unavailable"
ERROR_UNSUPPORTED_FLOW: Final[str] = "unsupported_flow"
ERROR_RENDER_FAILED: Final[str] = "document_render_failed"
ERROR_PLUGIN_NOT_FOUND: Final[str] = "plugin_not_found"
ERROR_PLUGIN_DISABLED: Final[str] = "plugin_disabled"
ERROR_PLUGIN_FAILED: Final[str] = "plugin_failed"

#: The only thing a client is told about an unhandled failure. Fixed text: anything derived
#: from the exception risks quoting a connection string or a credential.
INTERNAL_ERROR_DETAIL: Final[str] = (
    "The server failed to complete this request. The correlation id identifies the log "
    "entries describing what happened."
)

#: Detail for a provider that does not support the requested operation *by design* —
#: LinkedIn and Workday submission (golden rule #10). Phrased so the desktop app can show it
#: verbatim without implying a transient fault.
_UNSUPPORTED_FLOW_DETAIL: Final[str] = (
    "This provider does not support automated submission, so the application has to be "
    "completed by hand."
)

#: Keys kept from a pydantic validation error. ``input`` and ``ctx`` are dropped: they echo
#: the submitted value, which on PUT /settings is a credential.
_SAFE_VALIDATION_KEYS: Final[tuple[str, ...]] = ("loc", "msg", "type")

#: Response header carrying a provider-requested cooldown.
_RETRY_AFTER_HEADER: Final[str] = "Retry-After"


# ======================================================================================
# Rendering
# ======================================================================================


def error_response(
    request: Request,
    *,
    status_code: int,
    error: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Render one :class:`~app.schemas.common.ErrorResponse` as a JSON response.

    Args:
        request: The failing request, used to recover the correlation id.
        status_code: HTTP status to return.
        error: Stable machine code clients branch on.
        detail: Human-readable explanation, already known to be safe to display.
        headers: Extra response headers (``Retry-After``, for instance).
        extra: Additional top-level body keys, for the validation handler's field list.

    Returns:
        The JSON response.
    """
    body = ErrorResponse(
        error=error,
        detail=detail,
        correlation_id=correlation_id_of(request),
    ).model_dump(mode="json")
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _field_errors(raw: Sequence[Any]) -> list[dict[str, Any]]:
    """Reduce pydantic's error list to the parts that are safe to return.

    Args:
        raw: The list produced by ``RequestValidationError.errors()``.

    Returns:
        One dictionary per error carrying only ``loc``, ``msg`` and ``type``. The ``input``
        and ``ctx`` keys are dropped because they echo the submitted value.
    """
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        for key in _SAFE_VALIDATION_KEYS:
            if key not in item:
                continue
            value = item[key]
            entry[key] = list(value) if key == "loc" and isinstance(value, (list, tuple)) else value
        if entry:
            cleaned.append(entry)
    return cleaned


# ======================================================================================
# Handlers
# ======================================================================================


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    """Return 422 for a request whose body or query parameters failed validation.

    Args:
        request: The failing request.
        exc: The :class:`~fastapi.exceptions.RequestValidationError`.

    Returns:
        A 422 response listing the offending fields, without echoing their values.
    """
    errors = _field_errors(exc.errors()) if isinstance(exc, RequestValidationError) else []
    logger.info("api.validation_failed", path=request.url.path, error_count=len(errors))
    return error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error=ERROR_VALIDATION,
        detail="The request did not match the expected shape.",
        extra={"errors": errors},
    )


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    """Return an explicitly raised :class:`~fastapi.HTTPException` in the common shape.

    Args:
        request: The failing request.
        exc: The raised exception.

    Returns:
        The response, carrying whatever headers the raiser attached (``WWW-Authenticate``,
        ``Allow``).
    """
    if not isinstance(exc, (HTTPException, StarletteHTTPException)):  # pragma: no cover
        return await handle_unexpected_error(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else None
    code = {
        status.HTTP_404_NOT_FOUND: ERROR_NOT_FOUND,
        status.HTTP_409_CONFLICT: ERROR_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_ENTITY: ERROR_VALIDATION,
    }.get(exc.status_code, ERROR_INVALID_REQUEST if exc.status_code < 500 else ERROR_INTERNAL)
    headers = dict(getattr(exc, "headers", None) or {})
    return error_response(
        request,
        status_code=exc.status_code,
        error=code,
        detail=detail,
        headers=headers or None,
    )


async def handle_invalid_transition(request: Request, exc: Exception) -> Response:
    """Return 409 for a status change the application state machine forbids.

    Args:
        request: The failing request.
        exc: The :class:`~app.services.application_service.InvalidTransition`.

    Returns:
        A 409 response quoting the rejected move.
    """
    logger.info("api.invalid_transition", path=request.url.path, detail=str(exc))
    return error_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        error=ERROR_CONFLICT,
        detail=str(exc),
    )


async def handle_lookup_error(request: Request, exc: Exception) -> Response:
    """Return 404 for a service that could not find the id it was given.

    Args:
        request: The failing request.
        exc: The :class:`LookupError`.

    Returns:
        A 404 response quoting the service's message.
    """
    return error_response(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        error=ERROR_NOT_FOUND,
        detail=str(exc) or "The requested resource does not exist.",
    )


async def handle_value_error(request: Request, exc: Exception) -> Response:
    """Return 400 for input a service rejected as malformed or unsupported.

    Args:
        request: The failing request.
        exc: The :class:`ValueError`.

    Returns:
        A 400 response quoting the service's message, which services author for an operator.
    """
    logger.info("api.bad_request", path=request.url.path, detail=str(exc))
    return error_response(
        request,
        status_code=status.HTTP_400_BAD_REQUEST,
        error=ERROR_INVALID_REQUEST,
        detail=str(exc) or "The request could not be processed.",
    )


async def handle_unsupported_flow(request: Request, exc: Exception) -> Response:
    """Return 409 when a provider cannot perform the operation by design.

    Golden rule #10: LinkedIn and Workday do not support automated submission, and saying so
    plainly is the honest answer. It is not a 500 (nothing broke) and not a 501 (the server
    implements this fine — the employer's ATS does not permit it).

    Args:
        request: The failing request.
        exc: The :class:`~app.jobs.base.UnsupportedFlowError`.

    Returns:
        A 409 response.
    """
    logger.info("api.unsupported_flow", path=request.url.path, detail=str(exc))
    return error_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        error=ERROR_UNSUPPORTED_FLOW,
        detail=_UNSUPPORTED_FLOW_DETAIL,
    )


async def handle_rate_limited(request: Request, exc: Exception) -> Response:
    """Return 429 when a provider asked the caller to slow down.

    Args:
        request: The failing request.
        exc: The :class:`~app.jobs.base.ProviderRateLimitError`.

    Returns:
        A 429 response, carrying ``Retry-After`` when the provider named a cooldown.
    """
    retry_after = getattr(exc, "retry_after", None)
    headers = (
        {_RETRY_AFTER_HEADER: str(int(retry_after))}
        if isinstance(retry_after, (int, float)) and retry_after > 0
        else None
    )
    return error_response(
        request,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        error=ERROR_RATE_LIMITED,
        detail="The ATS provider is rate limiting this client. Try again shortly.",
        headers=headers,
    )


async def handle_posting_unavailable(request: Request, exc: Exception) -> Response:
    """Return 404 when a posting has been filled, withdrawn or expired.

    Args:
        request: The failing request.
        exc: The :class:`~app.jobs.base.PostingUnavailableError`.

    Returns:
        A 404 response.
    """
    return error_response(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        error=ERROR_POSTING_UNAVAILABLE,
        detail="This posting is no longer available from the provider.",
    )


async def handle_provider_auth(request: Request, exc: Exception) -> Response:
    """Return 502 when a provider rejected the request's credentials.

    Args:
        request: The failing request.
        exc: The :class:`~app.jobs.base.ProviderAuthError`.

    Returns:
        A 502 response. Not a 401: the client authenticated with *this* server perfectly
        well — it is the upstream ATS that refused us.
    """
    logger.warning("api.provider_auth_failed", provider=getattr(exc, "provider", None))
    return error_response(
        request,
        status_code=status.HTTP_502_BAD_GATEWAY,
        error=ERROR_PROVIDER_AUTH,
        detail="The ATS provider refused the request's credentials. Sign in again.",
    )


async def handle_provider_error(request: Request, exc: Exception) -> Response:
    """Return 502 for any other upstream ATS failure.

    Args:
        request: The failing request.
        exc: The :class:`~app.jobs.base.ProviderError`.

    Returns:
        A 502 response quoting the provider module's message, which is authored for an
        operator and carries no credential.
    """
    logger.warning(
        "api.provider_failed",
        provider=getattr(exc, "provider", None),
        status_code=getattr(exc, "status_code", None),
        detail=str(exc),
    )
    return error_response(
        request,
        status_code=status.HTTP_502_BAD_GATEWAY,
        error=ERROR_PROVIDER,
        detail=str(exc) or "The ATS provider could not be reached.",
    )


async def handle_render_error(request: Request, exc: Exception) -> Response:
    """Return 500 when a document could not be rendered.

    Reads ``exc.message`` rather than ``str(exc)``: the latter appends the rendering
    engine's stderr, which carries absolute filesystem paths and the full LaTeX log. That
    belongs in the server log, not in a response body.

    Args:
        request: The failing request.
        exc: The :class:`~app.documents.renderer.DocumentRenderError`.

    Returns:
        A 500 response naming the engine and template but nothing else.
    """
    engine = getattr(exc, "engine", None)
    template = getattr(exc, "template", None)
    message = getattr(exc, "message", None)
    logger.warning(
        "api.render_failed",
        engine=engine,
        template=template,
        detail=str(exc),
    )
    return error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error=ERROR_RENDER_FAILED,
        detail=message if isinstance(message, str) and message else "The document could not be rendered.",
        extra={"engine": engine, "template": template},
    )


async def handle_plugin_not_found(request: Request, exc: Exception) -> Response:
    """Return 404 when a named plugin is not registered.

    Args:
        request: The failing request.
        exc: The :class:`~app.plugins.base.PluginNotFound`.

    Returns:
        A 404 response. The registry's message lists the names that *do* exist, which turns
        a configuration typo into a one-line fix.
    """
    return error_response(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        error=ERROR_PLUGIN_NOT_FOUND,
        detail=str(exc),
    )


async def handle_plugin_disabled(request: Request, exc: Exception) -> Response:
    """Return 409 when a registered plugin has been switched off.

    Args:
        request: The failing request.
        exc: The :class:`~app.plugins.base.PluginDisabled`.

    Returns:
        A 409 response. Distinct from 404 on purpose: "you turned this off" and "this does
        not exist" call for different fixes.
    """
    return error_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        error=ERROR_PLUGIN_DISABLED,
        detail=str(exc),
    )


async def handle_plugin_load_error(request: Request, exc: Exception) -> Response:
    """Return 500 when a plugin could not be imported or constructed.

    Args:
        request: The failing request.
        exc: The :class:`~app.plugins.base.PluginLoadError`.

    Returns:
        A 500 response quoting the registry's message, which names the plugin and not its
        configuration.
    """
    logger.warning("api.plugin_load_failed", detail=str(exc))
    return error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error=ERROR_PLUGIN_FAILED,
        detail=str(exc),
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """Return 500 for anything not otherwise mapped.

    The traceback is logged with ``exc_info`` — where the structlog chain governs what is
    rendered and frame locals are off — and the client receives a fixed sentence plus the
    correlation id that finds it.

    Args:
        request: The failing request.
        exc: The unhandled exception.

    Returns:
        A 500 response containing no detail derived from *exc*.
    """
    logger.error(
        "api.unhandled_exception",
        path=request.url.path,
        http_method=request.method,
        error_type=type(exc).__name__,
        exc_info=exc,
    )
    return error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error=ERROR_INTERNAL,
        detail=INTERNAL_ERROR_DETAIL,
    )


#: Exception type → handler, most specific first. Starlette resolves a handler by walking
#: ``type(exc).__mro__``, so ordering here is documentation rather than dispatch: what makes
#: ``InvalidTransition`` beat ``ValueError`` is that both are registered.
_HANDLERS: Final[tuple[tuple[type[Exception], Any], ...]] = (
    (RequestValidationError, handle_validation_error),
    (InvalidTransition, handle_invalid_transition),
    (UnsupportedFlowError, handle_unsupported_flow),
    (ProviderRateLimitError, handle_rate_limited),
    (PostingUnavailableError, handle_posting_unavailable),
    (ProviderAuthError, handle_provider_auth),
    (ProviderError, handle_provider_error),
    (DocumentRenderError, handle_render_error),
    (PluginNotFound, handle_plugin_not_found),
    (PluginDisabled, handle_plugin_disabled),
    (PluginLoadError, handle_plugin_load_error),
    (LookupError, handle_lookup_error),
    (ValueError, handle_value_error),
    (Exception, handle_unexpected_error),
)


def install_exception_handlers(app: FastAPI) -> None:
    """Register every handler in this module on *app*.

    ``HTTPException`` is registered under both FastAPI's and Starlette's class, because a
    404 raised by the router itself is Starlette's and a 404 raised by a handler is
    FastAPI's, and both must render the same body.

    Args:
        app: The application to install onto.
    """
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(HTTPException, handle_http_exception)
    for exception_type, handler in _HANDLERS:
        app.add_exception_handler(exception_type, handler)
    logger.debug("api.exception_handlers_installed", count=len(_HANDLERS) + 2)
