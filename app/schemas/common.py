"""Shared response envelopes and pagination primitives.

Three shapes recur across every route group in ``docs/CONTRACTS.md`` §14 and are therefore
defined once, here:

:class:`Page`
    The list envelope. **Every** list endpoint returns ``{items, total, limit, offset}``,
    plus the derived ``has_more`` flag so the desktop client can drive infinite scrolling
    without re-deriving the arithmetic (and getting it subtly wrong on the last page).

:class:`ErrorResponse`
    The single error shape produced by ``app.api.errors``. ``correlation_id`` is what turns
    a user's screenshot of an error toast into a log query.

:class:`OkResponse`
    The acknowledgement returned by endpoints whose only meaningful answer is "done".

:class:`PaginationParams` is the FastAPI dependency behind every list endpoint. ``limit`` is
capped at :data:`MAX_PAGE_LIMIT` at the schema level rather than inside each route, so no
caller can ask for an unbounded scan of a table that grows without limit.

Every model in :mod:`app.schemas` sets ``from_attributes=True`` so that an ORM instance can
be validated directly (``PostingRead.model_validate(posting)``) without an intermediate
dictionary.

Note:
    ``from_attributes`` reads attributes eagerly. Never name a schema field after a
    *lazily* loaded SQLAlchemy relationship: validating an ORM object inside an async
    session would trigger an implicit IO and raise ``MissingGreenlet``. Fields that mirror
    a relationship in this package either target an eagerly loaded (``selectin``) one or
    carry a default so that pydantic skips the attribute when it is absent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "ErrorResponse",
    "MAX_PAGE_LIMIT",
    "MIN_PAGE_LIMIT",
    "OkResponse",
    "Page",
    "PaginationParams",
    "Schema",
    "paginate",
]

#: Page size used when a client does not ask for one. Chosen to fill a desktop viewport
#: twice over, so the first scroll never waits on a second request.
DEFAULT_PAGE_LIMIT: int = 50

#: Hard ceiling on page size. Prevents a client (or a bug) from requesting a full-table
#: scan of ``job_postings`` or ``log_entries``, both of which grow without bound.
MAX_PAGE_LIMIT: int = 200

#: Smallest page a client may request. Zero would mean "count only", which is a different
#: endpoint, not a degenerate page.
MIN_PAGE_LIMIT: int = 1

#: Element type of a :class:`Page`.
T = TypeVar("T")


class Schema(BaseModel):
    """Base class for every ApplicantOS pydantic schema.

    Carries the one configuration flag the whole package shares — ``from_attributes``, so
    ORM instances validate directly — in a single place, so no subclass can forget it and
    no subclass has to restate it.
    """

    model_config = ConfigDict(from_attributes=True)


class PaginationParams(Schema):
    """Offset/limit pagination, used as a FastAPI dependency on every list endpoint.

    Attributes:
        limit: Maximum number of items to return. Bounded by :data:`MAX_PAGE_LIMIT`.
        offset: Number of items to skip before collecting the page.
    """

    limit: int = Field(
        default=DEFAULT_PAGE_LIMIT,
        ge=MIN_PAGE_LIMIT,
        le=MAX_PAGE_LIMIT,
        description="Maximum number of items to return.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of items to skip before this page begins.",
    )


class Page(Schema, Generic[T]):
    """A single page of results — the return shape of every list endpoint.

    Attributes:
        items: The rows in this page, already serialised to their read schema.
        total: Total number of rows matching the query, ignoring ``limit``/``offset``.
        limit: The page size that produced ``items``.
        offset: The offset that produced ``items``.
    """

    items: list[T] = Field(default_factory=list, description="Rows in this page.")
    total: int = Field(default=0, ge=0, description="Total rows matching the query.")
    limit: int = Field(default=DEFAULT_PAGE_LIMIT, ge=0, description="Page size used.")
    offset: int = Field(default=0, ge=0, description="Offset this page starts at.")

    @computed_field(description="Whether more rows exist after this page.")  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        """Whether another page follows this one.

        Derived from ``offset + len(items)`` rather than from ``offset + limit`` so that a
        short final page (fewer rows returned than requested) is reported correctly, and so
        that a caller passing an oversized ``offset`` gets ``False`` rather than a phantom
        next page.

        Returns:
            ``True`` when rows remain beyond this page.
        """
        return (self.offset + len(self.items)) < self.total

    @classmethod
    def of(
        cls,
        items: Sequence[T],
        *,
        total: int,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page[T]:
        """Build a page from an already-fetched slice and its unpaginated total.

        Args:
            items: The rows for this page.
            total: Total number of rows matching the query.
            limit: Page size that was applied.
            offset: Offset that was applied.

        Returns:
            The populated page.
        """
        return cls(items=list(items), total=total, limit=limit, offset=offset)


def paginate(items: Sequence[T], *, total: int, params: PaginationParams) -> Page[T]:
    """Wrap a fetched slice in a :class:`Page` using the request's pagination parameters.

    Exists so that route handlers never restate ``limit=params.limit, offset=params.offset``
    and never accidentally report the *page* length as the total.

    Args:
        items: The rows returned by the query, already limited and offset.
        total: Total number of rows matching the query without pagination.
        params: The pagination parameters the query was executed with.

    Returns:
        The populated page.
    """
    return Page.of(items, total=total, limit=params.limit, offset=params.offset)


class ErrorResponse(Schema):
    """The single error body every failing endpoint returns.

    Attributes:
        error: Stable machine-readable error code (``"not_found"``, ``"policy_block"``).
            Clients branch on this; it is part of the API contract and must not be a
            free-text message.
        detail: Human-readable explanation, safe to display. Never contains secrets — the
            structlog redaction processor governs what reaches this field.
        correlation_id: Identifier bound to the request's log context, so a user-reported
            failure can be traced to the exact log lines that produced it.
    """

    error: str = Field(description="Stable machine-readable error code.")
    detail: str | None = Field(default=None, description="Human-readable explanation.")
    correlation_id: str | None = Field(
        default=None,
        description="Log correlation identifier for this request.",
    )


class OkResponse(Schema):
    """Acknowledgement returned by endpoints whose only answer is "done".

    Attributes:
        ok: Always ``True``; failures are reported as :class:`ErrorResponse` with a non-2xx
            status, never as ``ok=False``.
        message: Optional human-readable note ("3 sources queued for reindexing").
        data: Optional structured payload for callers that need a result without a
            dedicated response model (counts, generated identifiers).
    """

    ok: bool = Field(default=True, description="Always true; failures raise instead.")
    message: str | None = Field(default=None, description="Optional human-readable note.")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured result payload.",
    )
