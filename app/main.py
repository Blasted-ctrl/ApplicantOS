"""The FastAPI application factory (``docs/CONTRACTS.md`` §14).

:func:`create_app` assembles the whole HTTP surface and is the only place that knows the
assembly order. Everything it wires is defined elsewhere — the routers walk themselves into
existence in :mod:`app.api.routes`, the error shapes live in :mod:`app.api.errors`, the
correlation id and the duration histogram live in :mod:`app.observability.middleware` — so
this module contains no behaviour of its own, only the order things go in. That order is
where the interesting decisions are:

**Logging is configured before anything else.** A plugin that fails to load during the
lifespan has to be able to say so through the same structlog chain — with the same
``redact_secrets`` processor — as every later line. Configuring it after the app object
exists would leave startup failures rendered by the standard library's default handler,
which has no redaction at all.

**CORS is the outermost middleware, observability sits inside it.** Starlette runs the
*last* registered middleware first, so the registration order here is observability then
CORS. That way a rejected pre-flight never reaches the histogram (it is not a request the
API served), while every request that does get through is timed and carries a correlation id
before routing — including one that ends in an exception handler.

**Plugins load in the lifespan, not at import.** Importing this module has to stay cheap:
Alembic imports the app package, Celery imports it, and the test suite imports it. Entry-point
scanning and provider construction happen once, on startup, and a plugin that fails is logged
and skipped rather than taking the process down.

**The engine is disposed on shutdown.** A desktop install starts and stops the backend
constantly — every app launch, every update. Leaving pooled connections to be reclaimed by
process exit works on Linux and leaves file locks on Windows, which is a first-class target
here.

**``init_db`` runs only in SQLite mode.** The zero-infrastructure install has no operator to
run ``alembic upgrade head`` and no migration story worth having: it is one file under
``data_dir`` that the app owns end to end. A server install keeps Alembic as the single
source of schema truth, because ``create_all`` silently diverging from the migrations is the
failure that costs a production database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import install_exception_handlers
from app.api.events import bus
from app.api.routes import api_router, root_routers, tag_metadata
from app.api.routes._support import API_V1_PREFIX
from app.api.tasks import reset_dispatcher
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.database.session import dispose_engine, init_db

__all__ = ["API_PREFIX", "DESCRIPTION", "app", "create_app", "lifespan"]

logger = structlog.get_logger(__name__)

#: Mount point for every versioned route group. Read from :mod:`app.api.routes._support`,
#: which is also what builds the download links returned in response bodies — so the prefix
#: is stated once and a link can never point at a path the router does not serve.
API_PREFIX: Final[str] = API_V1_PREFIX

#: Shown at the top of the generated API documentation.
DESCRIPTION: Final[str] = (
    "ApplicantOS automates the repetitive parts of a job hunt: it discovers postings from "
    "supported ATS platforms, scores them against the user's preferences, generates a "
    "tailored resume and cover letter from a personal knowledge graph, applies "
    "automatically where it safely can, and escalates to human review where it cannot.\n\n"
    "Two independent switches gate every real submission and both default closed. "
    "`GET /settings` reports the effective permission as `is_submission_allowed` and never "
    "returns a credential of any kind."
)

#: Response headers a browser client is allowed to read. Without this the correlation id is
#: invisible to the desktop app's fetch layer, which is what turns a user's error report
#: into a log query.
_EXPOSED_HEADERS: Final[list[str]] = ["X-Request-ID", "X-Correlation-ID", "Retry-After"]


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load plugins on startup; release the bus, the dispatcher and the engine on shutdown.

    Startup is deliberately forgiving and shutdown is deliberately thorough. A plugin that
    cannot be imported costs that plugin (:func:`app.plugins.loader.load_all` logs and skips
    it), because one broken provider must not stop a user reading their own application
    history. Shutdown, by contrast, releases everything unconditionally: a desktop backend is
    stopped and restarted constantly, and a connection pool left open holds file locks on
    Windows.

    Args:
        application: The application being started. Unused — every collaborator is a
            process-wide singleton — but part of the lifespan protocol.

    Yields:
        Control, for the lifetime of the application.
    """
    settings = get_settings()

    from app.plugins.loader import load_all
    from app.plugins.registry import registry

    registry.configure(settings)
    load_all()
    logger.info(
        "app.plugins_loaded",
        counts=registry.counts(),
        environment=settings.environment,
    )

    if settings.sqlite_mode:
        # The zero-infrastructure install owns its single database file outright and has
        # nobody to run migrations for it. A server install keeps Alembic authoritative.
        await init_db()
        logger.info("app.schema_ensured", backend="sqlite")

    logger.info(
        "app.started",
        version=__version__,
        environment=settings.environment,
        submission_allowed=settings.is_submission_allowed,
        auto_apply_enabled=settings.auto_apply_enabled,
        dry_run=settings.dry_run,
    )

    try:
        yield
    finally:
        bus.close()
        reset_dispatcher()
        _stop_inline_executor()
        await dispose_engine()
        logger.info("app.stopped")


def _stop_inline_executor() -> None:
    """Stop the in-process task executor, if this install ever started one.

    Imported here rather than at module scope to preserve the property that a process which
    only serves JSON never pulls the pipeline in: an install that dispatches to a real Celery
    worker never touches :mod:`app.workers.inline`, and must not import it just to shut it
    down.
    """
    try:
        from app.workers.inline import shutdown_executor
    except ImportError:  # pragma: no cover - the module is part of the package
        return
    shutdown_executor()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ApplicantOS FastAPI application.

    Args:
        settings: Configuration override. ``None`` (the normal case) resolves the
            process-wide singleton; tests pass an explicit object so that a fixture does not
            have to mutate the environment and clear an ``lru_cache``.

    Returns:
        The configured application, with every route group mounted, the shared error shape
        installed, and the observability middleware in place.
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)

    application = FastAPI(
        title=resolved.app_name,
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=tag_metadata(),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Registered inside-out: Starlette runs the last-registered middleware first, so CORS
    # ends up outermost and a rejected pre-flight never reaches the duration histogram.
    from app.observability.middleware import ObservabilityMiddleware

    application.add_middleware(ObservabilityMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=_EXPOSED_HEADERS,
    )

    install_exception_handlers(application)

    application.include_router(api_router, prefix=API_PREFIX)
    for router in root_routers():
        application.include_router(router)

    logger.debug(
        "app.created",
        routes=len(application.routes),
        api_prefix=API_PREFIX,
        cors_origins=len(resolved.cors_origins),
    )
    return application


#: The ASGI application. ``uvicorn app.main:app`` and the Tauri sidecar both import this.
app: FastAPI = create_app()
