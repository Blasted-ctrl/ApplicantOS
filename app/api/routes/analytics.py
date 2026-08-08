"""Analytics — the funnel, the activity series and the insights panel (§14).

Every number here is computed server-side and the desktop client derives nothing. Two
reasons, and both are about correctness rather than convenience: a client that computes its
own conversion rate will eventually compute a different one from the server's, and the
aggregations run against indexes the client cannot see.

``GET /analytics/overview`` bundles all four panels so the analytics screen paints in one
request. The three narrower endpoints exist for clients refreshing a single panel — a live
dashboard re-reads ``/timeseries`` on a session tick and has no use for the insight
computation behind it.

**Insights are observational and say so.** They report which providers, score bands and
cover-letter choices *preceded* an interview; nothing here establishes cause, and
:attr:`~app.schemas.dashboard.InsightItem.sample_size` travels with every item precisely so
that a pattern drawn from three applications cannot be presented like one drawn from three
hundred. Items below :data:`~app.schemas.dashboard.MIN_INSIGHT_SAMPLE_SIZE` are still
returned — hiding them would make the panel look arbitrarily empty — and carry
``is_significant=False`` so the client can mark them provisional.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Query

from app.api.deps import AnalyticsServiceDep, CurrentUser
from app.schemas.common import Page
from app.schemas.dashboard import (
    AnalyticsOverview,
    FunnelStage,
    InsightItem,
    TimeseriesPoint,
)

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/analytics"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["analytics"]

#: Width of the activity window when the caller does not ask for one. Thirty days is long
#: enough to show a trend and short enough that a fresh install is not mostly empty buckets.
DEFAULT_WINDOW_DAYS: Final[int] = 30

#: Widest window a request may ask for. The service clamps to its own ceiling as well; this
#: bound exists so an absurd value is rejected at the boundary with a 422 rather than
#: silently reinterpreted.
MAX_WINDOW_DAYS: Final[int] = 365

#: Query parameter shared by the endpoints that take a window.
WindowDays = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_WINDOW_DAYS,
        description="Length of the activity window, in days.",
    ),
]

router = APIRouter()


@router.get(
    "/overview",
    response_model=AnalyticsOverview,
    summary="Every analytics panel in one payload",
)
async def overview(
    user: CurrentUser,
    service: AnalyticsServiceDep,
    days: WindowDays = DEFAULT_WINDOW_DAYS,
) -> AnalyticsOverview:
    """Return the tiles, the funnel, the activity series, the insights and the breakdowns.

    ``generated_at`` is stamped so the client can show staleness rather than implying a
    cached panel is live.

    Args:
        user: The acting user.
        service: The analytics aggregator.
        days: Width of the activity window.

    Returns:
        The whole payload. Rates are unit-interval fractions, never percentages — the client
        formats, because the server should not guess at locale or precision.
    """
    return await service.full_overview(user.id, days)


@router.get(
    "/funnel",
    response_model=Page[FunnelStage],
    summary="The discovery to offer funnel",
)
async def funnel(
    user: CurrentUser,
    service: AnalyticsServiceDep,
) -> Page[FunnelStage]:
    """Return one entry per funnel stage, in order.

    Both ratios are reported because they answer different questions and neither can be
    derived from the other without the whole series: ``conversion_rate`` ("of everything that
    reached the previous stage, how much got here?") localises where the pipeline leaks,
    while ``share_of_total`` sizes the leak.

    A fixed, short, ordered series — but still a :class:`~app.schemas.common.Page`, because
    every list endpoint in this API returns one and a client should not have to remember
    which shape each returns. ``total`` is the number of stages.

    Args:
        user: The acting user.
        service: The analytics aggregator.

    Returns:
        The stages.
    """
    stages = await service.funnel(user.id)
    return Page.of(stages, total=len(stages), limit=len(stages) or 1, offset=0)


@router.get(
    "/timeseries",
    response_model=Page[TimeseriesPoint],
    summary="Day-by-day activity",
)
async def timeseries(
    user: CurrentUser,
    service: AnalyticsServiceDep,
    days: WindowDays = DEFAULT_WINDOW_DAYS,
) -> Page[TimeseriesPoint]:
    """Return one point per day, oldest first, with empty days present and zeroed.

    Gaps are filled rather than omitted: a chart with missing days silently rescales its
    x-axis and lies about the shape of a run. Buckets are the server's **local** calendar
    day, because "what did I do on Tuesday?" is a human question.

    Args:
        user: The acting user.
        service: The analytics aggregator.
        days: Width of the window.

    Returns:
        The series.
    """
    points = await service.timeseries(user.id, days)
    return Page.of(points, total=len(points), limit=len(points) or 1, offset=0)


@router.get(
    "/insights",
    response_model=Page[InsightItem],
    summary="What correlates with interviews",
)
async def insights(
    user: CurrentUser,
    service: AnalyticsServiceDep,
) -> Page[InsightItem]:
    """Return observational findings about what preceded an interview.

    Strictly correlational, and the schema is built to keep it that way: ``detail`` is
    written to say so, ``sample_size`` travels with every item, and ``is_significant`` is a
    blunt threshold rather than a p-value — the underlying data is self-selected, so a formal
    significance claim would be more misleading than "we have not seen enough of these yet".

    Args:
        user: The acting user.
        service: The analytics aggregator.

    Returns:
        The findings.
    """
    items = await service.what_gets_interviews(user.id)
    return Page.of(items, total=len(items), limit=len(items) or 1, offset=0)
