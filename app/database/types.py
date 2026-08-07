"""Portable SQLAlchemy column types.

ApplicantOS ships in two shapes: a full install on PostgreSQL (with ``pgvector``) and a
lightweight zero-infrastructure install on SQLite. The same ORM models must map cleanly to
both, so every non-trivial column type is defined here as a
:class:`~sqlalchemy.types.TypeDecorator` that picks the best native representation for the
dialect it finds itself on and falls back to a portable one everywhere else.

======================  ==========================  ===========================
Type                    PostgreSQL                  Everything else
======================  ==========================  ===========================
:class:`GUID`           native ``UUID``             ``CHAR(36)``
:class:`JSONType`       ``JSONB``                   ``JSON``
:class:`EmbeddingType`  ``vector(n)`` via pgvector  ``JSON`` array of floats
:class:`UTCDateTime`    ``TIMESTAMPTZ``             ``DATETIME`` coerced to UTC
======================  ==========================  ===========================

Two invariants hold across every backend:

* **UUIDs round-trip as :class:`uuid.UUID`.** Binds accept either a ``UUID`` or its string
  form; results are always ``UUID`` objects, so application code never has to care whether
  it is talking to PostgreSQL or SQLite.
* **Datetimes are always timezone-aware UTC.** SQLite silently drops timezone information,
  so :class:`UTCDateTime` re-attaches UTC on the way out and normalises any aware datetime
  to UTC on the way in. Naive datetimes are interpreted as UTC rather than rejected,
  because that is what every producer in this codebase actually means.

``pgvector`` is imported lazily and never required: on a machine without it,
:class:`EmbeddingType` degrades to a JSON array and the in-memory / SQLite vector stores
take over. Importing this module must work with nothing installed beyond SQLAlchemy.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Final

from sqlalchemy import CHAR, DateTime, JSON, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgresUUID
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from app.config.logging import get_logger

__all__ = [
    "EmbeddingType",
    "GUID",
    "JSONType",
    "POSTGRES_DIALECT_NAMES",
    "UTCDateTime",
    "UUID_STRING_LENGTH",
    "needs_vector_extension",
    "utcnow",
]

logger = get_logger(__name__)

#: Length of the canonical hyphenated UUID string form ("8-4-4-4-12").
UUID_STRING_LENGTH: Final[int] = 36

#: Dialect names that expose PostgreSQL's native type set. CockroachDB speaks the
#: PostgreSQL wire protocol but reports its own dialect name.
POSTGRES_DIALECT_NAMES: Final[frozenset[str]] = frozenset({"postgresql", "cockroachdb"})

#: Fallback embedding width used when no explicit dimension is supplied and settings
#: cannot be read. Matches ``text-embedding-3-small``.
FALLBACK_EMBEDDING_DIM: Final[int] = 1536


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Used as the Python-side default for every timestamp column. Never use
    :meth:`datetime.utcnow`, which returns a naive value and silently loses the offset.

    Returns:
        The current instant, with ``tzinfo`` set to UTC.
    """
    return datetime.now(timezone.utc)


def _is_postgres(dialect: Dialect) -> bool:
    """Return whether *dialect* supports PostgreSQL's native type extensions."""
    return dialect.name in POSTGRES_DIALECT_NAMES


@lru_cache(maxsize=1)
def _load_pgvector_type() -> type[Any] | None:
    """Import ``pgvector.sqlalchemy.Vector`` if it is installed.

    The import is lazy and cached so that a missing optional dependency costs one failed
    import for the lifetime of the process instead of one per column definition.

    Returns:
        The ``Vector`` type class, or ``None`` when ``pgvector`` is not installed.
    """
    try:
        from pgvector.sqlalchemy import Vector
    except ImportError:
        logger.debug("database.pgvector_unavailable", fallback="json")
        return None
    return Vector


def needs_vector_extension(dialect_name: str) -> bool:
    """Return whether ``CREATE EXTENSION vector`` is meaningful for *dialect_name*.

    Both conditions must hold, and they are different questions:

    * The dialect must be PostgreSQL — no other backend has this extension.
    * The ``pgvector`` **Python package** must be importable, because that is exactly the
      condition under which :class:`EmbeddingType` renders a ``vector(n)`` column. Without
      it, embedding columns compile to plain ``JSON`` and the server-side extension is
      irrelevant.

    Both the runtime bootstrap (:func:`app.database.session.init_db`) and the initial
    Alembic migration must answer this question identically, or one will install an
    extension the other's schema cannot use.

    Args:
        dialect_name: The active dialect's ``name`` (for example ``"postgresql"``).

    Returns:
        ``True`` when the extension is both meaningful and required.
    """
    if dialect_name not in POSTGRES_DIALECT_NAMES:
        return False
    return _load_pgvector_type() is not None


@lru_cache(maxsize=1)
def _default_embedding_dim() -> int:
    """Return the configured embedding width.

    Imported lazily so that :mod:`app.database.types` stays usable in contexts where
    settings construction is undesirable (for example, Alembic autogenerate on a machine
    with no ``.env``).

    Returns:
        ``settings.embedding_dim``, or :data:`FALLBACK_EMBEDDING_DIM` if settings cannot
        be loaded.
    """
    try:
        from app.config.settings import settings
    except ImportError:  # pragma: no cover - settings is a hard dependency in practice
        logger.warning("database.settings_unavailable", fallback=FALLBACK_EMBEDDING_DIM)
        return FALLBACK_EMBEDDING_DIM
    return settings.embedding_dim


class GUID(TypeDecorator[uuid.UUID]):
    """Platform-independent UUID column.

    Stores a native ``UUID`` on PostgreSQL and a hyphenated ``CHAR(36)`` everywhere else.
    Binds accept a :class:`uuid.UUID` or any string parseable as one; results are always
    :class:`uuid.UUID` instances.
    """

    impl = CHAR
    cache_ok = True

    @property
    def python_type(self) -> type[uuid.UUID]:
        """Return the Python type this column produces."""
        return uuid.UUID

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Choose the backing type for *dialect*.

        Args:
            dialect: The dialect the statement is being compiled for.

        Returns:
            A native ``UUID`` descriptor on PostgreSQL, otherwise ``CHAR(36)``.
        """
        if _is_postgres(dialect):
            return dialect.type_descriptor(PostgresUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(UUID_STRING_LENGTH))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        """Normalise *value* to the representation the dialect expects.

        Args:
            value: ``None``, a :class:`uuid.UUID`, or a string form of one.
            dialect: The active dialect.

        Returns:
            A ``UUID`` on PostgreSQL, a hyphenated string elsewhere, or ``None``.

        Raises:
            ValueError: If *value* is a string that is not a valid UUID.
        """
        if value is None:
            return None
        identifier = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        if _is_postgres(dialect):
            return identifier
        return str(identifier)

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        """Convert a stored value back into a :class:`uuid.UUID`.

        Args:
            value: The raw value returned by the driver.
            dialect: The active dialect.

        Returns:
            The parsed UUID, or ``None`` when the column was NULL.
        """
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONType(TypeDecorator[Any]):
    """Portable JSON column.

    Uses ``JSONB`` on PostgreSQL — indexable, containment-queryable, and stored in a binary
    form — and the generic ``JSON`` type on every other backend. Serialisation is left to
    the dialect, so values round-trip as plain Python containers.

    Note:
        ``JSONB`` values are mutable Python objects that SQLAlchemy does not track by
        default. Reassign the attribute (or wrap the column in ``MutableDict``) rather
        than mutating a nested structure in place and expecting a flush to notice.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Choose ``JSONB`` on PostgreSQL and generic ``JSON`` elsewhere.

        Args:
            dialect: The dialect the statement is being compiled for.

        Returns:
            The dialect-appropriate JSON type descriptor.
        """
        if _is_postgres(dialect):
            return dialect.type_descriptor(JSONB(astext_type=Text()))
        return dialect.type_descriptor(JSON())


class EmbeddingType(TypeDecorator[list[float]]):
    """Vector-embedding column with a portable fallback.

    On PostgreSQL with ``pgvector`` installed this compiles to a native ``vector(n)``
    column, which supports ANN indexes and distance operators. Anywhere else — SQLite, or
    PostgreSQL without the extension — it stores a JSON array of floats, which keeps the
    schema valid and lets :class:`~app.knowledge.vector.sqlite_vec.SqliteVecStore` and the
    pure-Python in-memory store do the similarity work instead.

    Args:
        dim: Embedding width. Defaults to ``settings.embedding_dim``. Changing this after
            rows exist requires a migration and a re-index, because stored vectors are
            fixed-width on PostgreSQL.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int | None = None, **kwargs: Any) -> None:
        """Initialise the column with an explicit or configured embedding width."""
        self.dim: int = dim if dim is not None else _default_embedding_dim()
        super().__init__(**kwargs)

    @property
    def python_type(self) -> type[list]:
        """Return the Python type this column produces."""
        return list

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Choose ``vector(dim)`` when possible, otherwise ``JSON``.

        Args:
            dialect: The dialect the statement is being compiled for.

        Returns:
            A pgvector ``Vector`` descriptor on PostgreSQL when the extension package is
            importable, otherwise a generic ``JSON`` descriptor.
        """
        if _is_postgres(dialect):
            vector_type = _load_pgvector_type()
            if vector_type is not None:
                return dialect.type_descriptor(vector_type(self.dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        """Coerce *value* into a plain list of floats.

        Accepts any iterable of numbers — lists, tuples, and numpy arrays alike — so
        callers never have to convert before assigning.

        Args:
            value: ``None`` or an iterable of numeric components.
            dialect: The active dialect.

        Returns:
            A ``list[float]``, or ``None``.
        """
        if value is None:
            return None
        return [float(component) for component in value]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        """Convert a stored vector back into a plain list of floats.

        Args:
            value: The raw value from the driver — a JSON array, or a numpy array when
                pgvector is active.
            dialect: The active dialect.

        Returns:
            A ``list[float]``, or ``None`` when the column was NULL.
        """
        if value is None:
            return None
        return [float(component) for component in value]


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime column that is always UTC.

    PostgreSQL stores ``TIMESTAMPTZ`` and hands back aware datetimes; SQLite stores an ISO
    string and hands back a naive one. This decorator erases the difference:

    * **On bind** — naive values are *interpreted* as UTC (every producer in this codebase
      uses :func:`utcnow`), aware values are converted to UTC.
    * **On result** — naive values get UTC attached, aware values are converted to UTC.

    The result is that no comparison anywhere in the application can accidentally mix an
    aware and a naive datetime, which is the single most common source of ``TypeError`` in
    a codebase that spans two databases.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    @property
    def python_type(self) -> type[datetime]:
        """Return the Python type this column produces."""
        return datetime

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        """Normalise *value* to an aware UTC datetime before storage.

        Args:
            value: ``None`` or a :class:`~datetime.datetime`.
            dialect: The active dialect.

        Returns:
            An aware UTC datetime, or ``None``.

        Raises:
            TypeError: If *value* is neither ``None`` nor a datetime. Failing loudly here
                catches date/string mix-ups at the boundary instead of on read.
        """
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime requires a datetime, got {type(value).__name__!r}")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        """Guarantee the value read back is an aware UTC datetime.

        Args:
            value: The raw value from the driver, possibly naive.
            dialect: The active dialect.

        Returns:
            An aware UTC datetime, or ``None``.
        """
        if value is None:
            return None
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
