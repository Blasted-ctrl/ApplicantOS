"""Cache key derivation — stable, collision-resistant identifiers for cached work.

ApplicantOS caches aggressively (golden rule #9: *cache aggressively, invalidate
precisely*), which only works if two callers that ask for the same thing compute the same
key — in a different process, on a different day, after a restart. This module is the one
place where that canonicalisation happens.

The pipeline is always the same:

1. :func:`to_jsonable` recursively converts an arbitrary Python object into JSON-native
   data. Pydantic models go through ``model_dump(mode="json")``, dataclasses through
   ``dataclasses.asdict``, enums become their value, ``Path``/``UUID``/``datetime``/
   ``Decimal`` become strings, and byte strings become a digest. The function is *total*:
   anything it does not understand degrades to a repr with memory addresses stripped, so a
   key never silently depends on where an object happened to land in the heap.
2. :func:`canonical_json` serialises that structure with sorted keys and no whitespace.
3. :func:`hash_payload` takes the SHA-256 of the canonical form.
4. :func:`make_key` prefixes the digest with a normalised namespace, producing
   ``"<namespace>:<64 hex chars>"``.

The namespace prefix is load-bearing: it is what lets
:meth:`~app.cache.base.Cache.clear` drop one family of cached work without touching the
rest, what names the ``applicantos_cache_events_total{namespace,event}`` metric label, and
what becomes a directory name in :class:`~app.cache.disk.DiskCache`.

**Serialisation here is deliberately stdlib-only.** ``orjson`` is used for cache *values*
(see :mod:`app.cache.base`) but never for keys: a process with ``orjson`` installed and one
without must derive byte-identical keys, or a shared Redis instance would fragment into two
disjoint caches.

This module imports nothing from ``app`` and must stay that way — it sits below the cache
backends, the decorators, and every caller of them.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import decimal
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from enum import Enum
from pathlib import PurePath
from typing import Any, Final

__all__ = [
    "DEFAULT_NAMESPACE",
    "KEY_SEPARATOR",
    "MAX_NAMESPACE_LENGTH",
    "MAX_NORMALIZATION_DEPTH",
    "NAMESPACES",
    "canonical_json",
    "hash_payload",
    "make_key",
    "namespace_of",
    "normalize_namespace",
    "to_jsonable",
]

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Character separating the namespace prefix from the digest inside a cache key. Chosen to
#: match the Redis convention; it never appears in a normalised namespace or in a hex
#: digest, so ``key.partition(KEY_SEPARATOR)`` is an unambiguous split.
KEY_SEPARATOR: Final[str] = ":"

#: Namespace attributed to keys that were not produced by :func:`make_key`.
DEFAULT_NAMESPACE: Final[str] = "misc"

#: Upper bound on a namespace, so it is always a legal path segment on every filesystem.
MAX_NAMESPACE_LENGTH: Final[int] = 64

#: Recursion ceiling for :func:`to_jsonable`. Cache arguments are never legitimately this
#: deep; the limit bounds the cost of a pathological structure.
MAX_NORMALIZATION_DEPTH: Final[int] = 24

#: Marker substituted for a container that references itself, so normalisation terminates.
CYCLE_MARKER: Final[str] = "<cycle>"

#: Prefix identifying a byte string that was replaced by its digest during key derivation.
BYTES_DIGEST_PREFIX: Final[str] = "sha256:"

#: JSON object key used to tag a set during key derivation, so ``{1, 2}`` and ``[1, 2]``
#: cannot collide on the same digest.
SET_TAG: Final[str] = "__set__"

#: Characters that may appear in a normalised namespace. Everything else collapses to "_".
_NAMESPACE_UNSAFE_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_.-]+")

#: Matches the ``at 0x7f…`` fragment of a default ``repr``. Stripping it keeps keys stable
#: across runs when an object without a meaningful representation reaches normalisation.
_MEMORY_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(r"\s+at\s+0x[0-9a-fA-F]+")

#: ``json.dumps`` arguments that make output canonical: deterministic ordering, no padding.
_CANONICAL_JSON_KWARGS: Final[dict[str, Any]] = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": False,
}


class NAMESPACES:
    """The cache namespaces ApplicantOS uses, per ``docs/CONTRACTS.md`` §7.

    Every namespace corresponds to one family of expensive work that is worth reusing:

    ``LLM``
        Model completions, cached when ``settings.cache_llm_responses`` is on and the call
        is deterministic (``temperature == 0``).
    ``EMBEDDING``
        Embedding vectors, keyed by model plus text.
    ``RESUME_PARSE``
        Structured output of parsing an uploaded resume.
    ``PROJECT_SUMMARY``
        Summaries produced while analysing a local project folder.
    ``GITHUB``
        GitHub REST responses and per-repository analysis.
    ``COMPANY``
        Enriched company metadata.
    ``POSTING``
        Fetched job descriptions and provider payloads.
    ``RENDER``
        Rendered resumes and cover letters, keyed by document content hash.
    ``SCORE``
        Scoring results for a (posting, user) pair.
    ``HTTP``
        Generic outbound HTTP responses (crawlers, feeds).

    Use these constants rather than string literals so a rename is a single edit and
    :meth:`all` stays an accurate inventory for maintenance tasks.
    """

    __slots__ = ()

    LLM: Final[str] = "llm"
    EMBEDDING: Final[str] = "embedding"
    RESUME_PARSE: Final[str] = "resume_parse"
    PROJECT_SUMMARY: Final[str] = "project_summary"
    GITHUB: Final[str] = "github"
    COMPANY: Final[str] = "company"
    POSTING: Final[str] = "posting"
    RENDER: Final[str] = "render"
    SCORE: Final[str] = "score"
    HTTP: Final[str] = "http"

    @classmethod
    def all(cls) -> tuple[str, ...]:
        """Return every declared namespace value, in declaration order.

        Returns:
            The namespace strings, suitable for iterating in a maintenance task that
            clears or reports on each family of cached work.
        """
        return tuple(
            value for name, value in vars(cls).items() if name.isupper() and isinstance(value, str)
        )

    @classmethod
    def contains(cls, namespace: str) -> bool:
        """Return whether *namespace* is one of the contract-defined namespaces.

        Args:
            namespace: A namespace string, normalised or not.

        Returns:
            ``True`` when the normalised form of *namespace* is declared here. Unknown
            namespaces are still usable — this is an inventory check, not a gate.
        """
        try:
            return normalize_namespace(namespace) in cls.all()
        except (TypeError, ValueError):
            return False


# --------------------------------------------------------------------------------------
# Namespace handling
# --------------------------------------------------------------------------------------


def normalize_namespace(namespace: str) -> str:
    """Reduce *namespace* to a lowercase, filesystem- and Redis-safe token.

    Whitespace is trimmed, the token is lowercased, any character outside
    ``[a-z0-9_.-]`` collapses to ``_``, and leading/trailing separators are removed — the
    last step is what makes ``"."`` and ``".."`` impossible, so a namespace can never be
    used to escape the disk cache root.

    Args:
        namespace: The caller-supplied namespace, typically a :class:`NAMESPACES` constant.

    Returns:
        The normalised namespace, truncated to :data:`MAX_NAMESPACE_LENGTH` characters.

    Raises:
        TypeError: If *namespace* is not a string.
        ValueError: If nothing usable survives normalisation (empty, whitespace, ``"."``).
    """
    if not isinstance(namespace, str):
        raise TypeError(f"cache namespace must be a string, got {type(namespace).__name__}")
    candidate = _NAMESPACE_UNSAFE_RE.sub("_", namespace.strip().lower()).strip("._-")
    if not candidate:
        raise ValueError(f"cache namespace normalises to nothing: {namespace!r}")
    return candidate[:MAX_NAMESPACE_LENGTH]


def namespace_of(key: str) -> str:
    """Return the namespace a cache key belongs to.

    Args:
        key: A cache key, ideally one produced by :func:`make_key`.

    Returns:
        The normalised namespace prefix, or :data:`DEFAULT_NAMESPACE` when *key* carries no
        recognisable prefix. Never raises — callers use this to label metrics and choose a
        storage shard, and neither is worth failing a cache operation over.
    """
    prefix, separator, _ = key.partition(KEY_SEPARATOR)
    if not separator or not prefix:
        return DEFAULT_NAMESPACE
    try:
        return normalize_namespace(prefix)
    except (TypeError, ValueError):
        return DEFAULT_NAMESPACE


# --------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------


def _digest_bytes(raw: bytes) -> str:
    """Return the key-safe representation of a byte string.

    Args:
        raw: Arbitrary binary data.

    Returns:
        ``"sha256:<hex>"`` — bounded in size regardless of the input, which keeps a cache
        key derived from a 40 MB PDF exactly as small as one derived from a short string.
    """
    return f"{BYTES_DIGEST_PREFIX}{hashlib.sha256(raw).hexdigest()}"


def _encode_bytes(raw: bytes) -> str:
    """Return the value-safe representation of a byte string.

    Args:
        raw: Arbitrary binary data.

    Returns:
        The base64 ASCII encoding, which round-trips through JSON without loss.
    """
    return base64.b64encode(raw).decode("ascii")


def _fallback_repr(value: Any) -> str:
    """Represent an object that has no JSON-native form, stably across processes.

    An object relying on the default ``object.__repr__`` renders its memory address, which
    would make every cache key unique per run. Such objects collapse to their fully
    qualified type name instead; objects with a real ``__repr__`` keep it, with any
    embedded address stripped.

    Args:
        value: The unconvertible object.

    Returns:
        A deterministic string describing *value*.
    """
    cls = type(value)
    if cls.__repr__ is object.__repr__:
        return f"{cls.__module__}.{cls.__qualname__}"
    try:
        rendered = repr(value)
    except Exception:
        return f"{cls.__module__}.{cls.__qualname__}"
    return _MEMORY_ADDRESS_RE.sub("", rendered)


def _stringify_key(key: Any) -> str:
    """Convert a mapping key into the string JSON requires.

    Args:
        key: A dictionary key of any hashable type.

    Returns:
        A deterministic string form of *key*.
    """
    if isinstance(key, str):
        return key
    if isinstance(key, Enum):
        return _stringify_key(key.value)
    if key is None:
        return "null"
    if isinstance(key, (bool, int, float)):
        return json.dumps(key)
    if isinstance(key, (bytes, bytearray, memoryview)):
        return _digest_bytes(bytes(key))
    if isinstance(key, (PurePath, uuid.UUID, decimal.Decimal)):
        return str(key)
    return _fallback_repr(key)


def _model_dump(value: Any) -> Any | None:
    """Dump a pydantic model (v2 or v1) to plain data, or return ``None``.

    Duck typing rather than an import keeps this module free of third-party dependencies
    and simultaneously supports any object that offers the same interface.

    Args:
        value: A candidate pydantic model.

    Returns:
        The dumped mapping, or ``None`` when *value* is not model-like or refuses to dump.
    """
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            return dumper(mode="json")
        except TypeError:
            try:
                return dumper()
            except Exception:
                return None
        except Exception:
            return None
    legacy = getattr(value, "dict", None)
    if callable(legacy) and hasattr(value, "__fields__"):
        try:
            return legacy()
        except Exception:
            return None
    return None


def _dataclass_dump(value: Any) -> Any:
    """Dump a dataclass instance to plain data.

    Args:
        value: A dataclass *instance* (not the class itself).

    Returns:
        A dictionary of field name to value. :func:`dataclasses.asdict` is tried first
        because it recurses through nested dataclasses; if it fails — it deep-copies, which
        some field types refuse — the fields are read directly instead.
    """
    try:
        return dataclasses.asdict(value)
    except Exception:
        return {field.name: getattr(value, field.name, None) for field in dataclasses.fields(value)}


def _convert(value: Any, *, for_key: bool, depth: int, seen: set[int]) -> Any:
    """Recursively convert *value* into JSON-native data.

    Args:
        value: The object to convert.
        for_key: ``True`` when deriving a cache key — byte strings collapse to a digest and
            sets are tagged so they cannot collide with lists. ``False`` when serialising a
            cache value — byte strings are base64 encoded so they survive a round trip.
        depth: Current recursion depth, bounded by :data:`MAX_NORMALIZATION_DEPTH`.
        seen: Identity set of containers on the current path, which terminates cycles.

    Returns:
        JSON-native data: ``None``, ``bool``, ``int``, ``float``, ``str``, ``list``, or
        ``dict``.
    """
    if depth > MAX_NORMALIZATION_DEPTH:
        return _fallback_repr(value)

    # Scalars. Enum is checked first because StrEnum/IntEnum are str/int subclasses.
    if value is None:
        return None
    if isinstance(value, Enum):
        return _convert(value.value, for_key=for_key, depth=depth + 1, seen=seen)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value

    # Scalar-like objects with a canonical textual form.
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return _digest_bytes(raw) if for_key else _encode_bytes(raw)
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return str(value)

    identity = id(value)
    if identity in seen:
        return CYCLE_MARKER

    # Structured objects. Models and dataclasses are reduced to mappings, then recursed.
    dumped = _model_dump(value)
    if dumped is not None:
        seen.add(identity)
        try:
            return _convert(dumped, for_key=for_key, depth=depth + 1, seen=seen)
        finally:
            seen.discard(identity)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        seen.add(identity)
        try:
            return _convert(_dataclass_dump(value), for_key=for_key, depth=depth + 1, seen=seen)
        finally:
            seen.discard(identity)

    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            return {
                _stringify_key(item_key): _convert(
                    item_value, for_key=for_key, depth=depth + 1, seen=seen
                )
                for item_key, item_value in value.items()
            }
        finally:
            seen.discard(identity)

    if isinstance(value, (AbstractSet, frozenset, set)):
        seen.add(identity)
        try:
            members = [
                _convert(item, for_key=for_key, depth=depth + 1, seen=seen) for item in value
            ]
        finally:
            seen.discard(identity)
        # Sets have no intrinsic order; sorting their canonical form restores determinism
        # without requiring the members themselves to be mutually comparable.
        members.sort(key=lambda item: json.dumps(item, default=str, **_CANONICAL_JSON_KWARGS))
        return {SET_TAG: members} if for_key else members

    if isinstance(value, (list, tuple, range)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        seen.add(identity)
        try:
            return [_convert(item, for_key=for_key, depth=depth + 1, seen=seen) for item in value]
        finally:
            seen.discard(identity)

    return _fallback_repr(value)


def to_jsonable(value: Any, *, for_key: bool = False) -> Any:
    """Convert an arbitrary object into JSON-native data, without ever raising.

    Handles — recursively — pydantic models, dataclasses, enums, mappings, sequences, sets,
    ``Path``, ``UUID``, ``datetime``/``date``/``time``/``timedelta``, ``Decimal`` and byte
    strings. Cycles terminate at :data:`CYCLE_MARKER`, depth is capped at
    :data:`MAX_NORMALIZATION_DEPTH`, and anything unrecognised degrades to a stable repr.

    Args:
        value: The object to convert.
        for_key: ``True`` when the result feeds a cache key (byte strings become a digest;
            sets are tagged so they cannot collide with an equivalent list). ``False`` when
            the result is a cache value to be stored and read back — byte strings are
            base64 encoded and sets become plain lists.

    Returns:
        A structure containing only JSON-native types.
    """
    return _convert(value, for_key=for_key, depth=0, seen=set())


# --------------------------------------------------------------------------------------
# Key derivation
# --------------------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Serialise *value* to its canonical JSON form.

    Canonical means: normalised through :func:`to_jsonable`, object keys sorted, and no
    insignificant whitespace. Two structurally equal inputs always produce byte-identical
    output, in any process, on any platform.

    Args:
        value: The object to serialise.

    Returns:
        A single-line JSON document.
    """
    return json.dumps(to_jsonable(value, for_key=True), default=str, **_CANONICAL_JSON_KWARGS)


def hash_payload(obj: Any) -> str:
    """Return the SHA-256 digest of *obj* in canonical form.

    This is the content-addressing primitive used across ApplicantOS: a rendered resume is
    keyed by the hash of its document, a scoring result by the hash of the posting plus the
    rule pack, an LLM response by the hash of the prompt bundle.

    Args:
        obj: Any object; see :func:`to_jsonable` for how exotic types are handled.

    Returns:
        A 64-character lowercase hexadecimal digest.
    """
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def make_key(namespace: str, *parts: Any) -> str:
    """Build a stable, collision-resistant cache key.

    The key is ``"<namespace>:<sha256 of the parts>"``. Arity matters — ``make_key("llm",
    "a")`` and ``make_key("llm", "a", "b")`` differ — and so does order, because the parts
    are hashed as a sequence.

    Args:
        namespace: The family of cached work; prefer a :class:`NAMESPACES` constant.
            Normalised via :func:`normalize_namespace`.
        *parts: Everything that identifies this unit of work. Dictionaries are key-sorted,
            pydantic models and dataclasses are dumped, and unhashable-but-structured
            arguments are all supported — see :func:`to_jsonable`.

    Returns:
        The cache key.

    Raises:
        TypeError: If *namespace* is not a string.
        ValueError: If *namespace* normalises to nothing.
    """
    return f"{normalize_namespace(namespace)}{KEY_SEPARATOR}{hash_payload(list(parts))}"
