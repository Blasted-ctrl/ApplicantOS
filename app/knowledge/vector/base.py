"""Vector store protocol, records, filter semantics, and numpy-free math.

This module is the boundary every other part of ApplicantOS uses to talk to a vector
index. Three backends implement it — :class:`~app.knowledge.vector.pgvector.PgVectorStore`,
:class:`~app.knowledge.vector.sqlite_vec.SqliteVecStore` and
:class:`~app.knowledge.vector.memory_store.InMemoryVectorStore` — and callers pick one
through :func:`app.knowledge.vector.get_vector_store`, never by importing a concrete class.

Nothing here imports numpy, SQLAlchemy, or any optional dependency: importing this module
must succeed on a bare interpreter, because the in-memory backend built on top of it is the
one that always works (``docs/CONTRACTS.md`` §8.2).

Collections and identifiers
---------------------------

A *collection* is a flat namespace for records — ``"chunks"``, ``"facts"``, ``"memories"``.
It is a value, not a table: every backend keeps one physical store and partitions it by
collection, so adding a collection costs nothing and needs no migration.

Record ids should be **globally unique**, not merely unique within their collection. The
pgvector table is keyed by ``id`` alone (§8.2 fixes that schema), so re-upserting an id under
a different collection *moves* the record there, while the SQLite and in-memory backends key
on ``(collection, id)`` and would hold two copies. Chunk, fact and memory UUIDs satisfy this
naturally; see ``docs/OPEN_QUESTIONS.md``.

Filter semantics (identical on all three backends)
--------------------------------------------------

``filters`` is a :data:`Filter` — a plain ``dict[str, Any]`` read against
:attr:`VectorRecord.metadata`. Every entry must match for a record to be a candidate
(entries are ANDed), and the value decides the comparison:

=====================  ====================================================================
Value shape            Meaning
=====================  ====================================================================
scalar                 Exact match. ``{"kind": "resume"}`` keeps records whose metadata
                       ``kind`` equals ``"resume"``. :class:`uuid.UUID` is compared by its
                       canonical string form, so an id may be passed either way.
``list``/``tuple``/    ``IN``. ``{"kind": ["resume", "readme"]}`` keeps records matching any
``set``/``frozenset``  member. An **empty** collection matches nothing.
``None``               Matches records whose metadata value is JSON ``null`` *or* whose
                       metadata has no such key at all.
=====================  ====================================================================

Two keys are **reserved** because every backend indexes them:

``user_id``
    The owner of the record. Knowledge is per-user and retrieval must never cross users, so
    in practice every query passes this.
``kind``
    A :class:`~app.models.enums.SourceKind`, :class:`~app.models.enums.FactKind` or similar
    discriminator, letting a caller narrow a collection to one flavour of content.

Reserved keys are read from ``record.metadata`` exactly like any other key — callers always
put them there. The SQL backends additionally *promote* them to real indexed columns so the
common query shape uses an index instead of a JSON scan; that promotion is invisible here.

Filter keys must be plain identifiers (:data:`FILTER_KEY_PATTERN`). The SQL backends compile
keys into JSON paths and column names, and restricting the character set is what lets them
do so while still binding every filter *value* as a parameter — no backend ever builds SQL
by interpolating a value.

Scoring
-------

Every backend returns **cosine similarity in ``[-1.0, 1.0]``**, higher is better, sorted
descending. pgvector's ``<=>`` yields a distance and is converted with ``1 - distance``; the
pure-Python backends use :func:`cosine_similarity` (or the equivalent pre-normalised
:func:`dot`). A zero vector scores ``0.0`` against everything rather than raising — an
embedder that produced an all-zero vector for an empty string should not take down a query.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeAlias, runtime_checkable

__all__ = [
    "DEFAULT_TOP_K",
    "FILTER_KEY_KIND",
    "FILTER_KEY_PATTERN",
    "FILTER_KEY_USER_ID",
    "RESERVED_FILTER_KEYS",
    "SCORE_MAX",
    "SCORE_MIN",
    "ZERO_VECTOR_SCORE",
    "Filter",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
    "VectorStoreError",
    "batch_dimension",
    "check_dimension",
    "cosine_similarity",
    "dot",
    "matches_filters",
    "normalize",
    "normalize_filter_value",
    "rank_hits",
    "validate_collection",
    "validate_filters",
    "validate_k",
]

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Default neighbour count for :meth:`VectorStore.query`, per ``docs/CONTRACTS.md`` §8.2.
DEFAULT_TOP_K: Final[int] = 10

#: Reserved filter key naming the owning user. Promoted to an indexed column by the SQL
#: backends. Retrieval is per-user, so essentially every query carries it.
FILTER_KEY_USER_ID: Final[str] = "user_id"

#: Reserved filter key naming the content discriminator (a ``SourceKind``/``FactKind``
#: value in practice). Promoted to an indexed column by the SQL backends.
FILTER_KEY_KIND: Final[str] = "kind"

#: The reserved keys, in the column order the SQL backends use.
RESERVED_FILTER_KEYS: Final[tuple[str, ...]] = (FILTER_KEY_USER_ID, FILTER_KEY_KIND)

#: Filter (and therefore metadata) keys must be plain identifiers. The SQL backends turn a
#: key into a JSON path or a column reference, and neither can be a bound parameter in every
#: dialect; constraining the charset is what keeps that safe without ever interpolating a
#: *value*.
FILTER_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Lower bound of a cosine similarity score.
SCORE_MIN: Final[float] = -1.0

#: Upper bound of a cosine similarity score.
SCORE_MAX: Final[float] = 1.0

#: Score returned when either side of a comparison is the zero vector. Cosine is undefined
#: there; ``0.0`` (orthogonal) is the only answer that keeps ranking sane and never divides
#: by zero.
ZERO_VECTOR_SCORE: Final[float] = 0.0

#: Value types accepted as an ``IN`` filter. Spelled out one class at a time rather than as
#: ``tuple[type, ...]`` so that an ``isinstance`` test against it narrows the value to
#: "something iterable" instead of leaving it as ``object``.
_SEQUENCE_FILTER_TYPES: Final[
    tuple[type[list[Any]], type[tuple[Any, ...]], type[set[Any]], type[frozenset[Any]]]
] = (list, tuple, set, frozenset)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class VectorStoreError(RuntimeError):
    """Raised for any vector-store failure a caller could reasonably act on.

    Covers dimension mismatches, malformed filters, unusable collections, and backend
    configuration problems (a missing PostgreSQL extension, an unopenable database file).
    Backend-specific exceptions — :class:`sqlite3.Error`,
    :class:`sqlalchemy.exc.SQLAlchemyError` — are wrapped in this type so callers depend on
    the abstraction rather than on which store happens to be configured.
    """


# --------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class VectorRecord:
    """One embedded piece of text, ready to be stored.

    Attributes:
        id: Stable identifier for the record. Re-upserting the same id overwrites in place,
            which is what makes indexing idempotent. Should be globally unique — see the
            module docstring.
        embedding: The dense vector. Every record in a collection must share one width; the
            first record written to a collection fixes it.
        text: The text the embedding was computed from. Returned verbatim on a hit so a
            caller can build a prompt without a second database round trip.
        metadata: Arbitrary JSON-serialisable annotations, and the only thing ``filters``
            examines. Put ``user_id`` and ``kind`` here — see :data:`RESERVED_FILTER_KEYS`.
    """

    id: str
    embedding: list[float]
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dimension(self) -> int:
        """Width of :attr:`embedding`."""
        return len(self.embedding)

    def to_hit(self, score: float) -> VectorHit:
        """Return the :class:`VectorHit` this record produces at *score*.

        Args:
            score: Cosine similarity against the query vector.

        Returns:
            A hit carrying this record's id, text and metadata. The metadata dict is shared
            rather than copied — treat a hit as read-only.
        """
        return VectorHit(id=self.id, score=score, text=self.text, metadata=self.metadata)


@dataclass(slots=True)
class VectorHit:
    """One search result.

    Attributes:
        id: The matching record's :attr:`VectorRecord.id`.
        score: Cosine similarity in ``[-1.0, 1.0]``, higher is more similar. Every backend
            normalises to this scale, so a threshold is portable across them.
        text: The matching record's text.
        metadata: The matching record's metadata.
    """

    id: str
    score: float
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


#: A metadata filter. See the module docstring for the full semantics.
Filter: TypeAlias = dict[str, Any]


# --------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------


@runtime_checkable
class VectorStore(Protocol):
    """The vector index interface, per ``docs/CONTRACTS.md`` §8.2.

    Implementations are async because two of the three do real I/O. All six methods are
    idempotent in the sense that matters for indexing: upserting the same record twice
    leaves one row, deleting an absent id is not an error, and querying an unknown
    collection returns ``[]`` rather than raising.
    """

    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> int:
        """Insert or replace *records* in *collection*.

        Args:
            collection: Target collection; created implicitly on first write.
            records: Records to store. An empty sequence is a no-op.

        Returns:
            The number of records written.

        Raises:
            VectorStoreError: If a record's embedding is empty, or its width disagrees with
                the width already established for the collection.
        """
        ...

    async def query(
        self,
        collection: str,
        embedding: Sequence[float],
        *,
        k: int = DEFAULT_TOP_K,
        filters: Filter | None = None,
    ) -> list[VectorHit]:
        """Return the *k* records in *collection* most similar to *embedding*.

        Args:
            collection: Collection to search. An unknown collection yields ``[]``.
            embedding: The query vector, of the collection's width.
            k: Maximum number of hits. Fewer are returned when the filtered candidate set is
                smaller; no backend pads the result.
            filters: Metadata filter applied *before* ranking, so ``k`` counts matching
                records. See the module docstring.

        Returns:
            Hits sorted by descending score, ties broken by ascending id for determinism.

        Raises:
            VectorStoreError: If *k* is not positive, the query width disagrees with the
                collection, or a filter is malformed.
        """
        ...

    async def delete(self, collection: str, ids: Sequence[str]) -> int:
        """Remove records by id.

        Args:
            collection: Collection to delete from.
            ids: Record ids. Unknown ids are ignored; an empty sequence is a no-op.

        Returns:
            The number of records actually removed.
        """
        ...

    async def count(self, collection: str) -> int:
        """Return how many records *collection* holds (``0`` when it does not exist)."""
        ...

    async def clear(self, collection: str) -> int:
        """Delete every record in *collection*.

        The collection ceases to exist: it disappears from :meth:`list_collections` and
        forgets its embedding width, so the next :meth:`upsert` may use a different one.

        Args:
            collection: Collection to empty.

        Returns:
            The number of records removed.
        """
        ...

    async def list_collections(self) -> list[str]:
        """Return the names of all non-empty collections, sorted ascending."""
        ...


# --------------------------------------------------------------------------------------
# Validation helpers (shared by every backend, so the errors read identically)
# --------------------------------------------------------------------------------------


def validate_collection(collection: str) -> str:
    """Return *collection* stripped of surrounding whitespace, rejecting empty names.

    Args:
        collection: The caller-supplied collection name.

    Returns:
        The normalised name.

    Raises:
        VectorStoreError: If *collection* is not a non-empty string. A blank name would
            silently become a catch-all partition holding unrelated records.
    """
    if not isinstance(collection, str):
        raise VectorStoreError(f"collection must be a string, got {type(collection).__name__!r}")
    normalized = collection.strip()
    if not normalized:
        raise VectorStoreError("collection must be a non-empty string")
    return normalized


def validate_k(k: int) -> int:
    """Return *k* after checking it is a usable neighbour count.

    Args:
        k: Requested number of hits.

    Returns:
        *k* unchanged.

    Raises:
        VectorStoreError: If *k* is not a positive integer. Returning ``[]`` instead would
            hide the arithmetic bug that produced a zero or negative ``k``.
    """
    if isinstance(k, bool) or not isinstance(k, int):
        raise VectorStoreError(f"k must be an int, got {type(k).__name__!r}")
    if k <= 0:
        raise VectorStoreError(f"k must be positive, got {k}")
    return k


def validate_filters(filters: Filter | None) -> Filter:
    """Return *filters* as a dict, rejecting keys the SQL backends cannot compile.

    Args:
        filters: The caller-supplied filter, or ``None``.

    Returns:
        The filter itself, or an empty dict when ``None`` was given.

    Raises:
        VectorStoreError: If *filters* is not a mapping, or any key is not a plain
            identifier (:data:`FILTER_KEY_PATTERN`).
    """
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise VectorStoreError(f"filters must be a dict, got {type(filters).__name__!r}")
    for key in filters:
        if not isinstance(key, str) or not FILTER_KEY_PATTERN.match(key):
            raise VectorStoreError(
                f"unsupported filter key {key!r}: keys must match {FILTER_KEY_PATTERN.pattern!r}"
            )
    return filters


def _require_dimension(record: VectorRecord) -> int:
    """Return *record*'s embedding width, rejecting an empty vector.

    Args:
        record: The record about to be written.

    Returns:
        The embedding width.

    Raises:
        VectorStoreError: If the embedding has no components. A zero-width vector cannot be
            compared with anything and would poison the collection's width binding.
    """
    dimension = record.dimension
    if dimension == 0:
        raise VectorStoreError(f"record {record.id!r} has an empty embedding")
    return dimension


def check_dimension(actual: int, expected: int, *, context: str) -> None:
    """Raise unless *actual* equals *expected*, naming both widths.

    Args:
        actual: The width that was supplied.
        expected: The width the collection (or query) already established.
        context: Short phrase identifying what was being compared, used verbatim in the
            message — for example ``"collection 'chunks'"`` or ``"record 'abc'"``.

    Raises:
        VectorStoreError: On any mismatch.
    """
    if actual != expected:
        raise VectorStoreError(
            f"embedding dimension mismatch for {context}: got {actual}, expected {expected}"
        )


# --------------------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------------------


def normalize_filter_value(value: Any) -> Any:
    """Return *value* in the form comparisons are made against.

    :class:`uuid.UUID` becomes its canonical hyphenated string, because ids travel through
    JSON metadata as strings and a caller holding the ``UUID`` object should not have to
    remember to convert. Everything else is returned untouched.

    Args:
        value: A scalar filter or metadata value.

    Returns:
        The comparable form of *value*.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _scalar_matches(candidate: Any, expected: Any) -> bool:
    """Return whether a metadata value equals a scalar filter value."""
    return normalize_filter_value(candidate) == normalize_filter_value(expected)


def matches_filters(metadata: dict[str, Any], filters: Filter) -> bool:
    """Return whether *metadata* satisfies every entry of *filters*.

    This is the reference implementation of the semantics documented at the top of this
    module; the SQL backends compile the same rules into a parameterised ``WHERE`` clause,
    and the pure-Python backends call this directly.

    Args:
        metadata: A record's metadata.
        filters: An already-validated filter (see :func:`validate_filters`).

    Returns:
        ``True`` when every filter entry matches. An empty filter matches everything.
    """
    for key, expected in filters.items():
        present = key in metadata
        candidate = metadata.get(key)

        if expected is None:
            if present and candidate is not None:
                return False
            continue

        if isinstance(expected, _SEQUENCE_FILTER_TYPES):
            if not any(_scalar_matches(candidate, option) for option in expected):
                return False
            continue

        if not present or not _scalar_matches(candidate, expected):
            return False
    return True


# --------------------------------------------------------------------------------------
# Vector math — pure Python, no numpy, per §8.2
# --------------------------------------------------------------------------------------


def normalize(vec: Sequence[float]) -> list[float]:
    """Return *vec* scaled to unit length.

    Pre-normalising a vector turns cosine similarity into a plain dot product, which is
    three passes of arithmetic reduced to one — the difference between a snappy and a
    sluggish in-memory search over a few thousand chunks.

    Args:
        vec: Any sequence of numbers.

    Returns:
        A new list of floats with Euclidean norm ``1.0``. The **zero vector is returned as
        a list of zeros**, not an error: it is what a degenerate embedding of empty text
        looks like, and :func:`dot` against it correctly yields
        :data:`ZERO_VECTOR_SCORE`.
    """
    components = [float(component) for component in vec]
    norm = math.sqrt(math.fsum(component * component for component in components))
    if norm == 0.0:
        return components
    return [component / norm for component in components]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Return the dot product of two equal-width vectors.

    Equals the cosine similarity when both operands are already unit vectors, which is how
    :class:`~app.knowledge.vector.memory_store.InMemoryVectorStore` scores.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        The dot product, clamped into ``[-1.0, 1.0]`` so accumulated floating-point error in
        a pre-normalised comparison can never yield a score outside the documented range.

    Raises:
        VectorStoreError: If the two widths differ, naming both.
    """
    if len(a) != len(b):
        raise VectorStoreError(f"embedding dimension mismatch: got {len(a)}, expected {len(b)}")
    total = 0.0
    for left, right in zip(a, b, strict=True):
        total += left * right
    return _clamp_score(total)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return the cosine similarity of two equal-width vectors.

    Computed in a single pass — dot product and both norms accumulate together — so a brute
    force scan touches each component once.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Similarity in ``[-1.0, 1.0]``, higher meaning more similar. If either vector has
        zero magnitude the result is :data:`ZERO_VECTOR_SCORE`; there is no division by
        zero on any path.

    Raises:
        VectorStoreError: If the two widths differ, naming both.
    """
    if len(a) != len(b):
        raise VectorStoreError(f"embedding dimension mismatch: got {len(a)}, expected {len(b)}")
    product = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b, strict=True):
        product += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a == 0.0 or norm_b == 0.0:
        return ZERO_VECTOR_SCORE
    return _clamp_score(product / math.sqrt(norm_a * norm_b))


def _clamp_score(score: float) -> float:
    """Clamp *score* into ``[SCORE_MIN, SCORE_MAX]``."""
    if score > SCORE_MAX:
        return SCORE_MAX
    if score < SCORE_MIN:
        return SCORE_MIN
    return score


def rank_hits(hits: Iterable[VectorHit], k: int) -> list[VectorHit]:
    """Return the *k* best of *hits*, sorted by descending score.

    Ties break on ascending id. Without that, two records with identical scores would come
    back in whatever order the underlying dict or SQLite scan happened to produce, and the
    same query would rank differently on two backends — which makes a retrieval regression
    impossible to reproduce.

    Args:
        hits: Scored candidates, in any order.
        k: Maximum number to keep.

    Returns:
        At most *k* hits, best first.
    """
    ordered = sorted(hits, key=lambda hit: (-hit.score, hit.id))
    return ordered[:k]


def batch_dimension(records: Sequence[VectorRecord], expected: int | None) -> int:
    """Validate a batch's embeddings and return the width they all share.

    Every backend needs the same three checks before a write — the batch is non-empty, no
    embedding is empty, and every embedding matches both its siblings and whatever width the
    collection already holds — so they live here rather than three times over.

    Args:
        records: The batch about to be written. Must not be empty.
        expected: The collection's established width, or ``None`` when it is new.

    Returns:
        The width shared by every record in the batch.

    Raises:
        VectorStoreError: If *records* is empty, an embedding is empty, or a width disagrees
            with the batch or with the collection.
    """
    if not records:
        raise VectorStoreError("cannot determine dimension of an empty batch")
    dimension = _require_dimension(records[0])
    if expected is not None:
        check_dimension(dimension, expected, context="this collection")
    for record in records[1:]:
        check_dimension(_require_dimension(record), dimension, context=f"record {record.id!r}")
    return dimension
