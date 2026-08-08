"""Durable single-file vector store for the lightweight install.

This is the backend ``SQLITE_MODE=true`` selects (``app/config/settings.py`` forces
``vector_store="sqlite_vec"``), and the one a desktop user actually runs: one file under
``settings.data_path``, no server, no extension required.

Two modes, one schema
---------------------

The store always keeps the same rows in the same plain table, with embeddings held as
little-endian ``float32`` BLOBs. What changes is *who computes the distance*:

``extension``
    The optional `sqlite-vec <https://github.com/asg017/sqlite-vec>`_ extension is
    importable and loads into the connection. Ranking then happens inside SQLite via
    ``vec_distance_cosine``, in vectorised C, with the metadata filter compiled into the
    same ``WHERE`` clause — SQLite streams the candidates and only the top ``k`` rows ever
    cross into Python.

``fallback``
    No extension. The filtered candidate rows are read out and scored with
    :func:`~app.knowledge.vector.base.cosine_similarity` in pure Python. **This is a real
    implementation, not a degraded stub** — it returns exactly the same hits in exactly the
    same order, just slower. It is the default install path, so it has to be.

Because both modes share one storage format, installing or removing ``sqlite-vec`` never
requires a re-index: the same file works either way. The selected mode is logged once at
INFO, when the connection is first opened.

Correctness notes
-----------------

* ``vec_distance_cosine`` returns a cosine *distance*; it is converted to the similarity
  every backend reports with ``1 - distance`` and clamped to ``[-1, 1]``.
* Filters are compiled into the ``WHERE`` clause so ``k`` counts *matching* records. The
  reserved keys ``user_id`` and ``kind`` are promoted out of the metadata JSON into indexed
  columns; every other key is read with ``json_extract``. **Filter values are always bound
  parameters** — nothing derived from caller data is ever interpolated into SQL.
* Every record in a collection must share one embedding width; the width is stored per row
  and re-checked on write and on query, so a re-embedding with a different model is
  reported rather than silently returning nonsense.

Concurrency
-----------

One :class:`sqlite3.Connection` is opened lazily and every operation runs on it inside
:func:`asyncio.to_thread`, serialised by an :class:`asyncio.Lock`. The lock is what makes
sharing a single connection across worker threads safe; the thread offload is what keeps
the event loop from blocking on disk. WAL journalling plus a busy timeout let a second
process (a Celery worker, say) read while this one writes.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from array import array
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, ClassVar, Final, TypeVar

import structlog

from app.config.settings import Settings, get_settings
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
    cosine_similarity,
    normalize_filter_value,
    rank_hits,
    validate_collection,
    validate_filters,
    validate_k,
)

__all__ = [
    "MODE_EXTENSION",
    "MODE_FALLBACK",
    "VECTOR_DATABASE_FILENAME",
    "VECTOR_TABLE",
    "SqliteVecStore",
]

logger = structlog.get_logger(__name__)

_T = TypeVar("_T")

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Filename of the vector database, created under ``settings.data_path``. Kept separate
#: from ``applicantos.db`` so a re-index can delete the whole index without touching
#: application data, and so vector writes never contend with ORM writes for the same lock.
VECTOR_DATABASE_FILENAME: Final[str] = "vectors.db"

#: The single table holding every collection. Same name as the pgvector backend's table, so
#: an operator moving between installs recognises it.
VECTOR_TABLE: Final[str] = "knowledge_vectors"

#: Distances are computed by the ``sqlite-vec`` extension inside SQLite.
MODE_EXTENSION: Final[str] = "extension"

#: Distances are computed in pure Python over the filtered candidate rows.
MODE_FALLBACK: Final[str] = "fallback"

#: How long SQLite waits for a lock held by another connection before giving up.
BUSY_TIMEOUT_MS: Final[int] = 30_000

#: Ids per ``DELETE ... IN (...)`` statement. Comfortably under SQLite's default
#: ``SQLITE_MAX_VARIABLE_NUMBER`` (999 on older builds) with room for the collection bind.
DELETE_BATCH_SIZE: Final[int] = 500

#: ``array("f")`` is IEEE-754 single precision, 4 bytes per component, which is the layout
#: ``sqlite-vec`` expects for a ``float[n]`` BLOB.
FLOAT32_TYPECODE: Final[str] = "f"

#: Whether this machine already stores ``float32`` little-endian. When it does not, buffers
#: are byte-swapped on the way in and out so the file stays portable and readable by
#: ``sqlite-vec``.
_IS_LITTLE_ENDIAN: Final[bool] = sys.byteorder == "little"

#: PRAGMAs applied to the connection, in order. WAL plus ``NORMAL`` synchronous is the
#: standard durability/throughput trade for a single-user desktop database, and matches
#: what :mod:`app.database.session` does for the main file.
_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
)

_CREATE_SCHEMA_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} (
    collection TEXT    NOT NULL,
    record_id  TEXT    NOT NULL,
    user_id    TEXT,
    kind       TEXT,
    metadata   TEXT    NOT NULL DEFAULT '{{}}',
    "text"     TEXT    NOT NULL DEFAULT '',
    embedding  BLOB    NOT NULL,
    dim        INTEGER NOT NULL,
    updated_at REAL    NOT NULL,
    PRIMARY KEY (collection, record_id)
);
CREATE INDEX IF NOT EXISTS ix_{VECTOR_TABLE}_collection_user
    ON {VECTOR_TABLE} (collection, user_id);
CREATE INDEX IF NOT EXISTS ix_{VECTOR_TABLE}_collection_kind
    ON {VECTOR_TABLE} (collection, kind);
"""

_UPSERT_SQL: Final[str] = f"""
INSERT INTO {VECTOR_TABLE}
    (collection, record_id, user_id, kind, metadata, "text", embedding, dim, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (collection, record_id) DO UPDATE SET
    user_id    = excluded.user_id,
    kind       = excluded.kind,
    metadata   = excluded.metadata,
    "text"     = excluded."text",
    embedding  = excluded.embedding,
    dim        = excluded.dim,
    updated_at = excluded.updated_at
"""

_COLLECTION_DIM_SQL: Final[str] = f"SELECT dim FROM {VECTOR_TABLE} WHERE collection = ? LIMIT 1"

#: Probe vector used to confirm the loaded extension really answers distance queries.
_PROBE_VECTOR: Final[tuple[float, ...]] = (1.0, 0.0)


# --------------------------------------------------------------------------------------
# Encoding helpers
# --------------------------------------------------------------------------------------


def _encode_embedding(embedding: Sequence[float]) -> bytes:
    """Pack *embedding* into a little-endian ``float32`` BLOB.

    Args:
        embedding: The vector to store.

    Returns:
        ``4 * len(embedding)`` bytes, in the layout ``sqlite-vec`` reads directly.
    """
    buffer = array(FLOAT32_TYPECODE, (float(component) for component in embedding))
    if not _IS_LITTLE_ENDIAN:  # pragma: no cover - no big-endian CI target
        buffer.byteswap()
    return buffer.tobytes()


def _decode_embedding(blob: bytes) -> list[float]:
    """Unpack a ``float32`` BLOB written by :func:`_encode_embedding`.

    Args:
        blob: Raw column bytes.

    Returns:
        The vector as a list of floats.

    Raises:
        VectorStoreError: If the blob length is not a whole number of components, which
            means the row was written by something other than this store.
    """
    buffer = array(FLOAT32_TYPECODE)
    try:
        buffer.frombytes(blob)
    except ValueError as exc:
        raise VectorStoreError(f"corrupt embedding blob of {len(blob)} bytes") from exc
    if not _IS_LITTLE_ENDIAN:  # pragma: no cover - no big-endian CI target
        buffer.byteswap()
    return buffer.tolist()


def _bind_scalar(value: Any) -> Any:
    """Return *value* in a form :mod:`sqlite3` can bind.

    Normalises :class:`uuid.UUID` to text and collapses ``str``/``int`` subclasses — which
    is what every :class:`enum.StrEnum` member is — to their base type, so a caller may
    pass ``SourceKind.RESUME`` or a ``UUID`` straight into a filter.

    Args:
        value: A scalar filter value.

    Returns:
        ``None``, ``str``, ``int``, ``float`` or ``bytes``.

    Raises:
        VectorStoreError: If *value* is a type SQLite cannot store, which would otherwise
            surface as an opaque ``InterfaceError`` from deep inside a query.
    """
    normalized = normalize_filter_value(value)
    if normalized is None or isinstance(normalized, (float, bytes)):
        return normalized
    if isinstance(normalized, bool):
        return int(normalized)
    if isinstance(normalized, str):
        return str(normalized)
    if isinstance(normalized, int):
        return int(normalized)
    raise VectorStoreError(
        f"unsupported filter value of type {type(value).__name__!r}: "
        "expected a string, number, boolean, UUID or None"
    )


def _reserved_column_value(metadata: dict[str, Any], key: str) -> str | None:
    """Return the text form of a reserved metadata key, or ``None`` when absent.

    Args:
        metadata: A record's metadata.
        key: One of :data:`~app.knowledge.vector.base.RESERVED_FILTER_KEYS`.

    Returns:
        The value as text — the promoted columns are declared ``TEXT`` because ``user_id``
        is a UUID string and ``kind`` an enum value in every real call — or ``None``.
    """
    value = metadata.get(key)
    if value is None:
        return None
    return str(normalize_filter_value(value))


def _json_path(key: str) -> str:
    """Return the SQLite JSON path selecting *key* from the metadata column.

    Args:
        key: A filter key already validated against
            :data:`~app.knowledge.vector.base.FILTER_KEY_PATTERN`, so it contains only
            identifier characters and needs no quoting inside the path.

    Returns:
        A ``$.key`` path expression, passed to ``json_extract`` as a bound parameter.
    """
    return f"$.{key}"


# --------------------------------------------------------------------------------------
# Filter compilation
# --------------------------------------------------------------------------------------


def _compile_filters(filters: Filter) -> tuple[list[str], list[Any]]:
    """Compile *filters* into SQL fragments and their bound parameters.

    Implements the semantics documented in :mod:`app.knowledge.vector.base`: scalars are
    exact matches, sequences are ``IN``, ``None`` matches a JSON ``null`` or an absent key,
    and an empty sequence matches nothing. Reserved keys hit their indexed columns; every
    other key is read out of the metadata JSON.

    Only ``?`` placeholders and the already-validated identifier characters of a key ever
    reach the SQL string — no value is interpolated.

    Args:
        filters: An already-validated filter (see
            :func:`~app.knowledge.vector.base.validate_filters`).

    Returns:
        A ``(clauses, parameters)`` pair to be joined with ``AND``.

    Raises:
        VectorStoreError: If a value has a type SQLite cannot bind.
    """
    clauses: list[str] = []
    parameters: list[Any] = []

    for key, expected in filters.items():
        reserved = key in RESERVED_FILTER_KEYS
        if reserved:
            expression = key
            prefix: list[Any] = []
        else:
            expression = "json_extract(metadata, ?)"
            prefix = [_json_path(key)]

        if expected is None:
            clauses.append(f"{expression} IS NULL")
            parameters.extend(prefix)
            continue

        if isinstance(expected, (list, tuple, set, frozenset)):
            options = list(expected)
            if not options:
                clauses.append("0 = 1")
                continue
            placeholders = ", ".join("?" for _ in options)
            clauses.append(f"{expression} IN ({placeholders})")
            parameters.extend(prefix)
            parameters.extend(_bind_reserved(option, reserved) for option in options)
            continue

        clauses.append(f"{expression} = ?")
        parameters.extend(prefix)
        parameters.append(_bind_reserved(expected, reserved))

    return clauses, parameters


def _bind_reserved(value: Any, reserved: bool) -> Any:
    """Bind *value*, coercing to text when it targets a promoted ``TEXT`` column.

    Args:
        value: A scalar filter value.
        reserved: Whether the comparison is against ``user_id``/``kind``, which are stored
            as text by :func:`_reserved_column_value`.

    Returns:
        The bindable value.
    """
    bound = _bind_scalar(value)
    if reserved and bound is not None:
        return str(bound)
    return bound


def _where(clauses: Sequence[str]) -> str:
    """Return a ``WHERE`` clause joining *clauses* with ``AND`` (never empty here)."""
    return " AND ".join(clauses)


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


class SqliteVecStore:
    """A durable :class:`~app.knowledge.vector.base.VectorStore` backed by one SQLite file.

    Args:
        settings: Configuration supplying ``data_path``. Defaults to the process settings.
        path: Explicit database file, overriding the default
            ``settings.data_path / vectors.db``. Tests pass a temporary path; production
            never does.

    Note:
        The database file is opened lazily on the first operation, so constructing the
        store touches neither the filesystem nor the event loop.
    """

    backend_name: ClassVar[str] = "sqlite_vec"

    def __init__(self, settings: Settings | None = None, *, path: Path | None = None) -> None:
        """Record where the database lives without opening it."""
        self._settings = settings if settings is not None else get_settings()
        self._explicit_path = path
        self._connection: sqlite3.Connection | None = None
        self._mode: str | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        """Return a description including the file path and selected mode."""
        return f"<SqliteVecStore path={self.path} mode={self._mode or 'unopened'}>"

    # -- configuration ---------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path of the vector database file.

        Reading this creates ``settings.data_path`` if it does not exist (that property is
        defined to do so), but does not create the database itself.
        """
        if self._explicit_path is not None:
            return self._explicit_path
        return self._settings.data_path / VECTOR_DATABASE_FILENAME

    @property
    def mode(self) -> str | None:
        """:data:`MODE_EXTENSION` or :data:`MODE_FALLBACK`, or ``None`` before first use."""
        return self._mode

    # -- writes -----------------------------------------------------------------------------

    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> int:
        """Insert or replace *records* in *collection*.

        Args:
            collection: Target collection, created implicitly on first write.
            records: Records to store. Empty is a no-op returning ``0``.

        Returns:
            The number of records written.

        Raises:
            VectorStoreError: If an embedding is empty, or a width disagrees with the batch
                or with the width already stored for the collection (both are named).
        """
        name = validate_collection(collection)
        if not records:
            return 0

        def operation(connection: sqlite3.Connection) -> int:
            existing = self._existing_dimension(connection, name)
            dimension = batch_dimension(records, existing)
            now = time.time()
            rows = [
                (
                    name,
                    record.id,
                    _reserved_column_value(record.metadata, FILTER_KEY_USER_ID),
                    _reserved_column_value(record.metadata, FILTER_KEY_KIND),
                    json.dumps(record.metadata, default=str),
                    record.text,
                    _encode_embedding(record.embedding),
                    dimension,
                    now,
                )
                for record in records
            ]
            with connection:
                connection.executemany(_UPSERT_SQL, rows)
            return len(rows)

        written = await self._run(operation)
        logger.debug("vector.sqlite.upsert", collection=name, records=written)
        return written

    async def delete(self, collection: str, ids: Sequence[str]) -> int:
        """Remove records by id, in batches that respect SQLite's bind-variable limit.

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

        def operation(connection: sqlite3.Connection) -> int:
            removed = 0
            with connection:
                for batch in _batched(identifiers, DELETE_BATCH_SIZE):
                    placeholders = ", ".join("?" for _ in batch)
                    cursor = connection.execute(
                        f"DELETE FROM {VECTOR_TABLE} "
                        f"WHERE collection = ? AND record_id IN ({placeholders})",
                        [name, *batch],
                    )
                    removed += cursor.rowcount
            return removed

        removed = await self._run(operation)
        logger.debug("vector.sqlite.delete", collection=name, removed=removed)
        return removed

    async def clear(self, collection: str) -> int:
        """Delete every row of *collection*.

        Args:
            collection: Collection to empty.

        Returns:
            The number of rows removed.
        """
        name = validate_collection(collection)

        def operation(connection: sqlite3.Connection) -> int:
            with connection:
                cursor = connection.execute(
                    f"DELETE FROM {VECTOR_TABLE} WHERE collection = ?", (name,)
                )
            return cursor.rowcount

        removed = await self._run(operation)
        logger.debug("vector.sqlite.clear", collection=name, removed=removed)
        return removed

    # -- reads -------------------------------------------------------------------------------

    async def count(self, collection: str) -> int:
        """Return how many records *collection* holds (``0`` when unknown)."""
        name = validate_collection(collection)

        def operation(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {VECTOR_TABLE} WHERE collection = ?", (name,)
            ).fetchone()
            return int(row[0]) if row else 0

        return await self._run(operation)

    async def list_collections(self) -> list[str]:
        """Return the names of all non-empty collections, sorted ascending."""

        def operation(connection: sqlite3.Connection) -> list[str]:
            rows = connection.execute(
                f"SELECT DISTINCT collection FROM {VECTOR_TABLE} ORDER BY collection"
            ).fetchall()
            return [str(row[0]) for row in rows]

        return await self._run(operation)

    async def query(
        self,
        collection: str,
        embedding: Sequence[float],
        *,
        k: int = DEFAULT_TOP_K,
        filters: Filter | None = None,
    ) -> list[VectorHit]:
        """Return the *k* records in *collection* most similar to *embedding*.

        In :data:`MODE_EXTENSION` the ranking is done by ``vec_distance_cosine`` inside
        SQLite and only ``k`` rows are materialised. In :data:`MODE_FALLBACK` the filtered
        candidates are scored in Python. Both produce identical results.

        Args:
            collection: Collection to search. Unknown or empty yields ``[]``.
            embedding: Query vector, of the collection's width.
            k: Maximum number of hits.
            filters: Metadata filter applied in SQL, before ranking.

        Returns:
            Hits sorted by descending score, ties broken by ascending id.

        Raises:
            VectorStoreError: If *k* is not positive, a filter is malformed, or the query
                width differs from the collection's (both widths are named).
        """
        name = validate_collection(collection)
        validate_k(k)
        active_filters = validate_filters(filters)
        clauses, parameters = _compile_filters(active_filters)
        query_vector = [float(component) for component in embedding]

        def operation(connection: sqlite3.Connection) -> list[VectorHit]:
            dimension = self._existing_dimension(connection, name)
            if dimension is None:
                return []
            check_dimension(len(query_vector), dimension, context=f"collection {name!r}")

            base = ["collection = ?", "dim = ?"]
            base_parameters: list[Any] = [name, dimension]
            where = _where([*base, *clauses])

            if self._mode == MODE_EXTENSION:
                return self._query_with_extension(
                    connection, where, base_parameters, parameters, query_vector, k
                )
            return self._query_in_python(
                connection, where, base_parameters, parameters, query_vector, k
            )

        hits = await self._run(operation)
        logger.debug(
            "vector.sqlite.query", collection=name, returned=len(hits), k=k, mode=self._mode
        )
        return hits

    # -- lifecycle ------------------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying connection, if one was ever opened.

        Safe to call more than once. The next operation reopens the file and re-probes for
        the extension.
        """
        async with self._lock:
            connection = self._connection
            self._connection = None
            self._mode = None
            if connection is not None:
                await asyncio.to_thread(connection.close)
                logger.debug("vector.sqlite.closed", path=str(self.path))

    async def healthcheck(self) -> bool:
        """Return whether the database file can be opened and queried.

        Never raises: an unopenable file is an operational state to report, not an
        exception to propagate into a readiness probe.
        """
        try:
            await self._run(lambda connection: connection.execute("SELECT 1").fetchone())
        except VectorStoreError as exc:
            logger.warning("vector.sqlite.healthcheck_failed", error=str(exc))
            return False
        return True

    # -- query implementations ---------------------------------------------------------------------

    def _query_with_extension(
        self,
        connection: sqlite3.Connection,
        where: str,
        base_parameters: Sequence[Any],
        filter_parameters: Sequence[Any],
        query_vector: Sequence[float],
        k: int,
    ) -> list[VectorHit]:
        """Rank inside SQLite using ``sqlite-vec``'s cosine distance function.

        Args:
            connection: The open connection, with the extension loaded.
            where: The compiled ``WHERE`` clause.
            base_parameters: Binds for the collection and dimension predicates.
            filter_parameters: Binds produced by :func:`_compile_filters`.
            query_vector: The query embedding.
            k: Maximum number of hits.

        Returns:
            Up to *k* hits, best first.
        """
        sql = (
            f'SELECT record_id, "text", metadata, '
            f"vec_distance_cosine(embedding, ?) AS distance "
            f"FROM {VECTOR_TABLE} WHERE {where} "
            f"ORDER BY distance ASC LIMIT ?"
        )
        rows = connection.execute(
            sql,
            [
                _encode_embedding(query_vector),
                *base_parameters,
                *filter_parameters,
                k,
            ],
        ).fetchall()
        hits = [
            VectorHit(
                id=str(row[0]),
                score=_similarity_from_distance(row[3]),
                text=str(row[1]),
                metadata=_load_metadata(row[2]),
            )
            for row in rows
            if row[3] is not None
        ]
        return rank_hits(hits, k)

    def _query_in_python(
        self,
        connection: sqlite3.Connection,
        where: str,
        base_parameters: Sequence[Any],
        filter_parameters: Sequence[Any],
        query_vector: Sequence[float],
        k: int,
    ) -> list[VectorHit]:
        """Rank in Python over the rows SQLite has already filtered.

        Args:
            connection: The open connection.
            where: The compiled ``WHERE`` clause.
            base_parameters: Binds for the collection and dimension predicates.
            filter_parameters: Binds produced by :func:`_compile_filters`.
            query_vector: The query embedding.
            k: Maximum number of hits.

        Returns:
            Up to *k* hits, best first — the same answer the extension path gives.
        """
        sql = f'SELECT record_id, "text", metadata, embedding FROM {VECTOR_TABLE} WHERE {where}'
        cursor = connection.execute(sql, [*base_parameters, *filter_parameters])
        hits = [
            VectorHit(
                id=str(row[0]),
                score=cosine_similarity(query_vector, _decode_embedding(row[3])),
                text=str(row[1]),
                metadata=_load_metadata(row[2]),
            )
            for row in cursor
        ]
        return rank_hits(hits, k)

    # -- connection management ---------------------------------------------------------------------

    async def _run(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run *operation* against the connection, off the event loop.

        Serialised by :attr:`_lock` so exactly one thread ever touches the connection, and
        offloaded with :func:`asyncio.to_thread` so disk I/O never blocks the loop.

        Args:
            operation: A synchronous callable taking the connection.

        Returns:
            Whatever *operation* returns.

        Raises:
            VectorStoreError: Wrapping any :class:`sqlite3.Error` or :class:`OSError`, so
                callers never have to import :mod:`sqlite3` to handle a failure.
        """
        async with self._lock:
            connection = await self._connect_locked()
            try:
                return await asyncio.to_thread(operation, connection)
            except (sqlite3.Error, OSError) as exc:
                raise VectorStoreError(
                    f"sqlite vector store operation failed on {self.path}: {exc}"
                ) from exc

    async def _connect_locked(self) -> sqlite3.Connection:
        """Return the open connection, opening and initialising it on first call.

        Must be called with :attr:`_lock` held.

        Returns:
            The live :class:`sqlite3.Connection`.

        Raises:
            VectorStoreError: If the database file cannot be opened or the schema cannot be
                created.
        """
        if self._connection is not None:
            return self._connection
        try:
            connection, mode = await asyncio.to_thread(self._open)
        except (sqlite3.Error, OSError) as exc:
            raise VectorStoreError(
                f"cannot open sqlite vector store at {self.path}: {exc}"
            ) from exc
        self._connection = connection
        self._mode = mode
        logger.info(
            "vector.sqlite.ready",
            path=str(self.path),
            mode=mode,
            sqlite_version=sqlite3.sqlite_version,
        )
        return connection

    def _open(self) -> tuple[sqlite3.Connection, str]:
        """Open the database, apply PRAGMAs, load the extension, create the schema.

        Runs entirely on a worker thread. ``check_same_thread=False`` is required because
        :func:`asyncio.to_thread` hands the connection to whichever thread the executor
        picks; :attr:`_lock` supplies the mutual exclusion that setting gives up.

        Returns:
            The connection and the mode it will operate in.
        """
        database_path = self.path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database_path), check_same_thread=False)
        try:
            for pragma in _PRAGMAS:
                connection.execute(pragma)
            mode = MODE_EXTENSION if _load_sqlite_vec(connection) else MODE_FALLBACK
            connection.executescript(_CREATE_SCHEMA_SQL)
        except (sqlite3.Error, OSError):
            connection.close()
            raise
        return connection, mode

    @staticmethod
    def _existing_dimension(connection: sqlite3.Connection, collection: str) -> int | None:
        """Return the embedding width already stored for *collection*, or ``None``.

        Args:
            connection: The open connection.
            collection: Collection name.

        Returns:
            The width taken from any row of the collection — every row shares it, because
            :func:`~app.knowledge.vector.base.batch_dimension` enforces that on write — or
            ``None`` when the collection holds no rows.
        """
        row = connection.execute(_COLLECTION_DIM_SQL, (collection,)).fetchone()
        return int(row[0]) if row is not None else None


# --------------------------------------------------------------------------------------
# Module helpers
# --------------------------------------------------------------------------------------


def _similarity_from_distance(distance: float) -> float:
    """Convert a cosine *distance* into the similarity every backend reports.

    Args:
        distance: ``vec_distance_cosine`` output, nominally in ``[0, 2]``.

    Returns:
        ``1 - distance``, clamped into ``[-1, 1]``.
    """
    score = SCORE_MAX - float(distance)
    return max(SCORE_MIN, min(SCORE_MAX, score))


def _load_metadata(raw: str | None) -> dict[str, Any]:
    """Decode a stored metadata column.

    Args:
        raw: The JSON text from the ``metadata`` column.

    Returns:
        The decoded mapping, or an empty dict when the column is NULL, unparseable, or
        holds a non-object. A single malformed row must not fail a whole search.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("vector.sqlite.metadata_unreadable")
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _batched(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    """Yield consecutive slices of *items* of at most *size* elements."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _load_sqlite_vec(connection: sqlite3.Connection) -> bool:
    """Try to load the ``sqlite-vec`` extension into *connection*.

    Every failure mode is expected rather than exceptional: the package may not be
    installed, the interpreter may have been built without loadable-extension support, or
    the shared library may not match the platform. Each is caught, logged at debug, and
    turned into ``False`` so the caller runs in :data:`MODE_FALLBACK`.

    Args:
        connection: A freshly opened connection.

    Returns:
        ``True`` only when the extension loaded *and* answered a real distance query.
    """
    try:
        import sqlite_vec
    except ImportError:
        logger.debug("vector.sqlite.extension_unavailable", reason="not_installed")
        return False

    if not hasattr(connection, "enable_load_extension"):  # pragma: no cover - build dependent
        logger.debug("vector.sqlite.extension_unavailable", reason="loading_disabled")
        return False

    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    except (AttributeError, sqlite3.Error, OSError) as exc:
        logger.debug("vector.sqlite.extension_unavailable", reason="load_failed", error=str(exc))
        return False
    finally:
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):  # pragma: no cover - build dependent
            pass

    probe = _encode_embedding(_PROBE_VECTOR)
    try:
        connection.execute("SELECT vec_distance_cosine(?, ?)", (probe, probe)).fetchone()
    except sqlite3.Error as exc:
        logger.debug("vector.sqlite.extension_unavailable", reason="probe_failed", error=str(exc))
        return False
    return True
