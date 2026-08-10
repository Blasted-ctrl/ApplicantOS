"""File schemas — the catalogue entry returned after an upload.

Uploads are the one request in the product whose *body* is not JSON, so there is no
``FileCreate``: the payload is a ``multipart/form-data`` part and FastAPI models it as
:class:`~fastapi.UploadFile`, not as a pydantic model. What this module describes is the
other half — what the caller gets back, and what it can look up later.

:class:`FileRead` deliberately exposes ``storage_key`` and ``backend``. They read like
internals, and for a hosted multi-tenant service they would be; ApplicantOS is a desktop
application whose default backend is a directory on the user's own disk, and a user who
wants to open the resume they just uploaded in another program is entitled to know where it
went. The bytes are never in the response — they come from the download route, streamed.

``sha256`` is the content address the storage layer already computes on write (golden rule
#9). It is surfaced because it is what makes an upload idempotent from the client's side:
the same file uploaded twice is the same digest, and a caller can tell without a byte-range
request.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.models.enums import DocumentKind
from app.schemas.common import Schema

__all__ = ["FileRead"]


class FileRead(Schema):
    """One catalogue entry in ``uploaded_files``.

    Attributes:
        id: The file's identifier — what ``master_resume.file_id`` and every other reference
            to a blob stores.
        user_id: Owning user, or ``None`` for a system-owned artifact such as a submission
            screenshot captured outside a user session.
        kind: What the file is. Drives retention policy and which UI lists it.
        filename: The display name, as uploaded.
        content_type: MIME type, used for the download response and for parser dispatch.
        size_bytes: Size of the stored object.
        sha256: Lowercase hexadecimal content address of the bytes.
        storage_key: Backend-relative location. Meaningless without ``backend``.
        backend: Which backend holds the bytes — ``"local"`` or ``"s3"``.
        expires_at: When the bytes may be reclaimed; ``None`` means keep indefinitely.
        created_at: When the row was written.
    """

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    kind: DocumentKind
    filename: str
    content_type: str | None = None
    size_bytes: int = Field(ge=0)
    sha256: str | None = None
    storage_key: str
    backend: str
    expires_at: datetime | None = None
    created_at: datetime
