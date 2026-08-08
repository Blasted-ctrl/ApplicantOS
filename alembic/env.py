"""Alembic environment — async-aware, settings-driven, backend-portable.

This module is what makes ``alembic upgrade head`` work against both deployment shapes
ApplicantOS ships in: PostgreSQL with ``pgvector``, and a zero-infrastructure SQLite file.
Four decisions carry that:

**The URL comes from settings, never from ``alembic.ini``.** ``app.config.settings`` is the
single source of runtime configuration; a connection string duplicated into a checked-in ini
file is how a migration ends up applied to the wrong database.

**The whole model package is imported, not a hand-kept list.** SQLAlchemy only registers a
table when the module declaring it executes, so ``target_metadata`` is only complete if
every model module has been imported. :mod:`app.models` does exactly that and nothing else,
which is why importing it — rather than individual modules — is the contract.

**Online mode runs through the async engine.** ``settings.database_url`` names an async
driver (``asyncpg``, ``aiosqlite``), which a synchronous ``engine_from_config`` cannot open.
The async path drives migrations through ``connection.run_sync(do_run_migrations)``. A URL
that names a *synchronous* driver is detected and run on a plain engine instead, so
``SYNC_DATABASE_URL=... alembic upgrade head`` also works — useful in CI images that install
neither async driver.

**Two comparison flags are on.** ``compare_type=True`` makes autogenerate notice a changed
column type, which it ignores by default — and this schema is full of type decorators
(:class:`~app.database.types.GUID`, :class:`~app.database.types.JSONType`,
:class:`~app.database.types.EmbeddingType`) whose rendering differs per backend.
``render_as_batch=True`` makes every ``ALTER`` operation emit SQLite's copy-and-rename batch
form, without which no future column alteration would run on the lightweight install.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any, Final

from sqlalchemy import Connection, engine_from_config, pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the package (not individual modules) is what guarantees ``Base.metadata`` holds
# every table: a model module that nothing imports is invisible to autogenerate, and the
# resulting migration silently omits its table.
import app.models  # noqa: F401  - imported for the side effect of registering every model
from alembic import context
from app.config.settings import settings
from app.database.base import Base

#: Alembic's parsed ``alembic.ini``.
config: Final = context.config

#: Complete schema definition — populated by the ``app.models`` import above.
target_metadata: Final = Base.metadata

#: ConfigParser treats ``%`` as interpolation syntax, so a password containing one would
#: raise when written into the config. Doubling it round-trips to the original value.
_INTERPOLATION_ESCAPE: Final[tuple[str, str]] = ("%", "%%")

#: Option key Alembic and SQLAlchemy read the connection URL from.
_URL_OPTION: Final[str] = "sqlalchemy.url"


def _configure_logging() -> None:
    """Apply ``alembic.ini``'s logging configuration, when the file supplies one.

    Guarded because ``config.config_file_name`` is ``None`` when Alembic is driven
    programmatically (from a test fixture, or from ``app.database.session.init_db``). Calling
    :func:`logging.config.fileConfig` with ``None`` raises, and calling it unconditionally
    would tear down the application's structlog pipeline in-process.
    """
    if config.config_file_name is not None:
        fileConfig(config.config_file_name, disable_existing_loggers=False)


def _escape_for_config(value: str) -> str:
    """Escape a URL so it survives ConfigParser's ``%`` interpolation.

    Args:
        value: A connection URL, possibly containing a percent-encoded password.

    Returns:
        The URL with every ``%`` doubled.
    """
    return value.replace(*_INTERPOLATION_ESCAPE)


def _resolve_url(*, prefer_sync: bool) -> str:
    """Return the connection URL migrations should run against.

    Args:
        prefer_sync: When ``True``, prefer ``settings.sync_database_url`` (derived from
            ``database_url`` by swapping the async driver for its blocking counterpart).
            Offline mode never opens a connection but still renders dialect-specific SQL, so
            it wants the driver the operator will actually apply the script with.

    Returns:
        A SQLAlchemy connection URL.
    """
    if prefer_sync and settings.sync_database_url:
        return settings.sync_database_url
    return settings.database_url


def _is_async_url(url: str) -> bool:
    """Return whether *url* names a driver that requires the asyncio engine.

    Args:
        url: A SQLAlchemy connection URL.

    Returns:
        ``True`` when the dialect declares itself async (``asyncpg``, ``aiosqlite``, …).
    """
    return bool(make_url(url).get_dialect().is_async)


def _context_options(**overrides: Any) -> dict[str, Any]:
    """Build the keyword arguments for :func:`alembic.context.configure`.

    Centralised so offline and online modes cannot drift apart — a difference between them
    is the classic source of "the generated SQL script does not match what the migration
    actually did".

    Args:
        **overrides: Mode-specific options (``url`` offline, ``connection`` online).

    Returns:
        The full option mapping.
    """
    options: dict[str, Any] = {
        "target_metadata": target_metadata,
        # Off by default in Alembic; without it a changed column type is silently ignored,
        # and this schema is built almost entirely from portable type decorators.
        "compare_type": True,
        # SQLite cannot ALTER a column. Batch mode rewrites the table instead, which is the
        # only way the lightweight install can ever receive a schema change.
        "render_as_batch": True,
        # Name every constraint from the shared convention, so a future migration can
        # address by name a constraint the database would otherwise have named itself.
        "include_schemas": False,
        "compare_server_default": False,
    }
    options.update(overrides)
    return options


def do_run_migrations(connection: Connection) -> None:
    """Run the migration scripts against an open, synchronous connection.

    This is the function handed to ``AsyncConnection.run_sync``: Alembic's migration API is
    synchronous, so the async path opens the connection with the asyncio engine and then
    executes the scripts inside a worker greenlet where blocking calls are legal.

    Args:
        connection: A live synchronous (or greenlet-adapted) connection.
    """
    context.configure(**_context_options(connection=connection))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit migrations as SQL text, without connecting to a database.

    ``--sql`` mode. Used to hand a DBA a reviewable script, and to diff the DDL two branches
    would produce. ``literal_binds`` inlines parameters, since a generated script has no
    place to bind them.
    """
    url = _resolve_url(prefer_sync=True)
    context.configure(
        **_context_options(
            url=url,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_online_sync(url: str) -> None:
    """Run migrations over a blocking engine.

    Taken when the configured URL names a synchronous driver — for example a CI image that
    sets ``DATABASE_URL=postgresql+psycopg://…`` and installs neither ``asyncpg`` nor
    ``aiosqlite``.

    Args:
        url: A SQLAlchemy connection URL naming a synchronous driver.
    """
    config.set_main_option(_URL_OPTION, _escape_for_config(url))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # Migrations use a single connection for their whole lifetime; a pool would hold
        # idle connections open against a database that may be about to be dropped.
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            do_run_migrations(connection)
    finally:
        connectable.dispose()


async def _run_migrations_online_async(url: str) -> None:
    """Run migrations over the asyncio engine.

    The normal path: ``settings.database_url`` names ``asyncpg`` or ``aiosqlite``. Alembic's
    migration API is synchronous, so the actual work happens inside
    ``connection.run_sync(do_run_migrations)``.

    Args:
        url: A SQLAlchemy connection URL naming an async driver.
    """
    config.set_main_option(_URL_OPTION, _escape_for_config(url))
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Connect to the configured database and apply the migrations.

    Dispatches on the driver named in the URL rather than on a setting, so the same command
    works whether the operator points at an async or a blocking driver.
    """
    url = _resolve_url(prefer_sync=False)
    if _is_async_url(url):
        asyncio.run(_run_migrations_online_async(url))
    else:
        _run_migrations_online_sync(url)


_configure_logging()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
