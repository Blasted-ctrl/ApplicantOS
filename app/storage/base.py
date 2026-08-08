"""Blob storage — the :class:`StorageBackend` protocol and everything backends share.

``docs/CONTRACTS.md`` §0 places three modules here: this one, :mod:`app.storage.local` and
:mod:`app.storage.s3`. Bytes live in a backend; the *record* of them lives in
:class:`~app.models.file.UploadedFile`. That split is what lets the same row describe a
resume whether it sits under ``var/storage`` or in a bucket, and it is why every write here
returns a :class:`StoredObject` whose fields map one-to-one onto that table's columns
(``storage_key``, ``size_bytes``, ``sha256``, ``content_type``, ``backend``).

Depend on the protocol, never on a concrete backend::

    from app.storage import build_key, get_storage

    storage = get_storage()
    key = build_key("users", user_id, "applications", application_id, "resume", ext="pdf")
    stored = await storage.put_file(key, rendered_pdf)
    link = await storage.url(stored.key, filename="resume.pdf")

**Keys are the security boundary.** A key is assembled from user-influenced values — an
uploaded filename, a company name, a provider slug — and is then turned into a filesystem
path by the local backend. :func:`build_key` therefore *rejects* rather than repairs the
four shapes that make traversal possible: a ``..`` component, a leading separator, a
Windows drive letter, and an embedded NUL byte. Everything else is slugified down to
``[a-z0-9._-]``. Lowercasing is deliberate: it makes a case-insensitive NTFS volume and a
case-sensitive S3 bucket agree on what two keys mean, so ``Resume.PDF`` and ``resume.pdf``
cannot become one object on one machine and two on another.

:func:`normalize_key` is the counterpart for a key that already exists — one read back from
``uploaded_files.storage_key``. It re-runs the traversal checks but never slugifies,
because a stored key must resolve to the object it was written to, byte for byte.

**Content types come from a fixed table**, not from :mod:`mimetypes`, whose answers depend
on the host's registry: the same rendered PDF must not be served as ``application/pdf`` on
one machine and ``application/octet-stream`` on another.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "CONTENT_TYPES",
    "COPY_CHUNK_BYTES",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_CONTENT_TYPE",
    "DEFAULT_URL_TTL_SECONDS",
    "KEY_MAX_LENGTH",
    "KEY_SEPARATOR",
    "MAX_SEGMENT_LENGTH",
    "WINDOWS_RESERVED_NAMES",
    "ObjectNotFoundError",
    "StorageBackend",
    "StorageError",
    "StorageKeyError",
    "StoredObject",
    "build_key",
    "guess_content_type",
    "key_segments",
    "normalize_key",
    "resolve_content_type",
    "sha256_bytes",
    "sha256_file",
]

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Key component separator. POSIX-style on every platform, because that is what S3 expects
#: and what ``uploaded_files.storage_key`` stores; only the local backend translates it into
#: real path separators.
KEY_SEPARATOR: Final[str] = "/"

#: Maximum length of a whole key. Mirrors the width of ``uploaded_files.storage_key`` — a
#: key that cannot be catalogued is not a key worth writing.
KEY_MAX_LENGTH: Final[int] = 1024

#: Maximum length of one key component, after slugification. Comfortably inside the 255-byte
#: filename limit of every filesystem this runs on, with room for a temp-file suffix.
MAX_SEGMENT_LENGTH: Final[int] = 120

#: Component that makes traversal possible. Rejected outright, never rewritten.
TRAVERSAL_TOKEN: Final[str] = ".."

#: Byte that truncates a path in the C library underneath the filesystem calls.
NUL_BYTE: Final[str] = "\x00"

#: Read size for streaming reads and for hashing while copying. One mebibyte is a single
#: syscall for a resume and keeps a multi-megabyte screenshot off the heap.
COPY_CHUNK_BYTES: Final[int] = 1 << 20

#: Default chunk size handed to :meth:`StorageBackend.open`.
DEFAULT_CHUNK_SIZE: Final[int] = COPY_CHUNK_BYTES

#: Lifetime of a generated download link, where the backend has to sign one.
DEFAULT_URL_TTL_SECONDS: Final[int] = 3600

#: MIME type for anything the table below does not name.
DEFAULT_CONTENT_TYPE: Final[str] = "application/octet-stream"

#: Extension → MIME type. Fixed on purpose (see the module docstring). Covers what this
#: system actually stores: rendered documents, user uploads, and browser artifacts.
CONTENT_TYPES: Final[dict[str, str]] = {
    # rendered documents and their sources
    "pdf": "application/pdf",
    "tex": "application/x-tex",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "md": "text/markdown",
    "txt": "text/plain",
    "html": "text/html",
    "htm": "text/html",
    "mhtml": "multipart/related",
    # structured payloads
    "json": "application/json",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "xml": "application/xml",
    "csv": "text/csv",
    "har": "application/json",
    "log": "text/plain",
    # proof-of-submission captures and other artifacts
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "webm": "video/webm",
    "mp4": "video/mp4",
    "zip": "application/zip",
}

#: Device names Windows resolves *before* the filesystem, whatever directory they appear in.
#: A key component whose stem is one of these is prefixed rather than rejected: it is a
#: portability hazard, not an attack.
WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)

#: Prefix applied to a component that collides with a reserved device name.
RESERVED_NAME_PREFIX: Final[str] = "_"

#: Everything outside this set is replaced with a hyphen during slugification.
_UNSAFE_CHARS: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9._-]+")

#: Runs of hyphens collapse to one, so a slugified component stays readable.
_DASH_RUN: Final[re.Pattern[str]] = re.compile(r"-{2,}")

#: Runs of dots collapse to one. Belt and braces over the ``..`` rejection above: no
#: sequence of slugification steps can reconstitute a traversal component.
_DOT_RUN: Final[re.Pattern[str]] = re.compile(r"\.{2,}")

#: Either path separator, in any run length. Callers pass ``"a/b"`` and Windows callers pass
#: ``"a\\b"``; both mean the same nesting.
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[/\\]+")

#: A leading ``C:`` — a drive-relative path, which must never reach a key.
_DRIVE_LETTER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")

#: Characters stripped from both ends of a component: a trailing dot is invisible on
#: Windows, and a leading dot hides the file on POSIX.
_TRIM_CHARS: Final[str] = "-."


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class StorageError(RuntimeError):
    """A storage operation failed.

    Backends raise this instead of leaking :class:`OSError` or a vendor SDK's exception, so
    a caller can handle "the bytes did not land" without importing ``botocore``.
    """


class StorageKeyError(StorageError, ValueError):
    """A key is malformed, unsafe, or resolves outside the backend's root.

    Also a :class:`ValueError`, because a rejected key is bad input rather than a failed
    I/O operation — and because FastAPI's validation handlers already understand one.
    """


class ObjectNotFoundError(StorageError, LookupError):
    """No object exists under the requested key.

    Also a :class:`LookupError`, mirroring
    :class:`~app.knowledge.indexer.SourceNotFoundError`, so ``except LookupError`` covers
    the whole "asked for something that is not there" family.
    """


# --------------------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredObject:
    """The result of a successful write — exactly what the catalogue needs.

    Attributes:
        key: Backend-relative location of the bytes, canonical and safe.
        size_bytes: Size of the object as written.
        sha256: Lowercase hexadecimal SHA-256 of the bytes. Always :mod:`hashlib`, never
            :func:`hash`, whose salt changes per process (golden rule #9).
        content_type: MIME type recorded with the object.
        backend: Name of the backend that holds it — ``"local"`` or ``"s3"``, the same
            values ``uploaded_files.backend`` stores.
    """

    key: str
    size_bytes: int
    sha256: str
    content_type: str
    backend: str


# --------------------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """The structural type every blob store implements.

    Seven operations, all keyed by a POSIX-style string: two writes (:meth:`put` for bytes
    already in memory, :meth:`put_file` for something already on disk), two reads
    (:meth:`get` for the whole object, :meth:`open` for a stream), and
    :meth:`delete` / :meth:`exists` / :meth:`url`.

    Implementations never partially write: a reader either sees the previous object or the
    new one. They raise :class:`StorageKeyError` for an unsafe key,
    :class:`ObjectNotFoundError` for a missing one, and :class:`StorageError` for
    everything else.
    """

    @property
    def name(self) -> str:
        """Backend identifier persisted in ``uploaded_files.backend``."""

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        """Write *data* under *key*, replacing any existing object.

        Args:
            key: Destination key.
            data: The bytes to store.
            content_type: MIME type to record. Inferred from the key when omitted.

        Returns:
            The stored object's catalogue metadata.

        Raises:
            StorageKeyError: If *key* is unsafe.
            StorageError: If the write fails.
        """

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        """Upload the file at *source* to *key*, replacing any existing object.

        Args:
            key: Destination key.
            source: An existing local file — typically a temp render.
            content_type: MIME type to record. Inferred from *source*, then *key*.

        Returns:
            The stored object's catalogue metadata.

        Raises:
            StorageKeyError: If *key* is unsafe.
            ObjectNotFoundError: If *source* does not exist.
            StorageError: If the upload fails.
        """

    async def get(self, key: str) -> bytes:
        """Return the whole object stored under *key*.

        Args:
            key: The object's key.

        Returns:
            The stored bytes.

        Raises:
            StorageKeyError: If *key* is unsafe.
            ObjectNotFoundError: If nothing is stored under *key*.
            StorageError: If the read fails.
        """

    def open(self, key: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Stream the object under *key* in chunks.

        Not a coroutine: it returns an async iterator, so a download route can hand it
        straight to a streaming response without buffering the object.

        Args:
            key: The object's key.
            chunk_size: Bytes to yield per iteration.

        Returns:
            An async iterator over the object's bytes.

        Raises:
            StorageKeyError: If *key* is unsafe.
            ObjectNotFoundError: If nothing is stored under *key*.
            StorageError: If the read fails.
        """

    async def delete(self, key: str) -> bool:
        """Delete the object under *key*.

        Args:
            key: The object's key.

        Returns:
            ``True`` if an object was deleted, ``False`` if there was nothing there.
            Deleting an absent object is not an error — cleanup runs more than once.

        Raises:
            StorageKeyError: If *key* is unsafe.
            StorageError: If the delete fails for any other reason.
        """

    async def exists(self, key: str) -> bool:
        """Return whether an object is stored under *key*.

        Args:
            key: The object's key.

        Returns:
            ``True`` when the object is there.

        Raises:
            StorageKeyError: If *key* is unsafe.
            StorageError: If the check fails.
        """

    async def url(
        self,
        key: str,
        *,
        expires_in: int = DEFAULT_URL_TTL_SECONDS,
        filename: str | None = None,
    ) -> str:
        """Return a URL the desktop client can fetch the object from.

        Args:
            key: The object's key.
            expires_in: Lifetime in seconds, where the backend signs the link.
            filename: Name to suggest to the browser, where the backend can.

        Returns:
            A URL. The local backend returns the API's download path; S3 returns a
            presigned URL. Callers treat it as opaque.

        Raises:
            StorageKeyError: If *key* is unsafe.
            StorageError: If a link cannot be produced.
        """


# --------------------------------------------------------------------------------------
# Key construction
# --------------------------------------------------------------------------------------


def _reject_hazards(raw: str) -> None:
    """Raise if *raw* carries one of the four shapes that make traversal possible.

    Args:
        raw: One caller-supplied key component, before any rewriting.

    Raises:
        StorageKeyError: If *raw* contains a NUL byte or a ``..`` component, starts with a
            path separator, or starts with a drive letter.
    """
    if NUL_BYTE in raw:
        raise StorageKeyError("key component contains a NUL byte")
    if TRAVERSAL_TOKEN in raw:
        raise StorageKeyError(f"key component contains {TRAVERSAL_TOKEN!r}: {raw!r}")
    if raw[:1] in ("/", "\\"):
        raise StorageKeyError(f"key component is an absolute path: {raw!r}")
    if _DRIVE_LETTER.match(raw):
        raise StorageKeyError(f"key component names a drive: {raw!r}")


def _slugify(raw: str) -> str:
    """Reduce one component to ``[a-z0-9._-]``.

    Args:
        raw: A single component, already checked by :func:`_reject_hazards` and free of
            path separators.

    Returns:
        The slug, possibly empty when the component held nothing usable. A component whose
        stem is a Windows device name is prefixed with :data:`RESERVED_NAME_PREFIX`.
    """
    # Decompose first so accented Latin survives as its base letter: "Résumé.pdf" becomes
    # "resume.pdf" rather than "r-sum.pdf". Anything with no ASCII form still falls through
    # to the hyphen substitution below.
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE_CHARS.sub("-", folded.strip().lower())
    cleaned = _DASH_RUN.sub("-", cleaned)
    cleaned = _DOT_RUN.sub(".", cleaned)
    cleaned = cleaned.strip(_TRIM_CHARS)[:MAX_SEGMENT_LENGTH].strip(_TRIM_CHARS)
    if not cleaned:
        return ""
    if cleaned.partition(".")[0] in WINDOWS_RESERVED_NAMES:
        cleaned = f"{RESERVED_NAME_PREFIX}{cleaned}"
    return cleaned


def _normalize_extension(ext: str) -> str:
    """Reduce a requested extension to a safe suffix without its dot.

    Args:
        ext: The extension, with or without a leading dot.

    Returns:
        The slugified extension, or ``""`` when nothing usable remains.

    Raises:
        StorageKeyError: If *ext* carries a traversal hazard.
    """
    _reject_hazards(ext)
    return _slugify(ext.lstrip(".").replace(".", "-"))


def build_key(*parts: object, ext: str | None = None) -> str:
    """Assemble a slugified, traversal-safe storage key.

    Every component is stringified, checked, slugified and lowercased; empty components are
    dropped, so an optional prefix can be passed as ``""`` without special-casing at the
    call site. Nesting may be expressed either as separate arguments or with ``/`` inside
    one of them — ``build_key("users", uid, "resume.pdf")`` and
    ``build_key(f"users/{uid}", "resume.pdf")`` agree.

    Args:
        *parts: Key components. Any object; :class:`~uuid.UUID` and :class:`int` are
            stringified. ``None`` is skipped.
        ext: Extension to guarantee on the last component, with or without its dot. A
            component that already ends in it is left alone.

    Returns:
        A POSIX-style key: what both backends and ``uploaded_files.storage_key`` expect.

    Raises:
        StorageKeyError: If any component contains ``..`` or a NUL byte, is an absolute
            path, names a drive, if nothing usable survives slugification, or if the
            finished key exceeds :data:`KEY_MAX_LENGTH`.
    """
    segments: list[str] = []
    for part in parts:
        if part is None:
            continue
        raw = str(part)
        _reject_hazards(raw)
        for piece in _SEPARATORS.split(raw):
            slug = _slugify(piece)
            if slug:
                segments.append(slug)

    if not segments:
        raise StorageKeyError(f"no usable key components in {parts!r}")

    if ext:
        suffix = _normalize_extension(ext)
        if suffix and not segments[-1].endswith(f".{suffix}"):
            segments[-1] = f"{segments[-1]}.{suffix}"

    key = KEY_SEPARATOR.join(segments)
    if len(key) > KEY_MAX_LENGTH:
        raise StorageKeyError(f"key is {len(key)} characters, limit is {KEY_MAX_LENGTH}")
    return key


def key_segments(key: str) -> tuple[str, ...]:
    """Split an existing key into its safe components.

    Unlike :func:`build_key` this does not rewrite anything: a key read back from the
    database must resolve to the object it was written to. It only validates and drops the
    no-op ``.`` component.

    Args:
        key: A storage key, as stored.

    Returns:
        The components, in order.

    Raises:
        StorageKeyError: If *key* is empty, over :data:`KEY_MAX_LENGTH`, contains a NUL
            byte, is absolute, names a drive, or contains a ``..`` component.
    """
    if not key or not key.strip():
        raise StorageKeyError("key is empty")
    if len(key) > KEY_MAX_LENGTH:
        raise StorageKeyError(f"key is {len(key)} characters, limit is {KEY_MAX_LENGTH}")
    _reject_hazards(key)

    segments = tuple(piece for piece in _SEPARATORS.split(key.strip()) if piece not in ("", "."))
    if not segments:
        raise StorageKeyError(f"key has no components: {key!r}")
    return segments


def normalize_key(key: str) -> str:
    """Return the canonical form of an existing key.

    Args:
        key: A storage key, as supplied or as stored.

    Returns:
        The key with redundant separators and ``.`` components removed, joined with
        :data:`KEY_SEPARATOR`.

    Raises:
        StorageKeyError: For any of the conditions :func:`key_segments` rejects.
    """
    return KEY_SEPARATOR.join(key_segments(key))


# --------------------------------------------------------------------------------------
# Content types and digests
# --------------------------------------------------------------------------------------


def guess_content_type(path: str | Path) -> str:
    """Return the MIME type for a filename, key, or path.

    Resolved from :data:`CONTENT_TYPES` rather than :mod:`mimetypes`, whose answers depend
    on the host's registry — the same PDF must be served identically on every install.

    Args:
        path: A filename, a storage key, or a local path. Either separator is understood.

    Returns:
        The MIME type, or :data:`DEFAULT_CONTENT_TYPE` when the extension is unknown or
        absent.
    """
    text = str(path).replace("\\", KEY_SEPARATOR).rstrip(KEY_SEPARATOR)
    name = text.rpartition(KEY_SEPARATOR)[2]
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        return DEFAULT_CONTENT_TYPE
    return CONTENT_TYPES.get(extension.lower(), DEFAULT_CONTENT_TYPE)


def resolve_content_type(explicit: str | None, *candidates: str | Path) -> str:
    """Pick the MIME type to record for a write.

    Args:
        explicit: What the caller asked for. Always wins when set.
        *candidates: Names to infer from, in preference order — typically the source
            filename first and the destination key second, because a key may have been
            slugified past its extension.

    Returns:
        The first confident answer, or :data:`DEFAULT_CONTENT_TYPE`.
    """
    if explicit:
        return explicit
    for candidate in candidates:
        guessed = guess_content_type(candidate)
        if guessed != DEFAULT_CONTENT_TYPE:
            return guessed
    return DEFAULT_CONTENT_TYPE


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 of *data*.

    Args:
        data: The bytes to hash.

    Returns:
        The lowercase hexadecimal digest.
    """
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = COPY_CHUNK_BYTES) -> str:
    """Return the SHA-256 of a file, read in chunks.

    Synchronous by design: backends call it inside a worker thread, alongside the read or
    copy it accompanies.

    Args:
        path: The file to hash.
        chunk_size: Read size.

    Returns:
        The lowercase hexadecimal digest.

    Raises:
        OSError: If the file cannot be read. Backends translate this.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
