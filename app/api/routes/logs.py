"""The product's own log viewer (``docs/CONTRACTS.md`` §14).

ApplicantOS runs on a user's machine, where "check the server logs" is not an instruction
anyone can act on. So structured events are persisted to ``log_entries`` and read back here,
and the three filters this endpoint exposes are the three questions a user actually asks:

``correlation_id``
    "Here is the id from the error toast — what happened?" This is the highest-value filter
    in the API. Every response carries the id in ``X-Correlation-ID`` and every error body
    repeats it, so a screenshot of a failure becomes a log query.
``level``
    "Show me only what went wrong."
``event``
    "Show me every submission attempt" — the dotted event name is the stable identifier of
    *what happened*, deliberately separate from the human-readable message.

**Redaction happened upstream, and this endpoint depends on it.** ``payload`` has already
been through :func:`app.config.logging.redact_secrets` before it reached the table — golden
rule #4 applies to the database sink exactly as it does to stdout — so what is read back
carries no password, token, API key or cookie. This module therefore returns the payload as
stored rather than re-scrubbing it: a second, weaker scrub here would create the impression
that the write path did not need one.

Ordering is newest first. This is the one list in the API where the most recent row is the
one being looked for, because a log is read backwards from the moment something broke.

The read model is declared in this module rather than in :mod:`app.schemas`: ``log_entries``
is an operational table with no desktop-side type mirror, and the same reasoning puts
``HealthReport`` in :mod:`app.api.routes.health`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import Select, func, select

from app.api.deps import DbSession, PaginationDep
from app.api.routes._support import like_pattern
from app.models.log import LogEntry
from app.schemas.common import Page, Schema, paginate

__all__ = ["PREFIX", "TAGS", "LogEntryRead", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/logs"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["logs"]

router = APIRouter()


class LogEntryRead(Schema):
    """One persisted structured log event.

    Attributes:
        id: Row identity.
        level: Level name as emitted — ``"info"``, ``"warning"``, ``"error"``.
        event: Short dotted name; the stable identifier of what happened, and what a client
            should branch on. The prose lives in ``payload``, not here.
        logger: Emitting logger, conventionally the producing module.
        correlation_id: Request or task correlation identifier. The join key between an
            error a user saw and the lines that produced it.
        session_id: Run this line belongs to, when there was one.
        application_id: Application this line belongs to, when there was one.
        posting_id: Posting this line belongs to, when there was one.
        payload: Remaining structured context, already secret-redacted at write time.
        at: When the event was emitted, which may precede when the row was written.
        created_at: When the row was written.
    """

    id: uuid.UUID
    level: str = Field(description="Level name as emitted.")
    event: str = Field(description="Short dotted event name; the stable identifier.")
    logger: str | None = None
    correlation_id: str | None = Field(
        default=None,
        description="Correlation identifier echoed in X-Correlation-ID and error bodies.",
    )
    session_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    posting_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured context, already redacted before it reached the table.",
    )
    at: datetime = Field(description="When the event was emitted.")
    created_at: datetime


def _base_query(
    level: str | None,
    event: str | None,
    correlation_id: str | None,
    session_id: uuid.UUID | None,
    application_id: uuid.UUID | None,
    since: datetime | None,
) -> Select[Any]:
    """Build the ``WHERE`` clause behind ``GET /logs``.

    Args:
        level: Level name, matched case-insensitively and exactly — a level is a closed
            vocabulary, so a substring match would only ever be a mistake.
        event: Event name prefix or substring, so ``pipeline.`` selects a whole stage.
        correlation_id: Exact match; this is an opaque identifier and a partial one is
            meaningless.
        session_id: Restrict to one run.
        application_id: Restrict to one application.
        since: Only entries emitted at or after this instant.

    Returns:
        The filtered statement, without ordering or paging.
    """
    statement: Select[Any] = select(LogEntry)

    if level:
        statement = statement.where(func.lower(LogEntry.level) == level.strip().lower())
    if event:
        statement = statement.where(LogEntry.event.ilike(like_pattern(event), escape="\\"))
    if correlation_id:
        statement = statement.where(LogEntry.correlation_id == correlation_id.strip())
    if session_id is not None:
        statement = statement.where(LogEntry.session_id == session_id)
    if application_id is not None:
        statement = statement.where(LogEntry.application_id == application_id)
    if since is not None:
        statement = statement.where(LogEntry.at >= since)

    return statement


@router.get(
    "",
    response_model=Page[LogEntryRead],
    summary="Structured log entries, newest first",
)
async def list_logs(
    session: DbSession,
    page: PaginationDep,
    level: Annotated[
        str | None,
        Query(description="Level name; matched exactly, case-insensitively."),
    ] = None,
    event: Annotated[
        str | None,
        Query(description="Event name substring, e.g. 'pipeline.' for a whole stage."),
    ] = None,
    correlation_id: Annotated[
        str | None,
        Query(description="Exact correlation id, as shown on an error toast."),
    ] = None,
    session_id: Annotated[uuid.UUID | None, Query(description="Restrict to one run.")] = None,
    application_id: Annotated[
        uuid.UUID | None,
        Query(description="Restrict to one application."),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(description="Only entries emitted at or after this instant."),
    ] = None,
) -> Page[LogEntryRead]:
    """Return a filtered page of log entries.

    Not user-scoped. ``log_entries`` records the behaviour of the *process* — provider
    failures, render errors, dispatch timeouts — and most of those lines belong to no user
    at all. Scoping them to the acting profile would hide exactly the entries someone
    debugging a failure needs. On a single-user desktop install this is the whole log; the
    correlation, session and application filters are how it is narrowed.

    Args:
        session: The request's database session.
        page: Offset/limit pagination, bounded by ``MAX_PAGE_LIMIT`` so no request can scan
            a table that grows without bound.
        level: Level name.
        event: Event name substring.
        correlation_id: Exact correlation id.
        session_id: Restrict to one run.
        application_id: Restrict to one application.
        since: Lower bound on emission time.

    Returns:
        The page, newest first — a log is read backwards from the moment something broke.
    """
    statement = _base_query(level, event, correlation_id, session_id, application_id, since)

    total = await session.scalar(select(func.count()).select_from(statement.subquery()))

    rows = await session.execute(
        statement.order_by(LogEntry.at.desc(), LogEntry.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    entries = [LogEntryRead.model_validate(row) for row in rows.scalars().all()]
    return paginate(entries, total=int(total or 0), params=page)
