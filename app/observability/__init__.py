"""Prometheus collectors and the request middleware (``docs/CONTRACTS.md`` §16).

Two modules, and they are deliberately independent:

:mod:`app.observability.metrics`
    Every collector §16 names, on a private :data:`~app.observability.metrics.registry`,
    plus the small recorder functions the rest of the codebase calls. It has **no**
    dependency on FastAPI, Starlette or the API layer, so a Celery worker can import it
    and emit the same series a request handler does.

:mod:`app.observability.middleware`
    The ASGI middleware that binds a correlation id, times the request, and feeds
    ``applicantos_http_request_duration_seconds``. It imports Starlette, so it is only
    imported by :func:`app.main.create_app`.

Both are re-exported lazily below: importing this package must not drag Starlette into a
worker process that has no use for it, and must not fail in a tree where the optional
``prometheus_client`` accelerator is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "REQUEST_ID_HEADER",
    "CorrelationIdMiddleware",
    "ObservabilityMiddleware",
    "observe_apply",
    "record_application",
    "record_llm_usage",
    "registry",
    "render_metrics",
    "set_review_queue_size",
    "track_task",
]

if TYPE_CHECKING:  # pragma: no cover - import graph documentation only
    from app.observability.metrics import (
        observe_apply,
        record_application,
        record_llm_usage,
        registry,
        render_metrics,
        set_review_queue_size,
        track_task,
    )
    from app.observability.middleware import (
        REQUEST_ID_HEADER,
        CorrelationIdMiddleware,
        ObservabilityMiddleware,
    )

#: Attribute name → the submodule that defines it, used by :func:`__getattr__`.
_EXPORTS: dict[str, str] = {
    "observe_apply": "metrics",
    "record_application": "metrics",
    "record_llm_usage": "metrics",
    "registry": "metrics",
    "render_metrics": "metrics",
    "set_review_queue_size": "metrics",
    "track_task": "metrics",
    "CorrelationIdMiddleware": "middleware",
    "ObservabilityMiddleware": "middleware",
    "REQUEST_ID_HEADER": "middleware",
}


def __getattr__(name: str) -> Any:
    """Resolve a re-exported name from its defining submodule on first access.

    Keeps ``import app.observability`` free of both Starlette and ``prometheus_client``,
    which matters because :mod:`app.cache` and :mod:`app.ai.llm` probe this package from
    processes that have neither.

    Args:
        name: The attribute being looked up.

    Returns:
        The resolved attribute.

    Raises:
        AttributeError: If *name* is not one of the documented re-exports.
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{module_name}")
    return getattr(module, name)


def __dir__() -> list[str]:
    """Return the package's public names, including the lazy re-exports."""
    return sorted(set(__all__) | set(globals()))
