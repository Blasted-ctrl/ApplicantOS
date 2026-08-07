"""Async engine, session factory, and the two ways to get a session.

This module owns the single :class:`~sqlalchemy.ext.asyncio.AsyncEngine` for the process.
Creating an engine is expensive (it owns the connection pool) and creating more than one
silently multiplies the connection count, so everything shares the module-level
:data:`engine` and :data:`async_session_factory`.

There are exactly two supported ways to obtain a session, and they differ in who commits:

:func:`get_session`
    A FastAPI dependency. Yields a session, rolls back if the request handler raises, and
    always closes. It does **not** commit — the service layer decides what constitutes a
    unit of work, and an implicit commit on request teardown would silently persist
    half-finished writes.

:func:`session_scope`
    An async context manager for workers, CLI entry points, and anything outside a request.
    Commits on clean exit, rolls back on any exception, always closes.

Backend-specific behaviour:

* **SQLite** gets ``check_same_thread=False`` (the async driver hands connections between
  threads) and :class:`~sqlalchemy.pool.NullPool`, because SQLite's own file locking is the
  real concurrency control and pooled connections just multiply ``database is locked``
  errors. An in-memory database is the one exception — see :func:`_pool_options`.
  A ``connect`` listener issues ``PRAGMA foreign_keys=ON``, without which SQLite silently
  ignores every ``ON DELETE CASCADE`` in the schema.
* **PostgreSQL** gets a bounded pool with recycling, so a connection killed by a proxy or
  an idle timeout is replaced rather than handed to a caller half-dead. ``pool_pre_ping``
  covers the same failure mode for both backends.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.config.logging import get_logger
from app.config.settings import Settings, settings
from app.database.base import Base
from app.database.types import needs_vector_extension

__all__ = [
    "async_session_factory",
    "check_database",
    "create_engine_from_settings",
    "dispose_engine",
    "engine",
    "get_session",
    "init_db",
    "session_scope",
]

logger = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Pool tuning — named rather than inlined so an operator can reason about them.
# --------------------------------------------------------------------------------------

#: Steady-state connections held open per process against a server-based database.
POOL_SIZE: Final[int] = 10

#: Additional connections allowed above :data:`POOL_SIZE` during a burst.
MAX_OVERFLOW: Final[int] = 20

#: Seconds before a pooled connection is discarded and reopened. Kept below the typical
#: 3600s idle timeout of managed PostgreSQL and connection proxies.
POOL_RECYCLE_SECONDS: Final[int] = 1800

#: Seconds to wait for a free connection before raising ``TimeoutError``.
POOL_TIMEOUT_SECONDS: Final[int] = 30

#: Cheapest possible round-trip, used by :func:`check_database`.
HEALTHCHECK_QUERY: Final[str] = "SELECT 1"

#: SQL that installs pgvector. Idempotent, and a no-op when the extension already exists.
PGVECTOR_EXTENSION_SQL: Final[str] = "CREATE EXTENSION IF NOT EXISTS vector"

#: Module imported by :func:`init_db` to populate ``Base.metadata`` before ``create_all``.
MODELS_PACKAGE: Final[str] = "app.models"

#: URL fragments that identify an in-memory SQLite database.
_IN_MEMORY_SQLITE_MARKERS: Final[tuple[str, ...]] = (":memory:", "mode=memory")

#: PRAGMAs applied to every new SQLite connection, in order.
#: ``foreign_keys`` is mandatory (SQLite defaults it OFF, silently voiding every cascade);
#: WAL plus ``NORMAL`` synchronous is the standard durability/throughput trade for a
#: single-user desktop database.
SQLITE_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
)


# --------------------------------------------------------------------------------------
# Engine construction
# --------------------------------------------------------------------------------------


def _is_sqlite_url(url: str) -> bool:
    """Return whether *url* addresses a SQLite database."""
    return url.startswith("sqlite")


def _is_in_memory_sqlite(url: str) -> bool:
    """Return whether *url* addresses an in-memory SQLite database."""
    return _is_sqlite_url(url) and any(marker in url for marker in _IN_MEMORY_SQLITE_MARKERS)


def _pool_options(database_url: str) -> dict[str, Any]:
    """Return the pool and driver options appropriate for *database_url*.

    SQLite gets ``check_same_thread=False`` plus :class:`~sqlalchemy.pool.NullPool` so that
    the database file's own locking is the only concurrency control. The single exception
    is an in-memory database: it lives inside its connection, so ``NullPool`` would discard
    the schema between statements — those use :class:`~sqlalchemy.pool.StaticPool` to keep
    exactly one connection alive.

    Args:
        database_url: The SQLAlchemy URL the engine will be built from.

    Returns:
        Keyword arguments to pass to :func:`~sqlalchemy.ext.asyncio.create_async_engine`.
    """
    if _is_sqlite_url(database_url):
        options: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        options["poolclass"] = StaticPool if _is_in_memory_sqlite(database_url) else NullPool
        return options
    return {
        "pool_size": POOL_SIZE,
        "max_overflow": MAX_OVERFLOW,
        "pool_recycle": POOL_RECYCLE_SECONDS,
        "pool_timeout": POOL_TIMEOUT_SECONDS,
    }


def _register_sqlite_pragmas(target: AsyncEngine) -> None:
    """Attach a ``connect`` listener that applies :data:`SQLITE_PRAGMAS`.

    Registered on the underlying synchronous engine, which is where SQLAlchemy emits pool
    events even for async engines. A no-op for every non-SQLite backend.

    Args:
        target: The engine whose connections should be configured.
    """
    if target.dialect.name != "sqlite":
        return

    @event.listens_for(target.sync_engine, "connect")
    def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
        """Run the SQLite PRAGMAs on each freshly opened connection."""
        cursor = dbapi_connection.cursor()
        try:
            for pragma in SQLITE_PRAGMAS:
                cursor.execute(pragma)
        finally:
            cursor.close()


def create_engine_from_settings(config: Settings) -> AsyncEngine:
    """Build the async engine described by *config*.

    Args:
        config: Settings supplying the database URL and the debug/echo flag.

    Returns:
        A configured :class:`~sqlalchemy.ext.asyncio.AsyncEngine` with the SQLite PRAGMA
        listener already attached when relevant. No connection is opened here — SQLAlchemy
        connects lazily on first use.
    """
    created = create_async_engine(
        config.database_url,
        echo=config.debug,
        pool_pre_ping=True,
        future=True,
        **_pool_options(config.database_url),
    )
    _register_sqlite_pragmas(created)
    logger.debug(
        "database.engine_created",
        dialect=created.dialect.name,
        driver=created.dialect.driver,
        echo=config.debug,
    )
    return created


#: The process-wide async engine. Import this module rather than rebinding the name, so
#: every consumer shares one connection pool.
engine: AsyncEngine = create_engine_from_settings(settings)

#: Factory for :class:`~sqlalchemy.ext.asyncio.AsyncSession` objects bound to :data:`engine`.
#:
#: ``expire_on_commit=False`` keeps attributes readable after a commit, which matters
#: because a FastAPI response is serialised *after* the unit of work closes and expired
#: attributes would trigger a lazy load on a dead session. ``autoflush=False`` makes flush
#: points explicit, so a read never silently persists a half-built object.
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# --------------------------------------------------------------------------------------
# Session acquisition
# --------------------------------------------------------------------------------------


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    Rolls back and re-raises if the handler fails, and always closes the session. It never
    commits: the service layer owns transaction boundaries, so an endpoint that only reads
    costs no write transaction and a handler that raises halfway through never leaves a
    partial write behind.

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` valid for the request's lifetime.
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async context manager providing a transactional session.

    Commits when the block exits cleanly, rolls back on any exception, and always closes.
    This is the session helper for Celery tasks, CLI commands, and background services —
    anywhere without a FastAPI dependency graph.

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` whose work is committed on
        successful exit.

    Example:
        >>> async with session_scope() as session:  # doctest: +SKIP
        ...     session.add(record)
    """
    session = async_session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


def _import_models() -> None:
    """Import :mod:`app.models` so every table registers itself on ``Base.metadata``.

    ``Base.metadata`` is populated as a side effect of importing model modules. Without
    this, ``create_all`` would find an empty schema. A failure is logged rather than
    raised: the metadata may already be populated by an earlier import, and refusing to
    start over an import ordering detail would be worse than proceeding.
    """
    try:
        importlib.import_module(MODELS_PACKAGE)
    except ImportError as exc:
        logger.warning("database.models_import_failed", package=MODELS_PACKAGE, error=str(exc))


async def _ensure_extensions() -> None:
    """Install PostgreSQL extensions the configured feature set needs.

    Currently only ``pgvector``, and only when
    :func:`~app.database.types.needs_vector_extension` says the schema about to be created
    will actually contain a ``vector(n)`` column — the same predicate the initial Alembic
    migration uses, so the two bootstrap paths never disagree. Notably this is *not*
    ``settings.vector_store``: that selects which
    :class:`~app.knowledge.vector.base.VectorStore` implementation runs, while the column
    type depends only on the dialect and whether the ``pgvector`` package is importable.

    Managed PostgreSQL often refuses ``CREATE EXTENSION`` to non-superusers, so a failure
    is logged and tolerated: the operator can install it once by hand, and until then the
    embedding columns fall back to JSON.
    """
    if not needs_vector_extension(engine.dialect.name):
        return
    try:
        async with engine.begin() as connection:
            await connection.execute(text(PGVECTOR_EXTENSION_SQL))
    except SQLAlchemyError as exc:
        logger.warning("database.pgvector_extension_failed", error=str(exc))
    else:
        logger.debug("database.pgvector_extension_ready")


async def init_db() -> None:
    """Create the schema for the configured database.

    Imports the model package, installs any required extensions, then issues
    ``CREATE TABLE ... IF NOT EXISTS`` for every mapped table.

    This is the bootstrap path for SQLite installs, test databases, and first-run
    development. **Alembic remains the source of truth for schema migrations** — never call
    this against a database that Alembic manages, or the two will disagree about what has
    been applied.
    """
    _import_models()
    await _ensure_extensions()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info(
        "database.initialized",
        dialect=engine.dialect.name,
        tables=len(Base.metadata.tables),
    )


async def dispose_engine() -> None:
    """Close every pooled connection and release the engine's resources.

    Call from the application shutdown hook and from worker teardown. Skipping it leaves
    sockets open until the OS reaps them, which shows up as connection-limit errors during
    rapid restarts.
    """
    await engine.dispose()
    logger.debug("database.engine_disposed")


async def check_database() -> bool:
    """Return whether the database is reachable and answering queries.

    Backs the ``/ready`` endpoint and the worker healthcheck task. Never raises: an
    unreachable database is an expected operational state that must be *reported*, not
    propagated as a 500.

    Returns:
        ``True`` when a trivial query succeeds, ``False`` on any connection or query error.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text(HEALTHCHECK_QUERY))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("database.healthcheck_failed", error=str(exc), error_type=type(exc).__name__)
        return False
    return True
