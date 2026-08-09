"""Knowledge indexing tasks — ``knowledge.*`` (``docs/CONTRACTS.md`` §15).

Indexing is the engine's heartbeat: everything a resume can say comes from it, and golden
rule #7 means an unindexed fact is a bullet that can never be written. It is also the
slowest thing the system does — a first pass crawls GitHub, a portfolio site and a resume —
so it never happens inside a request.

Four tasks:

``knowledge.index_source`` / ``knowledge.index_all``
    One source, or every enabled source a user has. Both are cheap to call repeatedly: a
    source whose fingerprint has not moved is skipped without analysis or embedding, which
    is precisely what makes indexing on every launch affordable.

``knowledge.refresh_stale``
    The hourly sweep. Re-indexes only sources whose ``auto_refresh`` is on and whose last
    pass is older than ``knowledge_reindex_interval_minutes``.

``knowledge.embed_backlog``
    The repair pass. When the embedder is unavailable — no API key yet, a provider outage —
    the indexer still writes chunks and facts, marks them unembedded and logs
    ``chunks_unembedded``. Nothing else ever comes back for them, and an unembedded chunk is
    invisible to semantic retrieval, so it is silently absent from every resume rather than
    loudly missing. This task is what closes that gap once an embedder exists again.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final, cast

import structlog
from sqlalchemy import select

from app.api.events import (
    EVENT_KNOWLEDGE_INDEX_FINISHED,
    EVENT_KNOWLEDGE_INDEX_STARTED,
    bus,
)
from app.api.tasks import (
    TASK_KNOWLEDGE_EMBED_BACKLOG,
    TASK_KNOWLEDGE_INDEX_ALL,
    TASK_KNOWLEDGE_INDEX_SOURCE,
    TASK_KNOWLEDGE_REFRESH_STALE,
)
from app.config.settings import get_settings
from app.database.session import session_scope
from app.workers import run_async, task_span
from app.workers.celery_app import celery_app
from app.workers.retry import retryable

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.database.types import EmbeddingType

__all__ = [
    "EMBED_BATCH_SIZE",
    "MAX_EMBED_PASSES",
    "embed_backlog",
    "index_all",
    "index_source",
    "refresh_stale",
]

logger = structlog.get_logger(__name__)

#: Chunks embedded per request by :func:`embed_backlog`. Matches the indexer's own batch so
#: the backlog pass costs the provider exactly what the original pass would have.
EMBED_BATCH_SIZE: Final[int] = 64

#: Safety bound on the backlog loop, so a store that silently refuses to persist vectors
#: cannot spin forever. At :data:`EMBED_BATCH_SIZE` a full pass covers a very large corpus.
MAX_EMBED_PASSES: Final[int] = 500


# ======================================================================================
# Helpers
# ======================================================================================


async def _user_ids(user_id: str | None) -> list[uuid.UUID]:
    """Resolve which users a sweep covers.

    Args:
        user_id: An explicit user, or ``None`` for every active one — how beat calls it.

    Returns:
        User ids, oldest account first.
    """
    from app.models.user import User

    async with session_scope() as session:
        statement = select(User.id).where(User.is_active.is_(True))
        if user_id is not None:
            statement = statement.where(User.id == uuid.UUID(str(user_id)))
        rows = (await session.execute(statement.order_by(User.created_at.asc()))).scalars()
        return list(rows.all())


def _report_summary(reports: list[Any]) -> dict[str, Any]:
    """Fold a list of :class:`~app.schemas.knowledge.IndexReportRead` into one mapping.

    Args:
        reports: One report per source attempted.

    Returns:
        Totals plus the per-source detail, JSON-ready.
    """
    items = [report.model_dump(mode="json") for report in reports]
    return {
        "sources": len(items),
        "skipped": sum(1 for item in items if item.get("skipped")),
        "failed": sum(1 for item in items if item.get("errors")),
        "documents": sum(int(item.get("documents") or 0) for item in items),
        "chunks": sum(int(item.get("chunks") or 0) for item in items),
        "facts": sum(int(item.get("facts") or 0) for item in items),
        "reports": items,
    }


# ======================================================================================
# knowledge.index_source
# ======================================================================================


@celery_app.task(name=TASK_KNOWLEDGE_INDEX_SOURCE, bind=False)
@retryable()
def index_source(source_id: str, *, force: bool = False) -> dict[str, Any]:
    """Run one knowledge source through the indexing pipeline.

    Args:
        source_id: The source to index.
        force: Re-analyze, re-chunk and re-embed even when the fingerprint has not moved.

    Returns:
        The :class:`~app.schemas.knowledge.IndexReportRead` as a mapping. Analyzer failures
        do not raise: the source is marked ``failed`` and the report carries the error, so
        one unreachable repository never costs a batch its other results.

    Raises:
        LookupError: If no such source exists.
    """
    with task_span(TASK_KNOWLEDGE_INDEX_SOURCE, source_id=source_id) as log:

        async def _run() -> dict[str, Any]:
            """Index one source inside one unit of work."""
            from app.services.knowledge_service import KnowledgeService

            async with session_scope() as session:
                service = KnowledgeService(session, get_settings())
                report = await service.reindex(uuid.UUID(str(source_id)), force=force)
            return report.model_dump(mode="json")

        bus.publish(EVENT_KNOWLEDGE_INDEX_STARTED, {"source_id": str(source_id), "force": force})
        outcome = run_async(_run())
        bus.publish(EVENT_KNOWLEDGE_INDEX_FINISHED, outcome)
        log.info(
            "knowledge.indexed_source",
            skipped=outcome.get("skipped"),
            documents=outcome.get("documents"),
            chunks=outcome.get("chunks"),
            facts=outcome.get("facts"),
            errors=len(outcome.get("errors") or []),
        )
        return outcome


# ======================================================================================
# knowledge.index_all
# ======================================================================================


@celery_app.task(name=TASK_KNOWLEDGE_INDEX_ALL, bind=False)
@retryable()
def index_all(user_id: str, *, force: bool = False) -> dict[str, Any]:
    """Index every enabled source one user has, one report per source.

    The task ``POST /onboarding/complete`` enqueues, which is why a first run must not raise
    on a single bad source: the indexer gives each source its own session and commits
    independently, so a failure is isolated to that source's report.

    Args:
        user_id: Owning user.
        force: Re-index even sources whose content has not changed.

    Returns:
        Totals plus one report per source.

    Raises:
        LookupError: If no user with that id exists.
    """
    with task_span(TASK_KNOWLEDGE_INDEX_ALL, user_id=user_id) as log:

        async def _run() -> dict[str, Any]:
            """Index every source for one user inside one unit of work."""
            from app.services.knowledge_service import KnowledgeService

            async with session_scope() as session:
                service = KnowledgeService(session, get_settings())
                reports = await service.reindex_all(uuid.UUID(str(user_id)), force=force)
            return _report_summary(reports)

        bus.publish(EVENT_KNOWLEDGE_INDEX_STARTED, {"user_id": str(user_id), "force": force})
        outcome = run_async(_run())
        outcome["user_id"] = str(user_id)
        bus.publish(EVENT_KNOWLEDGE_INDEX_FINISHED, outcome)
        log.info(
            "knowledge.indexed_all",
            sources=outcome.get("sources"),
            skipped=outcome.get("skipped"),
            failed=outcome.get("failed"),
            documents=outcome.get("documents"),
            facts=outcome.get("facts"),
        )
        return outcome


# ======================================================================================
# knowledge.refresh_stale
# ======================================================================================


@celery_app.task(name=TASK_KNOWLEDGE_REFRESH_STALE, bind=False)
@retryable()
def refresh_stale(user_id: str | None = None) -> dict[str, Any]:
    """Re-index sources whose refresh interval has elapsed.

    Beat runs this hourly with no arguments. Staleness and the ``auto_refresh`` opt-out are
    decided by :meth:`~app.knowledge.indexer.KnowledgeIndexer.refresh_stale`, so the schedule
    lives in one place and this task only chooses the users.

    Args:
        user_id: Refresh one user, or ``None`` for every active user.

    Returns:
        ``{"users": int, "sources": int, "reports": [...]}``.
    """
    with task_span(TASK_KNOWLEDGE_REFRESH_STALE, user_id=user_id) as log:

        async def _run(identifier: uuid.UUID) -> dict[str, Any]:
            """Refresh one user's stale sources inside one unit of work."""
            from app.knowledge.indexer import KnowledgeIndexer

            async with session_scope() as session:
                indexer = KnowledgeIndexer(session, get_settings())
                reports = await indexer.refresh_stale(identifier)
            return {
                "user_id": str(identifier),
                "sources": len(reports),
                "documents": sum(int(report.documents) for report in reports),
                "chunks": sum(int(report.chunks) for report in reports),
                "facts": sum(int(report.facts) for report in reports),
                "failed": sum(1 for report in reports if report.errors),
            }

        users = run_async(_user_ids(user_id))
        results = [run_async(_run(identifier)) for identifier in users]
        outcome = {
            "users": len(users),
            "sources": sum(int(item["sources"]) for item in results),
            "reports": results,
        }
        log.info(
            "knowledge.refreshed_stale",
            users=outcome["users"],
            sources=outcome["sources"],
        )
        return outcome


# ======================================================================================
# knowledge.embed_backlog
# ======================================================================================


def _chunk_embedding_absent(session: Any) -> Any:
    """Return the predicate matching chunks that have **no** usable vector.

    ``embedding IS NULL`` is not sufficient, and the reason is specific to
    :class:`~app.database.types.EmbeddingType`: it resolves to a pgvector ``vector`` column
    on PostgreSQL when the extension package is importable, and to a plain ``JSON`` column
    everywhere else — including PostgreSQL without pgvector. SQLAlchemy's ``JSON`` type
    persists a Python ``None`` as the JSON literal ``null``, four characters, not SQL
    ``NULL``. So on the SQLite install **every unembedded chunk has a non-NULL column**, and
    the obvious query matches nothing: this backlog would report "nothing to do" forever
    while never embedding a row.

    The dialect is asked directly rather than inferred from its name, because the name alone
    does not determine the answer. Same rule, same reasoning as
    :meth:`app.knowledge.facts.FactStore._embedding_absent` and
    :meth:`app.services.knowledge_service.KnowledgeService._embedding_present`, applied to
    the chunk column those two do not cover.

    Args:
        session: The open async session, used only for its bound dialect.

    Returns:
        A predicate true for chunks with no vector.
    """
    from sqlalchemy import JSON, String, or_, type_coerce

    from app.models.knowledge import KnowledgeChunk

    dialect = session.get_bind().dialect
    # ``Column.type`` is annotated as the base ``TypeEngine``; this column is declared with
    # :class:`~app.database.types.EmbeddingType`, and ``load_dialect_impl`` is the
    # ``TypeDecorator`` hook being asked which implementation the dialect resolved to.
    column_type = cast("EmbeddingType", KnowledgeChunk.__table__.c.embedding.type)
    implementation = column_type.load_dialect_impl(dialect)
    if isinstance(implementation, JSON):
        return or_(
            KnowledgeChunk.embedding.is_(None),
            type_coerce(KnowledgeChunk.embedding, String) == "null",
        )
    return KnowledgeChunk.embedding.is_(None)


async def _embed_chunk_backlog(user_id: uuid.UUID, batch: int) -> int:
    """Embed the user's chunks that were written without a vector.

    The chunk half of the backlog. Facts have
    :meth:`~app.knowledge.facts.FactStore.reembed_missing`; chunks do not, because the
    indexer writes them through a private path. The loop is the same shape: take the next
    *batch* unembedded chunks, embed them in one call, write the vectors to both the row and
    the vector store, repeat.

    The "has no vector" test is :func:`_chunk_embedding_absent`, which is dialect-aware for
    the reason documented there — the naive ``IS NULL`` matches nothing on SQLite.

    Args:
        user_id: Owning user.
        batch: Chunks per embedding request.

    Returns:
        How many chunks were embedded. ``0`` when there is no backlog, or when no embedder
        is available — in which case the rows are left for the next pass rather than being
        written with a zero vector, which would poison every similarity search that touched
        them.
    """
    from app.knowledge.graph import KnowledgeStore
    from app.knowledge.indexer import CHUNK_COLLECTION
    from app.knowledge.vector.base import VectorRecord
    from app.models.enums import SourceKind
    from app.models.knowledge import KnowledgeChunk, KnowledgeDocument

    size = max(1, int(batch))
    embedded = 0

    async with session_scope() as session:
        store = KnowledgeStore(session)
        absent = _chunk_embedding_absent(session)
        for _ in range(MAX_EMBED_PASSES):
            statement = (
                select(KnowledgeChunk, KnowledgeDocument)
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .where(KnowledgeDocument.user_id == user_id, absent)
                .order_by(KnowledgeChunk.created_at.asc())
                .limit(size)
            )
            rows = list((await session.execute(statement)).all())
            if not rows:
                break

            vectors = await store.embed([chunk.text for chunk, _ in rows])
            if not vectors or len(vectors) != len(rows):
                logger.warning(
                    "knowledge.chunk_backlog_unavailable",
                    user_id=str(user_id),
                    pending=len(rows),
                )
                break

            records: list[VectorRecord] = []
            for (chunk, document), vector in zip(rows, vectors, strict=True):
                chunk.embedding = list(vector)
                records.append(
                    VectorRecord(
                        id=str(chunk.id),
                        embedding=list(vector),
                        text=chunk.text,
                        metadata={
                            "user_id": str(document.user_id),
                            "kind": SourceKind(document.kind).value,
                            "document_id": str(document.id),
                            "source_id": str(document.source_id),
                            "ordinal": int(chunk.ordinal),
                            "title": document.title,
                            "uri": document.uri,
                        },
                    )
                )
            await session.flush()
            await store.vector_upsert(CHUNK_COLLECTION, records)
            embedded += len(records)
        else:  # pragma: no cover - only reachable if a backend refuses to persist
            logger.warning(
                "knowledge.chunk_backlog_pass_limit",
                user_id=str(user_id),
                passes=MAX_EMBED_PASSES,
            )

    if embedded:
        logger.info("knowledge.chunk_backlog_embedded", user_id=str(user_id), chunks=embedded)
    return embedded


@celery_app.task(name=TASK_KNOWLEDGE_EMBED_BACKLOG, bind=False)
@retryable()
def embed_backlog(
    user_id: str | None = None,
    *,
    batch: int = EMBED_BATCH_SIZE,
) -> dict[str, Any]:
    """Embed the chunks and facts that were stored without a vector.

    Run this after adding an API key to a zero-key install, after an embedding provider
    outage, or after changing the embedding model. Until it runs, the affected chunks and
    facts exist in the database and are invisible to semantic retrieval.

    Args:
        user_id: One user, or ``None`` for every active user.
        batch: Rows per embedding request.

    Returns:
        ``{"users": int, "chunks": int, "facts": int, "by_user": [...]}``.
    """
    with task_span(TASK_KNOWLEDGE_EMBED_BACKLOG, user_id=user_id) as log:

        async def _facts(identifier: uuid.UUID) -> int:
            """Embed one user's unembedded facts inside one unit of work."""
            from app.knowledge.facts import FactStore

            async with session_scope() as session:
                return await FactStore(session).reembed_missing(identifier, batch=batch)

        users = run_async(_user_ids(user_id))
        by_user: list[dict[str, Any]] = []
        for identifier in users:
            chunks = run_async(_embed_chunk_backlog(identifier, batch))
            facts = run_async(_facts(identifier))
            by_user.append({"user_id": str(identifier), "chunks": chunks, "facts": facts})

        outcome = {
            "users": len(users),
            "chunks": sum(int(item["chunks"]) for item in by_user),
            "facts": sum(int(item["facts"]) for item in by_user),
            "by_user": by_user,
        }
        log.info(
            "knowledge.embed_backlog_finished",
            users=outcome["users"],
            chunks=outcome["chunks"],
            facts=outcome["facts"],
        )
        return outcome
