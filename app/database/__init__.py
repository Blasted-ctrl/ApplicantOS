"""Database package — declarative base, portable column types, and session plumbing.

Import everything database-related from here::

    from app.database import Base, session_scope, GUID

Session-level names (:data:`engine`, :data:`async_session_factory`, :func:`get_session`,
:func:`session_scope`, :func:`init_db`, :func:`dispose_engine`, :func:`check_database`) are
resolved **lazily** through a module ``__getattr__``. The reason is concrete: constructing
the async engine imports the configured DBAPI driver, so an eager re-export would mean that
merely importing ``app.database.base`` — as every model module and Alembic's ``env.py``
does — would fail on a machine without ``asyncpg`` installed. Deferring the import keeps
schema definition, migration autogeneration, and unit tests independent of driver
availability, while ``from app.database import engine`` still works exactly as written.

Types and the declarative base are imported eagerly: they depend on nothing beyond
SQLAlchemy itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.database.base import NAMING_CONVENTION, Base
from app.database.types import GUID, EmbeddingType, JSONType, UTCDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from app.database.session import (
        async_session_factory,
        check_database,
        dispose_engine,
        engine,
        get_session,
        init_db,
        session_scope,
    )

__all__ = [
    "GUID",
    "NAMING_CONVENTION",
    "Base",
    "EmbeddingType",
    "JSONType",
    "UTCDateTime",
    "async_session_factory",
    "check_database",
    "dispose_engine",
    "engine",
    "get_session",
    "init_db",
    "session_scope",
    "utcnow",
]

#: Names re-exported from :mod:`app.database.session`, resolved on first attribute access.
_LAZY_SESSION_EXPORTS: frozenset[str] = frozenset(
    {
        "async_session_factory",
        "check_database",
        "dispose_engine",
        "engine",
        "get_session",
        "init_db",
        "session_scope",
    }
)


def __getattr__(name: str) -> Any:
    """Resolve session-level exports on demand (PEP 562).

    Args:
        name: The attribute being looked up on the package.

    Returns:
        The requested object from :mod:`app.database.session`.

    Raises:
        AttributeError: If *name* is not one of this package's exports.
    """
    if name in _LAZY_SESSION_EXPORTS:
        from app.database import session as _session_module

        return getattr(_session_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return the package's public names, including the lazily resolved ones."""
    return sorted(__all__)
