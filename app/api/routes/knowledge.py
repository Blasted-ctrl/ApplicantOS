"""The knowledge engine's HTTP surface (``docs/CONTRACTS.md`` §14).

Golden rule #6 — knowledge is the source of truth, a resume is a generated view over it —
makes this the group a user reaches for when a resume says something they did not expect.
The endpoints follow the ingestion chain so that a surprising bullet can be traced backwards
in four hops: bullet → ``KnowledgeFact`` (``/knowledge/facts``) → the entity it is anchored
to (``/knowledge/entities``) → the source it was read from (``/knowledge/sources``).

**Indexing is queued, never awaited.** A single pass crawls repositories, a portfolio site
and a parsed resume; that is minutes of work and cannot hold a request open. Both reindex
endpoints therefore return ``202`` with the dispatch outcome, and when the broker is
unreachable they *still* return 202 with ``data.degraded`` set — the request was fine, the
worker is not running, and a 500 would misreport which.

**Two endpoints are unpaginated at the service layer and paginated here.**
:meth:`~app.services.knowledge_service.KnowledgeService.list_sources` and
:meth:`~app.services.knowledge_service.KnowledgeService.entities` return plain lists, and
their docstrings say so: sources are hand-registered (tens, not thousands) and ``entities``
explicitly documents ``limit=None`` as "what a route that paginates in memory wants". Slicing
here keeps ``Page.total`` a true count instead of a guess derived from the page length.

``GET /knowledge/search`` is the exception that cannot report a true total: reciprocal-rank
fusion over four stores has no row count, so ``total`` is the number of hits returned. The
docstring on the endpoint says so rather than leaving a client to infer it from a page that
never reports ``has_more``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession, KnowledgeServiceDep, PaginationDep
from app.api.routes._support import csv_list, require_owner
from app.api.tasks import (
    TASK_KNOWLEDGE_INDEX_ALL,
    TASK_KNOWLEDGE_INDEX_SOURCE,
    dispatch,
)
from app.models.enums import EntityKind, FactKind
from app.models.knowledge import KnowledgeFact
from app.schemas.common import OkResponse, Page, paginate
from app.schemas.knowledge import (
    EntityRead,
    FactRead,
    FactUpdate,
    GraphView,
    KnowledgeSearchResult,
    KnowledgeStats,
    SourceCreate,
    SourceRead,
)

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/knowledge"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["knowledge"]

#: Default and maximum node counts for ``GET /knowledge/graph``. A canvas drawing more than
#: the ceiling stops being readable long before it stops being renderable, and ``stats``
#: reports the whole graph's size so a truncated view can label itself honestly.
DEFAULT_GRAPH_NODES: Final[int] = 250
MAX_GRAPH_NODES: Final[int] = 2_000

#: Default and maximum hit counts for ``GET /knowledge/search``.
DEFAULT_SEARCH_HITS: Final[int] = 20
MAX_SEARCH_HITS: Final[int] = 100

router = APIRouter()


def _queue_message(subject: str, dispatched: bool) -> str:
    """Phrase the acknowledgement of a queued indexing pass.

    The message says which of the two things happened rather than always claiming success:
    a user told "queued" who then watches nothing index has been misled by the API, not by
    the worker that is not running.

    Args:
        subject: What was queued, e.g. ``"Source"``.
        dispatched: Whether the broker accepted the task.

    Returns:
        The message.
    """
    if dispatched:
        return f"{subject} queued for indexing."
    return (
        f"{subject} accepted, but the background worker is not running, so nothing has started yet."
    )


# ======================================================================================
# Sources
# ======================================================================================


@router.get(
    "/sources",
    response_model=Page[SourceRead],
    summary="Everything registered for indexing",
)
async def list_sources(
    user: CurrentUser,
    service: KnowledgeServiceDep,
    page: PaginationDep,
) -> Page[SourceRead]:
    """Return the user's registered sources, newest first.

    Paginated in memory: sources are registered by hand, so a user has tens of them and a
    second round trip to the database to count them would cost more than the slice saves.

    Args:
        user: The acting user.
        service: The knowledge-engine facade.
        page: Offset/limit pagination.

    Returns:
        One page of sources, each carrying the index status, last success and last error
        that the desktop knowledge screen renders as a health badge.
    """
    sources = await service.list_sources(user.id)
    window = sources[page.offset : page.offset + page.limit]
    return paginate(window, total=len(sources), params=page)


@router.post(
    "/sources",
    response_model=SourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a source for indexing",
    description="Identity is (user, kind, uri); re-posting the same pair updates one row.",
)
async def create_source(
    payload: SourceCreate,
    user: CurrentUser,
    service: KnowledgeServiceDep,
) -> SourceRead:
    """Register something for the indexer to read, or update the existing registration.

    A source whose ``kind`` no registered analyzer handles is rejected here — where the
    message can be acted on — rather than becoming a row that fails on every pass forever.

    Args:
        payload: The source to register.
        user: The acting user.
        service: The knowledge-engine facade.

    Returns:
        The registered source. Indexing is *not* started; call the reindex endpoint, or let
        the periodic refresh worker pick it up.

    Raises:
        ValueError: If no analyzer handles this kind — mapped to 400.
    """
    source = await service.add_source(user.id, payload)
    logger.info(
        "api.knowledge_source_registered",
        user_id=str(user.id),
        source_id=str(source.id),
        source_kind=source.kind.value,
    )
    return source


@router.delete(
    "/sources/{source_id}",
    response_model=OkResponse,
    summary="Unregister a source",
    description="Deletes its documents, chunks and vectors. Facts and entities survive.",
)
async def delete_source(
    source_id: uuid.UUID,
    user: CurrentUser,
    service: KnowledgeServiceDep,
) -> OkResponse:
    """Unregister a source and delete the text it contributed.

    **Knowledge outlives its source.** Facts and graph nodes learned from it stay: a
    verified accomplishment does not stop being true because the user unregistered the
    repository it was first read from. The documents, chunks and vectors go, because those
    are a cache of the source's text and the source is gone.

    Args:
        source_id: The source to unregister.
        user: The acting user.
        service: The knowledge-engine facade.

    Returns:
        An acknowledgement.

    Raises:
        LookupError: If no such source exists — mapped to 404.
    """
    await _require_source(service, user.id, source_id)
    await service.remove_source(source_id)
    logger.info("api.knowledge_source_removed", user_id=str(user.id), source_id=str(source_id))
    return OkResponse(message="Source unregistered. Facts learned from it were kept.")


@router.post(
    "/sources/{source_id}/reindex",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue one source for re-indexing",
)
async def reindex_source(
    source_id: uuid.UUID,
    user: CurrentUser,
    service: KnowledgeServiceDep,
    force: Annotated[
        bool,
        Query(description="Re-analyze even when the source's fingerprint has not moved."),
    ] = False,
) -> OkResponse:
    """Queue one source for an indexing pass.

    A pass whose fingerprint has not moved is skipped without analysis or embedding calls,
    which is what makes re-indexing on every launch affordable. ``force`` bypasses that,
    and is the fix for a source that indexed while a parser was broken.

    Args:
        source_id: The source to index.
        user: The acting user.
        service: The knowledge-engine facade, used to confirm the source exists first.
        force: Re-analyze, re-chunk and re-embed regardless of the fingerprint.

    Returns:
        202 with the dispatch outcome. ``data.degraded`` is true when the broker is
        unreachable — the request was accepted and can be re-run once a worker is up.

    Raises:
        LookupError: If no such source exists — mapped to 404. Checked before dispatching,
            so a typo does not become a task that fails silently on a queue.
    """
    await _require_source(service, user.id, source_id)
    outcome = await dispatch(TASK_KNOWLEDGE_INDEX_SOURCE, str(source_id), force=force)
    return OkResponse(message=_queue_message("Source", outcome.dispatched), data=outcome.as_dict())


@router.post(
    "/reindex",
    response_model=OkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue every enabled source for re-indexing",
)
async def reindex_all(
    user: CurrentUser,
    force: Annotated[
        bool,
        Query(description="Re-analyze even sources whose content has not changed."),
    ] = False,
) -> OkResponse:
    """Queue a full indexing pass over everything the user has registered.

    The indexer gives each source its own session and commits independently, so one
    unreachable repository costs that source's report and not the batch.

    Args:
        user: The acting user.
        force: Re-index even unchanged sources.

    Returns:
        202 with the dispatch outcome.
    """
    outcome = await dispatch(TASK_KNOWLEDGE_INDEX_ALL, str(user.id), force=force)
    return OkResponse(
        message=_queue_message("Every enabled source", outcome.dispatched),
        data=outcome.as_dict(),
    )


# ======================================================================================
# Facts
# ======================================================================================


@router.get(
    "/facts",
    response_model=Page[FactRead],
    summary="The fact table",
    description="Exact and substring matching with a true total. Semantic search is /search.",
)
async def list_facts(
    user: CurrentUser,
    service: KnowledgeServiceDep,
    page: PaginationDep,
    q: Annotated[str | None, Query(description="Substring of text, organization or role.")] = None,
    kind: Annotated[FactKind | None, Query(description="Restrict to one fact kind.")] = None,
    organization: Annotated[str | None, Query(description="Exact, case-insensitive.")] = None,
    role: Annotated[str | None, Query(description="Exact, case-insensitive.")] = None,
    skill: Annotated[str | None, Query(description="Matches skills or technologies.")] = None,
    active: Annotated[bool | None, Query(description="Filter on the retirement flag.")] = None,
    verified: Annotated[bool | None, Query(description="Filter on human confirmation.")] = None,
    min_impact: Annotated[int | None, Query(ge=0, le=100)] = None,
    min_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
) -> Page[FactRead]:
    """Return a filtered page of the user's facts, with a true unpaginated total.

    This is the fact *table*, not fact search: every filter is exact or substring, which is
    what lets the page report "1-50 of 812". Ranked semantic retrieval lives at
    ``GET /knowledge/search`` and deliberately cannot report a total.

    Args:
        user: The acting user.
        service: The knowledge-engine facade.
        page: Offset/limit pagination.
        q: Case-insensitive substring of the text, organization or role.
        kind: Restrict to one :class:`~app.models.enums.FactKind`.
        organization: Case-insensitive exact employer or school match.
        role: Case-insensitive exact role match.
        skill: Coarse containment against the skills and technologies arrays.
        active: ``True`` hides retired facts, ``False`` shows only them.
        verified: Restrict to facts a human has confirmed, or to those they have not.
        min_impact: Inclusive floor on the 0-100 prominence heuristic.
        min_confidence: Inclusive floor on extraction confidence.

    Returns:
        The matching page.
    """
    filters: dict[str, Any] = {
        "q": q,
        "kind": kind,
        "organization": organization,
        "role": role,
        "skill": skill,
        "active": active,
        "verified": verified,
        "min_impact": min_impact,
        "min_confidence": min_confidence,
        "limit": page.limit,
        "offset": page.offset,
    }
    supplied = {key: value for key, value in filters.items() if value is not None}
    return await service.facts(user.id, supplied)


@router.patch(
    "/facts/{fact_id}",
    response_model=FactRead,
    summary="Correct or retire one fact",
)
async def update_fact(
    fact_id: uuid.UUID,
    payload: FactUpdate,
    user: CurrentUser,
    service: KnowledgeServiceDep,
    session: DbSession,
) -> FactRead:
    """Apply a human correction to one fact.

    The user-correction path, and the reason golden rule #7 is liveable: when a resume says
    something wrong, the fix is to correct the fact rather than to edit the resume, and
    every future generation inherits the correction.

    Editing ``text``, ``organization``, ``role`` or the dates changes the fact's identity,
    so the service recomputes ``normalized_text`` and ``content_hash`` — without that,
    deduplication would silently stop working for the edited row and the next index would
    re-insert the original.

    Setting ``is_active=False`` retires a fact from generation while keeping the evidence,
    which is the right move for something true but no longer worth saying.

    Args:
        fact_id: The fact to correct.
        payload: The fields to change, applied with ``exclude_unset``.
        user: The acting user.
        service: The knowledge-engine facade.
        session: The request's database session, used only for the ownership check.

    Returns:
        The fact after the correction.

    Raises:
        HTTPException: ``404`` if no such fact exists or it belongs to another profile.
    """
    existing = await session.get(KnowledgeFact, fact_id)
    require_owner(existing.user_id if existing is not None else None, user.id, "fact")
    fact = await service.update_fact(fact_id, payload)
    logger.info(
        "api.knowledge_fact_updated",
        user_id=str(user.id),
        fact_id=str(fact_id),
        fields=sorted(payload.model_dump(exclude_unset=True)),
    )
    return fact


# ======================================================================================
# Graph
# ======================================================================================


@router.get(
    "/entities",
    response_model=Page[EntityRead],
    summary="Graph entities, most-mentioned first",
)
async def list_entities(
    user: CurrentUser,
    service: KnowledgeServiceDep,
    page: PaginationDep,
    kind: Annotated[EntityKind | None, Query(description="Restrict to one entity kind.")] = None,
) -> Page[EntityRead]:
    """Return the user's graph entities, ordered by prominence.

    Ordering by ``mention_count`` puts the technologies and employers that recur across many
    sources first, which is what a reviewer wants to see. Paginated in memory so ``total``
    is a real count — one person's graph is hundreds of entities, not millions.

    Args:
        user: The acting user.
        service: The knowledge-engine facade.
        page: Offset/limit pagination.
        kind: Restrict to one :class:`~app.models.enums.EntityKind`.

    Returns:
        One page of entities.
    """
    entities = await service.entities(user.id, kind)
    window = entities[page.offset : page.offset + page.limit]
    return paginate(window, total=len(entities), params=page)


@router.get(
    "/graph",
    response_model=GraphView,
    summary="A renderable slice of the knowledge graph",
)
async def read_graph(
    user: CurrentUser,
    service: KnowledgeServiceDep,
    kinds: Annotated[
        str | None,
        Query(description="Comma-separated entity kinds; omit for every kind."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_GRAPH_NODES)] = DEFAULT_GRAPH_NODES,
) -> GraphView:
    """Return nodes, the edges between them, and whole-graph statistics.

    Not a :class:`~app.schemas.common.Page`: a graph slice is chosen by prominence, not by
    offset, and paging one would return a second disconnected fragment rather than "more of
    the same picture". ``stats`` describes the **entire** graph so the canvas can say
    "showing 250 of 3,214 nodes" honestly instead of implying it drew everything.

    Args:
        user: The acting user.
        service: The knowledge-engine facade.
        kinds: Comma-separated :class:`~app.models.enums.EntityKind` values.
        limit: Maximum nodes to return.

    Returns:
        The slice.

    Raises:
        ValueError: If an entry in *kinds* is not a valid entity kind — mapped to 400.
    """
    return await service.graph(user.id, csv_list(kinds) or None, limit)


# ======================================================================================
# Search and statistics
# ======================================================================================


@router.get(
    "/search",
    response_model=Page[KnowledgeSearchResult],
    summary="Ranked search across facts, chunks, entities and memories",
)
async def search_knowledge(
    user: CurrentUser,
    service: KnowledgeServiceDep,
    q: Annotated[str, Query(min_length=1, description="The query text.")],
    kinds: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated store names (fact, chunk, entity, memory) to restrict "
                "which stores answer, and/or fact kinds to restrict which facts do."
            ),
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_HITS)] = DEFAULT_SEARCH_HITS,
) -> Page[KnowledgeSearchResult]:
    """Search everything the user knows, fused into one ranked list.

    Vector similarity, keyword matching and one-hop graph expansion are combined by
    reciprocal-rank fusion across four stores, so hits arrive already interleaved and
    ``kind`` says which store each came from.

    ``total`` is the number of hits returned, not a count of everything that could have
    matched: rank fusion produces a ranking, not a result set, and there is no honest row
    count to report. Clients paginate this endpoint by raising ``limit``, not by offsetting.

    Args:
        user: The acting user.
        service: The knowledge-engine facade.
        q: The query text.
        kinds: Store names, fact kinds, or both — ``fact,accomplishment`` means
            "accomplishments only".
        limit: Maximum hits.

    Returns:
        The ranked hits.

    Raises:
        ValueError: If an entry in *kinds* is neither a store name nor a fact kind —
            mapped to 400.
    """
    hits = await service.search(user.id, q, csv_list(kinds) or None, limit)
    return Page.of(hits, total=len(hits), limit=limit, offset=0)


@router.get(
    "/stats",
    response_model=KnowledgeStats,
    summary="Aggregate counts behind the knowledge dashboard",
)
async def read_stats(
    user: CurrentUser,
    service: KnowledgeServiceDep,
) -> KnowledgeStats:
    """Return the counts the knowledge dashboard renders.

    The gap between ``chunks`` and ``chunks_embedded`` is the embedding backlog, and
    ``sources_failed`` is the number of sources whose last pass ended in failure — the two
    numbers that say whether the engine is actually keeping up.

    Args:
        user: The acting user.
        service: The knowledge-engine facade.

    Returns:
        The aggregate counts.
    """
    return await service.stats(user.id)


# ======================================================================================
# Ownership checks
# ======================================================================================


async def _require_source(
    service: KnowledgeServiceDep,
    user_id: uuid.UUID,
    source_id: uuid.UUID,
) -> SourceRead:
    """Return the named source, or raise so the caller renders a 404.

    Checked through the service's own user-scoped listing rather than a direct load, so
    "belongs to another profile" and "does not exist" produce the same answer and the
    endpoint cannot be used to probe for ids.

    Args:
        service: The knowledge-engine facade.
        user_id: The acting user.
        source_id: The source being addressed.

    Returns:
        The source.

    Raises:
        LookupError: If the user has no source with that id — mapped to 404.
    """
    for source in await service.list_sources(user_id):
        if source.id == source_id:
            return source
    raise LookupError(f"knowledge source {source_id} not found")
