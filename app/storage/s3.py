"""Blob storage in an S3-compatible bucket — the shared-install backend.

Selected by ``settings.storage_backend="s3"``. Everything else about the system is
unchanged: keys are the same POSIX strings the local backend uses, so an install can be
migrated by copying a tree into a bucket and flipping one setting, and
``uploaded_files.storage_key`` keeps resolving.

**Two SDKs, one code path.** ``aioboto3`` is preferred because its calls are natively
awaitable; when it is absent, ``boto3`` is driven from :func:`asyncio.to_thread` so the
event loop still never blocks. Both are imported lazily inside the call that needs them —
a local install must never be asked to install an AWS SDK it will never use, and importing
:mod:`app.storage` must stay free. When neither is installed the error names the fix.

**MinIO and friends** are supported through ``settings.s3_endpoint_url``: point it at the
gateway and the same code runs unchanged. Credentials come from ``settings`` when set and
otherwise from the SDK's own chain (environment, profile, instance role), so a deployment
that already has a role attached does not have to copy keys into ``.env``.

:meth:`S3Storage.url` returns a **presigned** GET URL rather than a public one. Bytes here
are the user's resume and their proof of submission; nothing in this bucket should be
readable without a signature that expires.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Final

import structlog

from app.config.settings import get_settings
from app.storage.base import (
    COPY_CHUNK_BYTES,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_URL_TTL_SECONDS,
    ObjectNotFoundError,
    StorageError,
    StoredObject,
    normalize_key,
    resolve_content_type,
    sha256_bytes,
    sha256_file,
)

__all__ = ["BACKEND_NAME", "MISSING_SDK_DETAIL", "S3Storage"]

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Value recorded in ``uploaded_files.backend`` for objects held here.
BACKEND_NAME: Final[str] = "s3"

#: SDK module names, in preference order. ``aioboto3`` first: its client is natively
#: awaitable, so no thread is spent per call.
ASYNC_SDK: Final[str] = "aioboto3"
SYNC_SDK: Final[str] = "boto3"

#: Raised verbatim when neither SDK is importable. Names the package *and* the command,
#: because "no module named aioboto3" three frames deep is not an actionable error.
MISSING_SDK_DETAIL: Final[str] = (
    "STORAGE_BACKEND=s3 requires an AWS SDK, and neither 'aioboto3' nor 'boto3' is "
    "installed; run `pip install aioboto3` (preferred, natively async) or "
    "`pip install boto3`, or set STORAGE_BACKEND=local to keep files on this machine"
)

#: The S3 service name, spelled once.
SERVICE_NAME: Final[str] = "s3"

#: Error codes an S3-compatible service returns for "that object is not here". MinIO and
#: AWS disagree on which, so all four are treated the same.
NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"404", "NoSuchKey", "NotFound", "NoSuchBucket"})

#: Response keys carrying the object's recorded MIME type and size.
CONTENT_TYPE_FIELD: Final[str] = "ContentType"
CONTENT_LENGTH_FIELD: Final[str] = "ContentLength"

#: Characters allowed in the filename of a ``Content-Disposition`` header. Anything else is
#: dropped: the value is user-influenced and ends up inside a quoted header.
_DISPOSITION_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._ -]+")

#: Longest filename echoed back in a presigned link's ``Content-Disposition``.
_DISPOSITION_MAX_LENGTH: Final[int] = 120


class S3Storage:
    """A :class:`~app.storage.base.StorageBackend` over an S3-compatible bucket.

    Args:
        bucket: Bucket name. Defaults to ``settings.s3_bucket``.
        region: AWS region. Defaults to ``settings.s3_region``.
        endpoint_url: Override for MinIO or another gateway. Defaults to
            ``settings.s3_endpoint_url``.
        access_key_id: Access key. Defaults to ``settings.aws_access_key_id``; when neither
            is set the SDK's own credential chain is used.
        secret_access_key: Secret key, resolved the same way.
        url_expires_in: Default lifetime of a presigned link, in seconds.

    Raises:
        StorageError: If no bucket is configured. Failing here rather than on first write
            means a misconfigured install is caught at startup.
    """

    name: ClassVar[str] = BACKEND_NAME

    def __init__(
        self,
        bucket: str | None = None,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        url_expires_in: int = DEFAULT_URL_TTL_SECONDS,
    ) -> None:
        settings = get_settings()
        resolved_bucket = bucket if bucket is not None else settings.s3_bucket
        if not resolved_bucket:
            raise StorageError(
                "STORAGE_BACKEND=s3 requires S3_BUCKET to be set; "
                "set it in .env or use STORAGE_BACKEND=local"
            )
        self._bucket: str = resolved_bucket
        self._region: str = region if region is not None else settings.s3_region
        self._endpoint_url: str | None = (
            endpoint_url if endpoint_url is not None else settings.s3_endpoint_url
        )
        self._access_key_id: str | None = (
            access_key_id if access_key_id is not None else settings.aws_access_key_id
        )
        self._secret_access_key: str | None = (
            secret_access_key if secret_access_key is not None else settings.aws_secret_access_key
        )
        self._url_expires_in: int = url_expires_in
        self._sdk_name: str | None = None
        self._sync_client_instance: Any | None = None

    def __repr__(self) -> str:
        """Return a debug representation naming the bucket and endpoint, never the keys."""
        return f"{type(self).__name__}(bucket={self._bucket!r}, endpoint={self._endpoint_url!r})"

    @property
    def bucket(self) -> str:
        """The bucket every object lives in."""
        return self._bucket

    # -- SDK plumbing ---------------------------------------------------------------------

    def _client_kwargs(self) -> dict[str, Any]:
        """Return the keyword arguments both SDKs take when building a client.

        Returns:
            Region, endpoint and credentials. Unset credentials are omitted rather than
            passed as ``None``, so the SDK falls back to its own chain.
        """
        kwargs: dict[str, Any] = {"region_name": self._region}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._access_key_id and self._secret_access_key:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        return kwargs

    def _sdk(self) -> str:
        """Return the name of the SDK this instance will drive, importing lazily.

        Returns:
            :data:`ASYNC_SDK` or :data:`SYNC_SDK`. Memoised: the decision is made once per
            instance, and the import cost is paid on the first call rather than at import.

        Raises:
            StorageError: If neither SDK is installed.
        """
        if self._sdk_name is not None:
            return self._sdk_name
        try:
            import aioboto3  # noqa: F401 - probe only; the call sites re-import
        except ImportError:
            try:
                import boto3  # noqa: F401 - probe only; the call sites re-import
            except ImportError as exc:
                raise StorageError(MISSING_SDK_DETAIL) from exc
            self._sdk_name = SYNC_SDK
        else:
            self._sdk_name = ASYNC_SDK
        logger.debug("storage.s3_sdk_selected", sdk=self._sdk_name, bucket=self._bucket)
        return self._sdk_name

    @property
    def _is_async_sdk(self) -> bool:
        """Whether ``aioboto3`` is the SDK in use."""
        return self._sdk() == ASYNC_SDK

    @asynccontextmanager
    async def _async_client(self) -> AsyncIterator[Any]:
        """Yield an ``aioboto3`` S3 client for the duration of one operation.

        Yields:
            The client. Its connection pool is closed on exit, which is why streaming reads
            must consume the body *inside* the block.

        Vendor exceptions are deliberately not translated here: every call site already
        wraps its own block, and catching around the ``yield`` would also swallow errors
        raised by the caller's body.
        """
        import aioboto3

        session = aioboto3.Session()
        async with session.client(SERVICE_NAME, **self._client_kwargs()) as client:
            yield client

    def _sync_client(self) -> Any:
        """Return the memoised ``boto3`` client, building it on first use.

        Returns:
            A thread-safe ``boto3`` S3 client — the SDK documents clients as safe to share
            between threads, which is what makes memoising it correct here.

        Raises:
            StorageError: If the client cannot be built.
        """
        if self._sync_client_instance is not None:
            return self._sync_client_instance
        import boto3

        try:
            self._sync_client_instance = boto3.client(SERVICE_NAME, **self._client_kwargs())
        except Exception as exc:  # vendor errors must not escape this package
            raise _wrap("client", None, exc) from exc
        return self._sync_client_instance

    async def _call(self, operation: str, key: str | None, /, **kwargs: Any) -> dict[str, Any]:
        """Run one non-streaming S3 operation on whichever SDK is available.

        Args:
            operation: Client method name, e.g. ``"put_object"``.
            key: The key involved, for the error message.
            **kwargs: Arguments for the operation.

        Returns:
            The service response.

        Raises:
            ObjectNotFoundError: If the service reported a missing object.
            StorageError: For any other failure.
        """
        try:
            if self._is_async_sdk:
                async with self._async_client() as client:
                    response = await getattr(client, operation)(**kwargs)
                    return dict(response)
            client = await asyncio.to_thread(self._sync_client)
            response = await asyncio.to_thread(partial(getattr(client, operation), **kwargs))
            return dict(response)
        except StorageError:
            raise
        except Exception as exc:  # vendor errors must not escape this package
            raise _wrap(operation, key, exc) from exc

    # -- the protocol ----------------------------------------------------------------------

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
            content_type: MIME type to record. Inferred from *key* when omitted.

        Returns:
            The stored object's catalogue metadata.

        Raises:
            StorageKeyError: If *key* is malformed.
            StorageError: If the upload fails.
        """
        safe_key = normalize_key(key)
        resolved_type = resolve_content_type(content_type, safe_key)
        await self._call(
            "put_object",
            safe_key,
            Bucket=self._bucket,
            Key=safe_key,
            Body=data,
            ContentType=resolved_type,
        )
        logger.debug("storage.s3_put", key=safe_key, size_bytes=len(data), bucket=self._bucket)
        return StoredObject(
            key=safe_key,
            size_bytes=len(data),
            sha256=sha256_bytes(data),
            content_type=resolved_type,
            backend=self.name,
        )

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        """Upload the file at *source* to *key*, replacing any existing object.

        The digest and size are taken from the local file in a worker thread before the
        upload, so a large artifact is never held in memory.

        Args:
            key: Destination key.
            source: An existing local file.
            content_type: MIME type to record. Inferred from *source* then *key*.

        Returns:
            The stored object's catalogue metadata.

        Raises:
            StorageKeyError: If *key* is malformed.
            ObjectNotFoundError: If *source* does not exist.
            StorageError: If the upload fails.
        """
        path = Path(source)
        safe_key = normalize_key(key)
        resolved_type = resolve_content_type(content_type, path.name, safe_key)
        size, digest = await asyncio.to_thread(_measure_file, path)
        extra = {CONTENT_TYPE_FIELD: resolved_type}

        try:
            if self._is_async_sdk:
                async with self._async_client() as client:
                    await client.upload_file(str(path), self._bucket, safe_key, ExtraArgs=extra)
            else:
                client = await asyncio.to_thread(self._sync_client)
                await asyncio.to_thread(
                    partial(
                        client.upload_file,
                        str(path),
                        self._bucket,
                        safe_key,
                        ExtraArgs=extra,
                    )
                )
        except StorageError:
            raise
        except Exception as exc:  # vendor errors must not escape this package
            raise _wrap("upload_file", safe_key, exc) from exc

        logger.debug("storage.s3_put_file", key=safe_key, size_bytes=size, bucket=self._bucket)
        return StoredObject(
            key=safe_key,
            size_bytes=size,
            sha256=digest,
            content_type=resolved_type,
            backend=self.name,
        )

    async def get(self, key: str) -> bytes:
        """Return the whole object stored under *key*.

        Args:
            key: The object's key.

        Returns:
            The stored bytes.

        Raises:
            StorageKeyError: If *key* is malformed.
            ObjectNotFoundError: If nothing is stored under *key*.
            StorageError: If the read fails.
        """
        safe_key = normalize_key(key)
        try:
            if self._is_async_sdk:
                async with self._async_client() as client:
                    response = await client.get_object(Bucket=self._bucket, Key=safe_key)
                    body = response["Body"]
                    try:
                        return bytes(await body.read())
                    finally:
                        await _close_async_body(body)
            client = await asyncio.to_thread(self._sync_client)
            response = await asyncio.to_thread(
                partial(client.get_object, Bucket=self._bucket, Key=safe_key)
            )
            body = response["Body"]
            try:
                return bytes(await asyncio.to_thread(body.read))
            finally:
                await asyncio.to_thread(_close_sync_body, body)
        except StorageError:
            raise
        except Exception as exc:  # vendor errors must not escape this package
            raise _wrap("get_object", safe_key, exc) from exc

    def open(self, key: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Stream the object under *key* in chunks.

        The key is validated eagerly; the request is issued on first iteration.

        Args:
            key: The object's key.
            chunk_size: Bytes to request per iteration.

        Returns:
            An async iterator over the object's bytes.

        Raises:
            StorageKeyError: If *key* is malformed.
        """
        return self._stream(normalize_key(key), max(1, chunk_size))

    async def _stream(self, key: str, chunk_size: int) -> AsyncIterator[bytes]:
        """Yield one object's bytes, holding the response body open while it is read.

        Args:
            key: The already-validated key.
            chunk_size: Bytes per chunk.

        Yields:
            Successive chunks, the last one possibly short.

        Raises:
            ObjectNotFoundError: If nothing is stored under *key*.
            StorageError: If the read fails.
        """
        if self._is_async_sdk:
            try:
                async with self._async_client() as client:
                    response = await client.get_object(Bucket=self._bucket, Key=key)
                    body = response["Body"]
                    try:
                        while chunk := await body.read(chunk_size):
                            yield bytes(chunk)
                    finally:
                        await _close_async_body(body)
            except StorageError:
                raise
            except Exception as exc:  # vendor errors stay in this package
                raise _wrap("get_object", key, exc) from exc
            return

        try:
            client = await asyncio.to_thread(self._sync_client)
            response = await asyncio.to_thread(
                partial(client.get_object, Bucket=self._bucket, Key=key)
            )
        except StorageError:
            raise
        except Exception as exc:  # vendor errors stay in this package
            raise _wrap("get_object", key, exc) from exc

        body = response["Body"]
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(body.read, chunk_size)
                except Exception as exc:  # vendor errors stay in this package
                    raise _wrap("get_object", key, exc) from exc
                if not chunk:
                    return
                yield bytes(chunk)
        finally:
            await asyncio.to_thread(_close_sync_body, body)

    async def delete(self, key: str) -> bool:
        """Delete the object under *key*.

        S3 reports success whether or not the object was there, so existence is checked
        first: cleanup accounting wants to know how much it actually reclaimed.

        Args:
            key: The object's key.

        Returns:
            ``True`` if an object was deleted, ``False`` if there was nothing there.

        Raises:
            StorageKeyError: If *key* is malformed.
            StorageError: If the delete fails.
        """
        safe_key = normalize_key(key)
        if not await self.exists(safe_key):
            return False
        await self._call("delete_object", safe_key, Bucket=self._bucket, Key=safe_key)
        logger.debug("storage.s3_delete", key=safe_key, bucket=self._bucket)
        return True

    async def exists(self, key: str) -> bool:
        """Return whether an object is stored under *key*.

        Args:
            key: The object's key.

        Returns:
            ``True`` when the object is there.

        Raises:
            StorageKeyError: If *key* is malformed.
            StorageError: If the check fails for a reason other than absence.
        """
        safe_key = normalize_key(key)
        try:
            await self._call("head_object", safe_key, Bucket=self._bucket, Key=safe_key)
        except ObjectNotFoundError:
            return False
        return True

    async def url(
        self,
        key: str,
        *,
        expires_in: int = DEFAULT_URL_TTL_SECONDS,
        filename: str | None = None,
    ) -> str:
        """Return a presigned GET URL for *key*.

        Args:
            key: The object's key.
            expires_in: Link lifetime in seconds.
            filename: Name to suggest to the browser, sanitised into a
                ``Content-Disposition`` override.

        Returns:
            A signed URL that stops working when it expires.

        Raises:
            StorageKeyError: If *key* is malformed.
            StorageError: If the link cannot be signed.
        """
        safe_key = normalize_key(key)
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": safe_key}
        disposition = _content_disposition(filename)
        if disposition:
            params["ResponseContentDisposition"] = disposition
        lifetime = expires_in if expires_in > 0 else self._url_expires_in

        try:
            if self._is_async_sdk:
                async with self._async_client() as client:
                    signed = await client.generate_presigned_url(
                        "get_object", Params=params, ExpiresIn=lifetime
                    )
                    return str(signed)
            client = await asyncio.to_thread(self._sync_client)
            signed = await asyncio.to_thread(
                partial(
                    client.generate_presigned_url,
                    "get_object",
                    Params=params,
                    ExpiresIn=lifetime,
                )
            )
            return str(signed)
        except StorageError:
            raise
        except Exception as exc:  # vendor errors must not escape this package
            raise _wrap("generate_presigned_url", safe_key, exc) from exc

    async def close(self) -> None:
        """Release the memoised ``boto3`` client, if one was built.

        A no-op under ``aioboto3``, whose client is scoped to each operation. Provided so a
        shutdown hook can call it unconditionally.
        """
        client = self._sync_client_instance
        self._sync_client_instance = None
        if client is None or not hasattr(client, "close"):
            return
        try:
            await asyncio.to_thread(client.close)
        except Exception as exc:
            logger.debug("storage.s3_close_failed", error=str(exc))


# --------------------------------------------------------------------------------------
# Module helpers
# --------------------------------------------------------------------------------------


def _measure_file(path: Path) -> tuple[int, str]:
    """Return a local file's size and SHA-256, in one blocking pass.

    Args:
        path: The file to measure.

    Returns:
        ``(size_bytes, sha256_hex)``.

    Raises:
        ObjectNotFoundError: If the file does not exist.
        StorageError: If it cannot be read.
    """
    if not path.is_file():
        raise ObjectNotFoundError(f"source file does not exist: {path}")
    try:
        return path.stat().st_size, sha256_file(path, chunk_size=COPY_CHUNK_BYTES)
    except OSError as exc:
        raise StorageError(f"could not read {path}: {exc}") from exc


def _close_sync_body(body: Any) -> None:
    """Close a ``botocore`` streaming body, ignoring a body that has none.

    Args:
        body: The response body.
    """
    closer = getattr(body, "close", None)
    if closer is not None:
        closer()


async def _close_async_body(body: Any) -> None:
    """Close an ``aiobotocore`` streaming body, awaiting the close when it is a coroutine.

    Args:
        body: The response body.
    """
    closer = getattr(body, "close", None)
    if closer is None:
        return
    result = closer()
    if asyncio.iscoroutine(result):
        await result


def _error_code(exc: BaseException) -> str:
    """Extract the service error code from a vendor exception.

    Args:
        exc: The exception a ``botocore``/``aiobotocore`` call raised.

    Returns:
        The code, or ``""`` when the exception carries no service response — which is what
        a connection error looks like.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return str(code) if code is not None else ""


def _is_not_found(exc: BaseException) -> bool:
    """Return whether *exc* means "no such object".

    Args:
        exc: The exception a vendor call raised.

    Returns:
        ``True`` for the codes in :data:`NOT_FOUND_CODES`, and for the SDK's own
        ``NoSuchKey`` exception class.
    """
    if _error_code(exc) in NOT_FOUND_CODES:
        return True
    return type(exc).__name__ in ("NoSuchKey", "NoSuchBucket")


def _wrap(operation: str, key: str | None, exc: BaseException) -> StorageError:
    """Translate a vendor exception into this package's error type.

    Keeping ``botocore`` exceptions inside this module is what lets every caller handle
    storage failures without importing an AWS SDK.

    Args:
        operation: The S3 operation that failed.
        key: The key involved, when there was one.
        exc: The vendor exception.

    Returns:
        An :class:`~app.storage.base.ObjectNotFoundError` for a missing object, otherwise a
        :class:`~app.storage.base.StorageError`. The message names the operation and the
        key but never the credentials.
    """
    target = f" for {key!r}" if key else ""
    if _is_not_found(exc):
        return ObjectNotFoundError(f"no object stored under {key!r}")
    return StorageError(f"s3 {operation} failed{target}: {type(exc).__name__}: {exc}")


def _content_disposition(filename: str | None) -> str | None:
    """Build a safe ``Content-Disposition`` override for a presigned link.

    Args:
        filename: The name to suggest, which is user-influenced and therefore sanitised
            down to letters, digits, dot, underscore, hyphen and space.

    Returns:
        The header value, or ``None`` when nothing usable remains.
    """
    if not filename:
        return None
    cleaned = _DISPOSITION_UNSAFE.sub("", filename).strip()[:_DISPOSITION_MAX_LENGTH].strip()
    if not cleaned:
        return None
    return f'attachment; filename="{cleaned}"'
