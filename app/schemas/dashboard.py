"""Dashboard and analytics schemas.

Four screens read from these models — the home dashboard, the funnel, the timeseries chart,
and the insights panel — and they share one design decision: **every number is precomputed
server-side**. The desktop client renders what it is given and derives nothing. Two reasons:
a client that computes its own conversion rate will eventually compute a different one from
the server's, and the aggregations run against indexes the client cannot see.

* :class:`DashboardStats` is the tile row on the home screen. The seven counters are frozen
  by the module brief: ``applications_today``, ``interviews``, ``pending``, ``rejected``,
  ``needs_review``, ``offers``, ``total``.
* :class:`FunnelStage` is one bar of the discovery → offer funnel, carrying both its
  stage-to-stage conversion and its share of the top of the funnel, because those answer
  different questions and neither can be derived from the other without the whole series.
* :class:`TimeseriesPoint` is one day. Counters are explicit fields rather than a generic
  ``{series, value}`` pair so the chart binds directly and a missing series is visibly zero
  rather than silently absent.
* :class:`InsightItem` is one finding from
  :meth:`app.services.analytics_service.AnalyticsService.what_gets_interviews` —
  observational, never causal, and always carrying its sample size so a pattern drawn from
  three applications is not presented like one drawn from three hundred.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import Field, computed_field

from app.schemas.common import Schema

__all__ = [
    "AnalyticsOverview",
    "DashboardStats",
    "FunnelStage",
    "InsightItem",
    "InsightSentiment",
    "MIN_INSIGHT_SAMPLE_SIZE",
    "TimeseriesPoint",
]

#: How an insight should be rendered: as something working, something not working, or a
#: neutral observation.
InsightSentiment = Literal["positive", "negative", "neutral"]

#: Below this many observations an insight is noise. The analytics service still emits the
#: item — hiding it would make the panel look arbitrarily empty — but tags it so the UI can
#: mark it provisional, and so no user reads a two-application coincidence as a finding.
MIN_INSIGHT_SAMPLE_SIZE: int = 10

#: Rates are unit-interval fractions everywhere in this module, never percentages. The
#: client formats; the server does not guess at locale or precision.
_RATE_FLOOR: float = 0.0
_RATE_CEILING: float = 1.0


class DashboardStats(Schema):
    """The counter tiles on the home screen.

    ``pending`` counts applications still in flight — anything submitted or confirmed with
    no outcome recorded yet. ``total`` is every application ever created, including drafts,
    so the tiles do not silently exclude work that failed before submission.
    """

    applications_today: int = Field(
        default=0,
        description="Applications submitted since local midnight; the daily-cap numerator.",
    )
    interviews: int = Field(default=0, description="Applications that reached interview.")
    pending: int = Field(
        default=0,
        description="Submitted or confirmed with no outcome recorded yet.",
    )
    rejected: int = Field(default=0)
    needs_review: int = Field(
        default=0,
        description="Applications parked awaiting a human; the review queue depth.",
    )
    offers: int = Field(default=0)
    total: int = Field(default=0, description="Every application ever created.")


class TimeseriesPoint(Schema):
    """One day of activity.

    ``date`` is a calendar date in the server's local timezone, because "applications today"
    is a human question about a human day, not a UTC-midnight-to-UTC-midnight window.
    """

    date: datetime.date
    discovered: int = Field(default=0, description="Postings discovered that day.")
    scored: int = Field(default=0, description="Postings scored that day.")
    applications: int = Field(default=0, description="Applications created that day.")
    submitted: int = Field(default=0, description="Applications submitted that day.")
    interviews: int = Field(default=0)
    offers: int = Field(default=0)
    rejections: int = Field(default=0)


class FunnelStage(Schema):
    """One stage of the discovery → offer funnel.

    Both ratios are reported because they answer different questions: ``conversion_rate``
    ("of everything that reached the previous stage, how much got here?") localises where
    the pipeline leaks, while ``share_of_total`` ("of everything discovered, how much got
    here?") sizes the leak.
    """

    key: str = Field(description="Stable stage identifier, e.g. 'discovered', 'submitted'.")
    label: str = Field(default="", description="Human-facing stage name.")
    count: int = Field(default=0, ge=0)
    conversion_rate: float = Field(
        default=_RATE_FLOOR,
        ge=_RATE_FLOOR,
        le=_RATE_CEILING,
        description="Fraction of the previous stage that reached this one.",
    )
    share_of_total: float = Field(
        default=_RATE_FLOOR,
        ge=_RATE_FLOOR,
        le=_RATE_CEILING,
        description="Fraction of the top of the funnel that reached this stage.",
    )


class InsightItem(Schema):
    """One observational finding about what correlates with interviews.

    Strictly correlational. The system observes which postings, companies, providers and
    resume choices preceded an interview; it does not and cannot establish cause. ``detail``
    is written to say so, and ``sample_size`` travels with every item so a finding drawn
    from three applications cannot be presented like one drawn from three hundred.
    """

    key: str = Field(description="Stable identifier, so the client can dismiss or pin it.")
    title: str = Field(description="One-line finding.")
    detail: str | None = Field(default=None, description="Supporting explanation.")
    sentiment: InsightSentiment = Field(
        default="neutral",  # type: ignore[assignment]
        description="Whether this reads as something working, failing, or neutral.",
    )
    metric: float | None = Field(
        default=None,
        description="The number the finding is about (a rate, a count, a mean).",
    )
    sample_size: int = Field(
        default=0,
        ge=0,
        description="Observations behind the finding; below MIN_INSIGHT_SAMPLE_SIZE it is provisional.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured backing data so the client can deep-link to the evidence.",
    )

    @computed_field(description="Whether the sample is large enough to be more than noise.")  # type: ignore[prop-decorator]
    @property
    def is_significant(self) -> bool:
        """Whether ``sample_size`` reaches :data:`MIN_INSIGHT_SAMPLE_SIZE`.

        A single flag rather than a p-value: the underlying data is observational and
        self-selected, so a formal significance claim would be more misleading than a
        blunt "we have not seen enough of these yet".
        """
        return self.sample_size >= MIN_INSIGHT_SAMPLE_SIZE


class AnalyticsOverview(Schema):
    """The full analytics payload — ``GET /analytics/overview``.

    Bundles the four views so the analytics screen paints in one request. The individual
    endpoints (``/funnel``, ``/timeseries``, ``/insights``) return the corresponding
    sub-collections for clients that refresh one panel at a time.
    """

    stats: DashboardStats = Field(default_factory=DashboardStats)
    funnel: list[FunnelStage] = Field(default_factory=list)
    timeseries: list[TimeseriesPoint] = Field(default_factory=list)
    insights: list[InsightItem] = Field(default_factory=list)

    by_provider: dict[str, int] = Field(
        default_factory=dict,
        description="Applications per ATS provider.",
    )
    by_company: dict[str, int] = Field(
        default_factory=dict,
        description="Applications per company, keyed by display name.",
    )
    by_status: dict[str, int] = Field(
        default_factory=dict,
        description="Applications per ApplicationStatus value.",
    )

    response_rate: float = Field(
        default=_RATE_FLOOR,
        ge=_RATE_FLOOR,
        le=_RATE_CEILING,
        description="Fraction of submitted applications that got any employer response.",
    )
    interview_rate: float = Field(
        default=_RATE_FLOOR,
        ge=_RATE_FLOOR,
        le=_RATE_CEILING,
        description="Fraction of submitted applications that reached interview.",
    )
    offer_rate: float = Field(
        default=_RATE_FLOOR,
        ge=_RATE_FLOOR,
        le=_RATE_CEILING,
        description="Fraction of submitted applications that reached offer.",
    )
    avg_application_seconds: float | None = Field(
        default=None,
        description="Mean wall-clock duration of a completed application.",
    )
    avg_score: float | None = Field(
        default=None,
        description="Mean normalised score of the postings applied to.",
    )
    tokens_used: dict[str, int] = Field(
        default_factory=dict,
        description="LLM token totals by model or usage kind, over the window.",
    )
    window_days: int = Field(
        default=0,
        ge=0,
        description="Length of the window these aggregates cover.",
    )
    generated_at: datetime.datetime | None = Field(
        default=None,
        description="When this payload was computed; lets the client show staleness.",
    )
