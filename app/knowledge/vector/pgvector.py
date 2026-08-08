"""PostgreSQL vector store, backed by the ``pgvector`` extension.

The production backend (``VECTOR_STORE=pgvector``, the default in
``app/config/settings.py``). It reuses the application's async SQLAlchemy engine, so the
vector index shares one connection pool with the ORM and needs no separate configuration,
credentials, or health check.

Schema (``docs/CONTRACTS.md`` §8.2)
----------------------------------

One table, :data:`VECTOR_TABLE`, partitioned by a ``collection`` column::

    id         text primary key
    collection text not null
    user_id    text
    kind       text
    metadata   jsonb not null
    "text"     text not null
    embedding  vector(settings.embedding_dim) not null

plus an **ivfflat** index on ``embedding`` with ``vector_cosine_ops`` for approximate
nearest-neighbour search, and a **btree** index on ``(collection, user_id)`` — the shape of
every real query, since retrieval is always scoped to one user's slice of one collection.

The primary key is ``id`` alone, as the contract specifies. That makes ids globally unique
rather than unique per collection: re-upserting an id under a different collection *moves*
the record. See ``docs/OPEN_QUESTIONS.md``.

Query
-----

``embedding <=> :q`` is pgvector's cosine **distance** operator, so the ``ORDER BY`` is
ascending and the score every backend reports is ``1 - distance``, clamped to ``[-1, 1]``.
The metadata filter compiles into the same ``WHERE`` clause, which is what lets ``LIMIT k``
count *matching* rows.

**Every filter value is a bound parameter.** Reserved keys compare against their promoted
columns; all other keys use jsonb containment (``metadata @> :fragment``), with the
fragment built by :func:`json.dumps` and bound, never spliced. The only caller-derived text
that reaches the SQL string is a filter *key*, and only after
:func:`~app.knowledge.vector.base.validate_filters` has restricted it to identifier
characters — and even then it is bound as a parameter rather than written into the clause.

Availability
------------

``pgvector`` is optional infrastructure, so both the Python package and the server
extension are probed lazily on first use. If the extension cannot be created — the usual
cause is a managed PostgreSQL that refuses ``CREATE EXTENSION`` to non-superusers — the
store raises :class:`~app.knowledge.vector.base.VectorStoreError` telling the operator to
set ``VECTOR_STORE=sqlite_vec``, which is a complete, working alternative rather than a
degraded one.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Any, ClassVar, Final

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config.settings import Settings, get_settings
from app.database.types import POSTGRES_DIALECT_NAMES
from app.knowledge.vector.base import (
    DEFAULT_TOP_K,
    FILTER_KEY_KIND,
    FILTER_KEY_USER_ID,
    RESERVED_FILTER_KEYS,
    SCORE_MAX,
    SCORE_MIN,
    Filter,
    VectorHit,
    VectorRecord,
    VectorStoreError,
    batch_dimension,
    check_dimension,
    normalize_filter_value,
    rank_hits,
    validate_collection,
    validate_filters,
    validate_k,
)

__all__ = [
    "IVFFLAT_LISTS",
    "IVFFLAT_MAX_DIM",
    "VECTOR_TABLE",
    "PgVectorStore",
]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: The single table holding every collection, per ``docs/CONTRACTS.md`` §8.2.
VECTOR_TABLE: Final[str] = "knowledge_vectors"

#: Number of ivfflat lists (inverted cells). pgvector's guidance is ``rows / 1000`` for up
#: to a million rows; 100 covers the ~100k chunks a heavily-indexed personal knowledge base
#: reaches, and the index can be rebuilt with a different value without touching this code.
IVFFLAT_LISTS: Final[int] = 100

#: Widest embedding an ivfflat index supports. Beyond this pgvector refuses to build one,
#: so the index is skipped and search falls back to an exact sequential scan.
IVFFLAT_MAX_DIM: Final[int] = 2000

#: Index names, so a later migration can find them.
EMBEDDING_INDEX_NAME: Final[str] = f"ix_{VECTOR_TABLE}_embedding_cosine"
COLLECTION_USER_INDEX_NAME: Final[str] = f"ix_{VECTOR_TABLE}_collection_user"

#: Installs pgvector. Idempotent, and a no-op when the extension already exists.
CREATE_EXTENSION_SQL: Final[str] = "CREATE EXTENSION IF NOT EXISTS vector"

#: What an operator should do when pgvector is unavailable. ``sqlite_vec`` is a complete
#: implementation, so this is a real remedy rather than a downgrade notice.
FALLBACK_ADVICE: Final[str] = (
    "set VECTOR_STORE=sqlite_vec to use the local single-file vector store instead"
)

#: Rows per ``INSERT ... ON CONFLICT`` round trip. Large enough to amortise latency, small
#: enough to keep one statement's parameter list well inside PostgreSQL's 65535 bind limit.
UPSERT_BATCH_SIZE: Final[int] = 500

_UPSERT_SQL: Final[str] = f"""
INSERT INTO {VECTOR_TABLE} (id, collection, user_id, kind, metadata, "text", embedding)
VALUES (
    :id, :collection, :user_id, :kind,
    CAST(:metadata AS jsonb), :body, CAST(:embedding AS vector)
)
ON CONFLICT (id) DO UPDATE SET
    collection = EXCLUDED.collection,
    user_id    = EXCLUDED.user_id,
    kind       = EXCLUDED.kind,
    metadata   = EXCLUDED.metadata,
    "text"     = EXCLUDED."text",
    embedding  = EXCLUDED.embedding
"""


def _schema_statements(dimension: int) -> tuple[str, ...]:
    """Return the DDL that brings the vector schema up to date, in dependency order.

    Only the embedding width is substituted, and only after it has been checked to be a
    positive :class:`int` — DDL cannot take a bind parameter for a type modifier.

    Args:
        dimension: ``settings.embedding_dim``.

    Returns:
        ``CREATE EXTENSION``, ``CREATE TABLE``, then the two indexes. The ivfflat index is
        omitted when *dimension* exceeds :data:`IVFFLAT_MAX_DIM`, because pgvector cannot
        build one that wide.
    """
    statements = [
        CREATE_EXTENSION_SQL,
        f"""
        CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} (
            id         text PRIMARY KEY,
            collection text NOT NULL,
            user_id    text,
            kind       text,
            metadata   jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            "text"     text NOT NULL DEFAULT '',
            embedding  vector({dimension}) NOT NULL
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS {COLLECTION_USER_INDEX_NAME}
        ON {VECTOR_TABLE} (collection, user_id)
        """,
    ]
    if dimension <= IVFFLAT_MAX_DIM:
        statements.append(
            f"""
            CREATE INDEX IF NOT EXISTS {EMBEDDING_INDEX_NAME}
            ON {VECTOR_TABLE} USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = {IVFFLAT_LISTS})
            """
        )
    return tuple(statements)


# --------------------------------------------------------------------------------------
# Value encoding
# --------------------------------------------------------------------------------------


def _vector_literal(embedding: Sequence[float]) -> str:
    """Render *embedding* in pgvector's text input format.

    Binding the vector as text and casting it server-side (``CAST(:p AS vector)``) keeps
    this backend independent of driver-level type registration, so it behaves identically
    on asyncpg and psycopg.

    Args:
        embedding: The vector to encode.

    Returns:
        A ``"[1.0,2.0,3.0]"`` literal.

    Raises:
        VectorStoreError: If any component is NaN or infinite. pgvector rejects those, and
            catching it here names the offending vector instead of failing mid-statement.
    """
    components: list[str] = []
    for component in embedding:
        value = float(component)
        if not math.isfinite(value):
            raise VectorStoreError(f"embedding contains a non-finite component: {value!r}")
        components.append(repr(value))
    return f"[{','.join(components)}]"


def _jsonable(value: Any) -> Any:
    """Return *value* in the form it is stored as inside the metadata jsonb column.

    Args:
        value: A scalar metadata or filter value.

    Returns:
        A JSON-serialisable equivalent — :class:`uuid.UUID` becomes text, and ``str``/
        ``int`` subclasses (every :class:`enum.StrEnum` member) collapse to their base
        type so a containment fragment matches what was written.
    """
    normalized = normalize_filter_value(value)
    if isinstance(normalized, bool) or normalized is None:
        return normalized
    if isinstance(normalized, str):
        return str(normalized)
    if isinstance(normalized, int):
        return int(normalized)
    return normalized


def _column_text(value: Any) -> str | None:
    """Return the text form stored in a promoted ``user_id``/``kind`` column."""
    if value is None:
        return None
    return str(normalize_filter_value(value))


def _load_metadata(raw: Any) -> dict[str, Any]:
    """Decode whatever the driver returned for the jsonb ``metadata`` column.

    asyncpg hands back the raw JSON text for an untyped column while psycopg decodes it, so
    both shapes are accepted.

    Args:
        raw: The driver's value — a mapping, a JSON string, or ``None``.

    Returns:
        The metadata mapping; an empty dict when the value is missing or unreadable. One
        malformed row must not fail a whole search.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("vector.pgvector.metadata_unreadable")
        return {}
    return decoded if isinstance(decoded, dict) else {}


# --------------------------------------------------------------------------------------
# Filter compilation — every value is bound, never interpolated
# --------------------------------------------------------------------------------------


def _compile_filters(filters: Filter) -> tuple[list[str], dict[str, Any]]:
    """Compile *filters* into SQL fragments and a parameter mapping.

    Implements the semantics documented in :mod:`app.knowledge.vector.base`. Reserved keys
    compare against their promoted columns; every other key uses jsonb containment, which
    an operator can accelerate later with a GIN index on ``metadata`` without changing a
    line here.

    Args:
        filters: An already-validated filter (see
            :func:`~app.knowledge.vector.base.validate_filters`).

    Returns:
        A ``(clauses, parameters)`` pair. Clauses are joined with ``AND`` by the caller and
        every ``:name`` in them appears in *parameters*.
    """
    clauses: list[str] = []
    parameters: dict[str, Any] = {}

    for index, (key, expected) in enumerate(filters.items()):
        if key in RESERVED_FILTER_KEYS:
            clause = _compile_reserved(key, expected, index, parameters)
        else:
            clause = _compile_metadata(key, expected, index, parameters)
        clauses.append(clause)
    return clauses, parameters


def _compile_reserved(key: str, expected: Any, index: int, parameters: dict[str, Any]) -> str:
    """Compile one filter entry against a promoted (indexed) column.

    Args:
        key: ``user_id`` or ``kind``.
        expected: The filter value.
        index: Position of this entry, used to build unique parameter names.
        parameters: Mapping the produced binds are added to, in place.

    Returns:
        The SQL fragment.
    """
    column = f'"{key}"'
    if expected is None:
        return f"{column} IS NULL"
    if isinstance(expected, (list, tuple, set, frozenset)):
        options = list(expected)
        if not options:
            return "FALSE"
        names: list[str] = []
        for position, option in enumerate(options):
            name = f"flt_{index}_{position}"
            parameters[name] = _column_text(option)
            names.append(f":{name}")
        return f"{column} IN ({', '.join(names)})"
    name = f"flt_{index}"
    parameters[name] = _column_text(expected)
    return f"{column} = :{name}"


def _compile_metadata(key: str, expected: Any, index: int, parameters: dict[str, Any]) -> str:
    """Compile one filter entry against the metadata jsonb column.

    Args:
        key: The metadata key, already validated as an identifier.
        expected: The filter value.
        index: Position of this entry, used to build unique parameter names.
        parameters: Mapping the produced binds are added to, in place.

    Returns:
        The SQL fragment. ``None`` is expressed as "the key is absent, or its value is JSON
        null", matching the documented cross-backend semantics.
    """
    if expected is None:
        name = f"flt_{index}_key"
        parameters[name] = key
        selector = f"metadata -> CAST(:{name} AS text)"
        return f"({selector} IS NULL OR {selector} = CAST('null' AS jsonb))"

    if isinstance(expected, (list, tuple, set, frozenset)):
        options = list(expected)
        if not options:
            return "FALSE"
        fragments: list[str] = []
        for position, option in enumerate(options):
            name = f"flt_{index}_{position}"
            parameters[name] = json.dumps({key: _jsonable(option)})
            fragments.append(f"metadata @> CAST(:{name} AS jsonb)")
        return f"({' OR '.join(fragments)})"

    name = f"flt_{index}"
    parameters[name] = json.dumps({key: _jsonable(expected)})
    return f"metadata @> CAST(:{name} AS jsonb)"


def _where(clauses: Sequence[str]) -> str:
    """Return *clauses* joined with ``AND`` (the caller always supplies at least one)."""
    return " AND ".join(clauses)


@lru_cache(maxsize=1)
def _pgvector_package_available() -> bool:
    """Return whether the ``pgvector`` Python package is importable.

    Purely diagnostic here — this backend speaks to the server in text and needs no
    driver-level type registration — but :class:`~app.database.types.EmbeddingType` *does*
    need the package to render a ``vector(n)`` ORM column, so knowing the answer makes the
    "why is my embedding column JSON?" question answerable from one log line. Cached
    because a failed import should be paid once per process.
    """
    try:
        import pgvector  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


class PgVectorStore:
    """A :class:`~app.knowledge.vector.base.VectorStore` backed by PostgreSQL + pgvector.

    Args:
        settings: Configuration supplying ``embedding_dim``. Defaults to process settings.
        engine: The async engine to use. Defaults to the application engine
            (:data:`app.database.session.engine`), imported lazily so that constructing
            this store — or merely importing this module — never builds a connection pool.

    Note:
        The schema is created on first use, once per instance, under a lock. Construction
        performs no I/O.
    """

    backend_name: ClassVar[str] = "pgvector"

    def __init__(
        self, settings: Settings | None = None, *, engine: AsyncEngine | None = None
    ) -> None:
        """Record configuration without connecting."""
        self._settings = settings if settings is not None else get_settings()
        self._engine = engine
        self._schema_ready = False
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        """Return a description including the embedding width and readiness."""
        return (
            f"<PgVectorStore dim={self.dimension} "
            f"schema={'ready' if self._schema_ready else 'pending'}>"
        )

    # -- configuration -----------------------------------------------------------------------

    @property
    def dimension(self) -> int:
        """The embedding width the ``vector(n)`` column is declared with."""
        return self._settings.embedding_dim

    # -- writes -------------------------------------------------------------------------------

    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> int:
        """Insert or replace *records*, keyed by :attr:`VectorRecord.id`.

        Args:
            collection: Target collection.
            records: Records to store. Empty is a no-op returning ``0``.

        Returns:
            The number of records written.

        Raises:
            VectorStoreError: If an embedding is empty, its width disagrees with the batch
                or with ``settings.embedding_dim`` (both are named), a component is
                non-finite, or the write fails.
        """
        name = validate_collection(collection)
        if not records:
            return 0

        batch_dimension(records, self.dimension)
        rows = [
            {
                "id": record.id,
                "collection": name,
                "user_id": _column_text(record.metadata.get(FILTER_KEY_USER_ID)),
                "kind": _column_text(record.metadata.get(FILTER_KEY_KIND)),
                "metadata": json.dumps(record.metadata, default=str),
                "body": record.text,
                "embedding": _vector_literal(record.embedding),
            }
            for record in records
        ]

        statement = text(_UPSERT_SQL)
        async with self._begin() as connection:
            for start in range(0, len(rows), UPSERT_BATCH_SIZE):
                await connection.execute(statement, rows[start : start + UPSERT_BATCH_SIZE])

        logger.debug("vector.pgvector.upsert", collection=name, records=len(rows))
        return len(rows)

    async def delete(self, collection: str, ids: Sequence[str]) -> int:
        """Remove records by id.

        Args:
            collection: Collection to delete from.
            ids: Record ids; unknown ids are ignored.

        Returns:
            The number of rows actually removed.
        """
        name = validate_collection(collection)
        identifiers = [str(identifier) for identifier in ids]
        if not identifiers:
            return 0

        statement = text(
            f"DELETE FROM {VECTOR_TABLE} WHERE collection = :collection AND id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        async with self._begin() as connection:
            result = await connection.execute(statement, {"collection": name, "ids": identifiers})
        removed = max(result.rowcount, 0)
        logger.debug("vector.pgvector.delete", collection=name, removed=removed)
        return removed

    async def clear(self, collection: str) -> int:
        """Delete every row of *collection*.

        Args:
            collection: Collection to empty.

        Returns:
            The number of rows removed.
        """
        name = validate_collection(collection)
        statement = text(f"DELETE FROM {VECTOR_TABLE} WHERE collection = :collection")
        async with self._begin() as connection:
            result = await connection.execute(statement, {"collection": name})
        removed = max(result.rowcount, 0)
        logger.debug("vector.pgvector.clear", collection=name, removed=removed)
        return removed

    # -- reads ---------------------------------------------------------------------------------

    async def count(self, collection: str) -> int:
        """Return how many records *collection* holds (``0`` when unknown)."""
        name = validate_collection(collection)
        statement = text(f"SELECT count(*) FROM {VECTOR_TABLE} WHERE collection = :collection")
        async with self._connect() as connection:
            result = await connection.execute(statement, {"collection": name})
            row = result.first()
        return int(row[0]) if row is not None else 0

    async def list_collections(self) -> list[str]:
        """Return the names of all non-empty collections, sorted ascending."""
        statement = text(f"SELECT DISTINCT collection FROM {VECTOR_TABLE} ORDER BY collection")
        async with self._connect() as connection:
            result = await connection.execute(statement)
            rows = result.fetchall()
        return [str(row[0]) for row in rows]

    async def query(
        self,
        collection: str,
        embedding: Sequence[float],
        *,
        k: int = DEFAULT_TOP_K,
        filters: Filter | None = None,
    ) -> list[VectorHit]:
        """Return the *k* records in *collection* most similar to *embedding*.

        Ranking is done by pgvector's ``<=>`` cosine-distance operator, which the ivfflat
        index accelerates; the reported score is ``1 - distance``.

        Args:
            collection: Collection to search.
            embedding: Query vector, of ``settings.embedding_dim`` components.
            k: Maximum number of hits.
            filters: Metadata filter compiled into the ``WHERE`` clause, so ``k`` counts
                matching rows.

        Returns:
            Hits sorted by descending score, ties broken by ascending id.

        Raises:
            VectorStoreError: If *k* is not positive, a filter is malformed, the query width
                differs from the column's (both widths are named), or the query fails.
        """
        name = validate_collection(collection)
        validate_k(k)
        active_filters = validate_filters(filters)
        check_dimension(len(embedding), self.dimension, context=f"collection {name!r}")

        clauses, parameters = _compile_filters(active_filters)
        where = _where(["collection = :collection", *clauses])
        statement = text(
            f'SELECT id, "text", metadata, '
            f"1 - (embedding <=> CAST(:query_vector AS vector)) AS score "
            f"FROM {VECTOR_TABLE} WHERE {where} "
            f"ORDER BY embedding <=> CAST(:query_vector AS vector) ASC "
            f"LIMIT :limit"
        )
        bound: dict[str, Any] = {
            "collection": name,
            "query_vector": _vector_literal(embedding),
            "limit": k,
            **parameters,
        }

        async with self._connect() as connection:
            result = await connection.execute(statement, bound)
            rows = result.fetchall()

        hits = [
            VectorHit(
                id=str(row[0]),
                score=_clamp(float(row[3])),
                text=str(row[1] or ""),
                metadata=_load_metadata(row[2]),
            )
            for row in rows
            if row[3] is not None
        ]
        ranked = rank_hits(hits, k)
        logger.debug("vector.pgvector.query", collection=name, returned=len(ranked), k=k)
        return ranked

    # -- lifecycle -------------------------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Return whether the schema is reachable and the extension is installed.

        Never raises — a database that is down is an operational state to report.
        """
        try:
            await self.count("__healthcheck__")
        except VectorStoreError as exc:
            logger.warning("vector.pgvector.healthcheck_failed", error=str(exc))
            return False
        return True

    async def close(self) -> None:
        """Release this store's hold on the engine.

        The engine itself is **not** disposed: it is shared with the ORM and owned by
        :mod:`app.database.session`, which disposes it at application shutdown. Only the
        "schema already created" memo is dropped, so a later use re-verifies it.
        """
        async with self._lock:
            self._schema_ready = False

    # -- connection management ------------------------------------------------------------------

    def _resolve_engine(self) -> AsyncEngine:
        """Return the engine to use, importing the application's one on first need.

        The import is deferred because :mod:`app.database.session` builds its engine at
        import time; doing it here keeps ``import app.knowledge.vector`` free of that side
        effect.

        Returns:
            The :class:`~sqlalchemy.ext.asyncio.AsyncEngine` backing this store.
        """
        if self._engine is None:
            from app.database.session import engine

            self._engine = engine
        return self._engine

    def _connect(self) -> _ConnectionContext:
        """Return an async context manager yielding a read connection, schema ensured."""
        return _ConnectionContext(self, transactional=False)

    def _begin(self) -> _ConnectionContext:
        """Return an async context manager yielding a write transaction, schema ensured."""
        return _ConnectionContext(self, transactional=True)

    async def ensure_schema(self) -> None:
        """Create the extension, table and indexes if they are not already present.

        Idempotent and guarded by a lock, so concurrent first queries issue the DDL once.
        Safe to call explicitly from a bootstrap path; every operation calls it anyway.

        Raises:
            VectorStoreError: If the backend is not PostgreSQL, if ``CREATE EXTENSION
                vector`` fails, or if the DDL cannot be applied. The message always names
                :data:`FALLBACK_ADVICE`, because the operator's next step is the same in
                every case.
        """
        if self._schema_ready:
            return
        async with self._lock:
            if self._schema_ready:
                return
            engine = self._resolve_engine()
            self._require_postgres(engine)
            dimension = self._require_dimension()
            try:
                async with engine.begin() as connection:
                    for statement in _schema_statements(dimension):
                        await connection.execute(text(statement))
            except SQLAlchemyError as exc:
                raise VectorStoreError(
                    f"pgvector is unavailable ({exc.__class__.__name__}: {exc}). "
                    f"Install the extension for this database, or {FALLBACK_ADVICE}."
                ) from exc
            self._schema_ready = True
            logger.info(
                "vector.pgvector.ready",
                table=VECTOR_TABLE,
                dim=dimension,
                ivfflat=dimension <= IVFFLAT_MAX_DIM,
                package_installed=_pgvector_package_available(),
            )

    def _require_dimension(self) -> int:
        """Return ``settings.embedding_dim``, rejecting a value that cannot type a column.

        Returns:
            The configured embedding width.

        Raises:
            VectorStoreError: If it is not a positive integer.
        """
        dimension = self.dimension
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise VectorStoreError(f"embedding_dim must be a positive int, got {dimension!r}")
        return dimension

    @staticmethod
    def _require_postgres(engine: AsyncEngine) -> None:
        """Raise unless *engine* addresses a PostgreSQL-compatible backend.

        Args:
            engine: The engine about to be used.

        Raises:
            VectorStoreError: On any other dialect. Without this the first statement would
                fail with a syntax error about ``vector``, which tells an operator nothing.
        """
        dialect = engine.dialect.name
        if dialect not in POSTGRES_DIALECT_NAMES:
            raise VectorStoreError(
                f"PgVectorStore requires PostgreSQL but the configured database is "
                f"{dialect!r}; {FALLBACK_ADVICE}."
            )


class _ConnectionContext:
    """Async context manager that ensures the schema, then yields a connection.

    Wrapping SQLAlchemy's own ``connect()``/``begin()`` context managers keeps
    "make sure the schema exists" and "translate driver errors" in one place instead of
    repeated in all six operations.

    Args:
        store: The store whose engine and schema are used.
        transactional: ``True`` to open a transaction that commits on clean exit
            (``engine.begin()``), ``False`` for a plain read connection.
    """

    __slots__ = ("_context", "_store", "_transactional")

    def __init__(self, store: PgVectorStore, *, transactional: bool) -> None:
        """Record what kind of connection to open."""
        self._store = store
        self._transactional = transactional
        self._context: Any = None

    async def __aenter__(self) -> AsyncConnection:
        """Ensure the schema and open the connection.

        Returns:
            A live :class:`~sqlalchemy.ext.asyncio.AsyncConnection`.

        Raises:
            VectorStoreError: If the schema cannot be ensured or the connection refused.
        """
        await self._store.ensure_schema()
        engine = self._store._resolve_engine()
        self._context = engine.begin() if self._transactional else engine.connect()
        try:
            return await self._context.__aenter__()
        except (SQLAlchemyError, OSError) as exc:
            self._context = None
            raise VectorStoreError(f"cannot reach the vector database: {exc}") from exc

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """Close the connection, translating driver failures into ``VectorStoreError``.

        Two translations happen here so no operation has to repeat them: a failure while
        committing or closing, and a :class:`~sqlalchemy.exc.SQLAlchemyError` raised by a
        statement *inside* the block. Both surface to the caller as a
        :class:`~app.knowledge.vector.base.VectorStoreError` with the original chained.

        Returns:
            ``False`` — an exception from inside the block is never suppressed, only
            re-raised in the abstraction's own type.

        Raises:
            VectorStoreError: On any driver-level failure.
        """
        context = self._context
        self._context = None
        if context is None:
            return False

        try:
            await context.__aexit__(exc_type, exc, traceback)
        except SQLAlchemyError as error:
            if exc_type is None:
                raise VectorStoreError(f"vector database operation failed: {error}") from error
            logger.warning("vector.pgvector.teardown_failed", error=str(error))

        if exc_type is not None and issubclass(exc_type, SQLAlchemyError):
            raise VectorStoreError(f"vector database operation failed: {exc}") from exc
        return False


def _clamp(score: float) -> float:
    """Clamp *score* into ``[SCORE_MIN, SCORE_MAX]``."""
    return max(SCORE_MIN, min(SCORE_MAX, score))
