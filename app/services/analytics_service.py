"""Analytics — the numbers behind the dashboard, and their honest error bars (§13).

Every figure this service returns is computed server-side and handed to the client already
finished. The desktop app derives nothing: a client that computes its own conversion rate
will eventually compute a different one from the server's, and these aggregations run against
indexes the client cannot see.

**The whole module is one aggregate query per question.** Counts come from ``func.count`` with
``group_by``; rates come from conditional aggregation (``SUM(CASE WHEN … THEN 1 ELSE 0 END)``)
so a rate and its denominator are read in the same pass. Nothing here loops a query over rows.
:meth:`AnalyticsService.timeseries` is the one place bucketing happens in Python, for the
reason :meth:`app.services.application_service.ApplicationService.timeseries` already
documents: ``date_trunc`` is PostgreSQL-only and ``strftime`` is SQLite-only, so a
dialect-specific query would make the chart work on one backend and fail on the other. The
row count there is bounded by :data:`MAX_TIMESERIES_DAYS` of one user's activity.

Two day boundaries appear in this module and the difference is deliberate.
``applications_today`` uses the **UTC** calendar day because it is the numerator of the daily
cap, and :meth:`app.services.application_service.ApplicationService.daily_count` — the
enforcement — counts the UTC day. A tile that disagrees with the limit it is reporting against
is worse than a tile in the wrong timezone. :meth:`AnalyticsService.timeseries` buckets by the
**server's local** calendar day, because "what did I do on Tuesday?" is a human question about
a human day and nothing is enforced against it.

**On insights: report the measurement, never act on it.**
:meth:`AnalyticsService.what_gets_interviews` is strictly observational. One user sends a
couple of hundred applications and gets a handful of interviews, so every finding here rests
on a sample that would embarrass a statistician. That is why each
:class:`~app.schemas.dashboard.InsightItem` carries its ``sample_size``, why its ``context``
carries an explicit ``confidence`` that is :data:`CONFIDENCE_LOW` below
:data:`~app.schemas.dashboard.MIN_INSIGHT_SAMPLE_SIZE` observations, and why the prose says so
out loud. Nothing in ApplicantOS reads these items back to change a setting. An auto-tuner
that switches resume templates on ``n=4`` is worse than no auto-tuner at all, because from
that moment the user can no longer explain why their resume changed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import Select, case, func, select

from app.database.types import utcnow
from app.models.application import Application
from app.models.company import Company
from app.models.enums import (
    APPLICATION_POST_SUBMIT_STATES,
    ApplicationStatus,
    ATSProviderName,
)
from app.models.posting import JobPosting
from app.models.score import JobScore
from app.models.session import RunSession
from app.schemas.dashboard import (
    MIN_INSIGHT_SAMPLE_SIZE,
    AnalyticsOverview,
    DashboardStats,
    FunnelStage,
    InsightItem,
    InsightSentiment,
    TimeseriesPoint,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "CONFIDENT_INSIGHT_SAMPLE_SIZE",
    "DEFAULT_BREAKDOWN_LIMIT",
    "DEFAULT_TIMESERIES_DAYS",
    "FUNNEL_STAGES",
    "INTERVIEW_OUTCOME_STATES",
    "MAX_TIMESERIES_DAYS",
    "SCORE_BANDS",
    "UNKNOWN_LABEL",
    "AnalyticsService",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Vocabulary
# ======================================================================================

#: Default width of the activity chart, matching the dashboard's default range.
DEFAULT_TIMESERIES_DAYS: Final[int] = 30

#: Hard ceiling on a series, so a hand-crafted query string cannot ask the process to build a
#: ten-year day-by-day chart in memory.
MAX_TIMESERIES_DAYS: Final[int] = 365

#: Rows returned by a breakdown (:meth:`AnalyticsService.by_company`) when the caller does not
#: ask for a size. A dashboard panel shows a top-N list, never every employer ever seen.
DEFAULT_BREAKDOWN_LIMIT: Final[int] = 20

#: Key used for a grouping whose label is missing — an application whose company was purged,
#: a posting with no resolved employer. Named rather than blank so the row is still clickable.
UNKNOWN_LABEL: Final[str] = "Unknown"

#: Statuses that mean the application reached at least an interview. ``offer`` is included
#: because status is a *current* state, not a history: an application now holding an offer
#: went through the interview stage, and excluding it would make the funnel non-monotonic and
#: understate the interview rate exactly where the good news is.
INTERVIEW_OUTCOME_STATES: Final[tuple[ApplicationStatus, ...]] = (
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.OFFER,
)

#: Statuses that mean the employer replied at all, in any direction.
_RESPONDED_STATES: Final[tuple[ApplicationStatus, ...]] = (
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.OFFER,
    ApplicationStatus.REJECTED,
)

#: Deterministic ordering of the post-submit statuses for ``IN`` clauses. Frozenset iteration
#: order varies between processes, which would defeat the compiled-statement cache.
_POST_SUBMIT_ORDERED: Final[tuple[ApplicationStatus, ...]] = tuple(
    sorted(APPLICATION_POST_SUBMIT_STATES, key=lambda status: status.value)
)

#: The funnel, in order: ``(key, label)``. Frozen here so the chart's stage vocabulary is one
#: constant rather than a string literal repeated across the service and the client.
FUNNEL_STAGES: Final[tuple[tuple[str, str], ...]] = (
    ("discovered", "Discovered"),
    ("scored", "Scored"),
    ("prepared", "Prepared"),
    ("submitted", "Submitted"),
    ("interview", "Interview"),
    ("offer", "Offer"),
)

#: Score bands used by the insight that asks whether higher-scoring roles reply more often.
#: Three bands, not ten: with a couple of hundred applications, ten buckets hold twenty
#: applications each and every one of them is noise. ``(key, label, low, high)``, inclusive.
SCORE_BANDS: Final[tuple[tuple[str, str, int, int], ...]] = (
    ("below_70", "Below 70", 0, 69),
    ("70_84", "70–84", 70, 84),
    ("85_plus", "85 and above", 85, 100),
)

#: Confidence labels reported in :attr:`~app.schemas.dashboard.InsightItem.context`.
CONFIDENCE_LOW: Final[str] = "low"
CONFIDENCE_MEDIUM: Final[str] = "medium"
CONFIDENCE_HIGH: Final[str] = "high"

#: Observations above which an insight is reported as :data:`CONFIDENCE_HIGH`. Still not a
#: significance claim — the data is observational and self-selected — just the point past
#: which the number stops moving every time one more employer replies.
CONFIDENT_INSIGHT_SAMPLE_SIZE: Final[int] = 30

#: Sentence appended to any insight drawn from fewer than
#: :data:`~app.schemas.dashboard.MIN_INSIGHT_SAMPLE_SIZE` observations. The copy says it
#: because a number rendered without its sample size gets read as a finding.
_PROVISIONAL_NOTE: Final[str] = (
    "Based on only {n} application(s), so this is a hint rather than a finding — one more "
    "reply would move it noticeably."
)

#: Rounding applied to every rate. Four places is more than a percentage needs and keeps the
#: client free to format as it likes.
_RATE_PRECISION: Final[int] = 4


class AnalyticsService:
    """Computes the dashboard, the funnel, the activity chart and the insights.

    Read-only: nothing in this class writes a row, and — importantly — nothing reads its
    output back to change a setting. See the module docstring on why measuring and tuning are
    kept apart.

    Args:
        session: The unit of work. Nothing here commits.

    Usage::

        analytics = AnalyticsService(session)
        stats = await analytics.overview(user.id)
        insights = await analytics.what_gets_interviews(user.id)
    """

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to one session."""
        self._session = session

    # ----------------------------------------------------------------------------------
    # Dashboard
    # ----------------------------------------------------------------------------------

    async def overview(self, user_id: uuid.UUID | str) -> DashboardStats:
        """Return the counter tiles on the home screen.

        Two queries: one ``GROUP BY status`` over the user's applications, and one count of
        today's submissions.

        ``applications_today`` counts the **UTC** calendar day, matching
        :meth:`app.services.application_service.ApplicationService.daily_count`. The schema
        describes the tile as the daily-cap numerator, and a numerator measured over a
        different window from its own limit is a bug however nicely it renders.

        Args:
            user_id: The applicant.

        Returns:
            The populated tiles; every counter is ``0`` on a fresh install rather than
            ``None``, so the dashboard renders numbers instead of dashes.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        counts = await self._status_counts(identifier)

        today = await self._session.scalar(
            select(func.count(Application.id)).where(
                Application.user_id == identifier,
                Application.submitted_at.is_not(None),
                Application.submitted_at >= _start_of_utc_day(),
            )
        )

        return DashboardStats(
            applications_today=int(today or 0),
            interviews=counts.get(ApplicationStatus.INTERVIEW, 0),
            pending=(
                counts.get(ApplicationStatus.SUBMITTED, 0)
                + counts.get(ApplicationStatus.CONFIRMED, 0)
            ),
            rejected=counts.get(ApplicationStatus.REJECTED, 0),
            needs_review=counts.get(ApplicationStatus.NEEDS_REVIEW, 0),
            offers=counts.get(ApplicationStatus.OFFER, 0),
            total=sum(counts.values()),
        )

    # ----------------------------------------------------------------------------------
    # Funnel
    # ----------------------------------------------------------------------------------

    async def funnel(self, user_id: uuid.UUID | str) -> list[FunnelStage]:
        """Return the discovery → offer funnel, one bar per stage.

        Three queries: the posting pool, this user's scores, and this user's applications
        grouped by status.

        ``discovered`` counts the **whole** posting pool. Postings are not user-owned —
        discovery is shared, and two users looking at the same Greenhouse board see one row —
        so scoping it per user would either double-count the pool or make the first stage
        identical to the second and say nothing.

        Both ratios are reported because they answer different questions: ``conversion_rate``
        localises where the pipeline leaks, ``share_of_total`` sizes the leak. Both are clamped
        into ``[0, 1]``: a posting applied to before it was ever scored would otherwise produce
        a ratio above one and fail the schema's own validation, taking the dashboard down over
        an accounting quirk.

        Args:
            user_id: The applicant.

        Returns:
            One stage per entry of :data:`FUNNEL_STAGES`, in order.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")

        discovered = int(await self._session.scalar(select(func.count(JobPosting.id))) or 0)
        scored = int(
            await self._session.scalar(
                select(func.count(JobScore.id)).where(JobScore.user_id == identifier)
            )
            or 0
        )
        counts = await self._status_counts(identifier)

        totals: dict[str, int] = {
            "discovered": discovered,
            "scored": scored,
            "prepared": sum(counts.values()),
            "submitted": sum(counts.get(status, 0) for status in _POST_SUBMIT_ORDERED),
            "interview": sum(counts.get(status, 0) for status in INTERVIEW_OUTCOME_STATES),
            "offer": counts.get(ApplicationStatus.OFFER, 0),
        }

        top = totals["discovered"]
        stages: list[FunnelStage] = []
        previous: int | None = None
        for key, label in FUNNEL_STAGES:
            count = totals[key]
            stages.append(
                FunnelStage(
                    key=key,
                    label=label,
                    count=count,
                    conversion_rate=1.0 if previous is None else _ratio(count, previous),
                    share_of_total=_ratio(count, top),
                )
            )
            previous = count
        return stages

    # ----------------------------------------------------------------------------------
    # Activity over time
    # ----------------------------------------------------------------------------------

    async def timeseries(
        self,
        user_id: uuid.UUID | str,
        days: int = DEFAULT_TIMESERIES_DAYS,
    ) -> list[TimeseriesPoint]:
        """Return day-by-day activity, oldest first.

        Three queries, each fetching only timestamps; the day bucketing happens in Python
        because there is no portable SQL spelling for it (see the module docstring). Days with
        no activity are present and zeroed — a chart with gaps silently rescales its x-axis and
        lies about the shape of a run.

        Buckets are the **server's local** calendar day, which is what
        :class:`~app.schemas.dashboard.TimeseriesPoint` declares: "what did I do on Tuesday?"
        is a human question, and nothing is enforced against this series.

        Interviews, offers and rejections are counted on ``updated_at`` — the day the
        application *became* that — because ``status`` records the current state and the
        database keeps no transition log outside
        :class:`~app.models.application.ApplicationEvent`. It is an approximation, and it is
        the same one :meth:`app.services.application_service.ApplicationService.timeseries`
        makes.

        Args:
            user_id: The applicant.
            days: Window width, clamped to ``1..``:data:`MAX_TIMESERIES_DAYS`.

        Returns:
            One point per day in the window.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        window = max(1, min(int(days or DEFAULT_TIMESERIES_DAYS), MAX_TIMESERIES_DAYS))

        today = dt.datetime.now().astimezone().date()
        first_day = today - dt.timedelta(days=window - 1)
        start = _start_of_local_day(first_day)

        buckets: dict[dt.date, TimeseriesPoint] = {
            first_day + dt.timedelta(days=offset): TimeseriesPoint(
                date=first_day + dt.timedelta(days=offset)
            )
            for offset in range(window)
        }

        for (created_at,) in (
            await self._session.execute(
                select(JobPosting.created_at).where(JobPosting.created_at >= start)
            )
        ).all():
            point = buckets.get(_local_day(created_at))
            if point is not None:
                point.discovered += 1

        for (created_at,) in (
            await self._session.execute(
                select(JobScore.created_at).where(
                    JobScore.user_id == identifier, JobScore.created_at >= start
                )
            )
        ).all():
            point = buckets.get(_local_day(created_at))
            if point is not None:
                point.scored += 1

        rows = (
            await self._session.execute(
                select(
                    Application.created_at,
                    Application.submitted_at,
                    Application.updated_at,
                    Application.status,
                ).where(
                    Application.user_id == identifier,
                    (Application.created_at >= start)
                    | (Application.submitted_at >= start)
                    | (Application.updated_at >= start),
                )
            )
        ).all()

        for created_at, submitted_at, updated_at, status in rows:
            created_point = buckets.get(_local_day(created_at))
            if created_point is not None:
                created_point.applications += 1

            submitted_point = buckets.get(_local_day(submitted_at))
            if submitted_point is not None:
                submitted_point.submitted += 1

            resolved = ApplicationStatus(status)
            attribute = {
                ApplicationStatus.INTERVIEW: "interviews",
                ApplicationStatus.OFFER: "offers",
                ApplicationStatus.REJECTED: "rejections",
            }.get(resolved)
            if attribute is None:
                continue
            outcome_point = buckets.get(_local_day(updated_at)) or created_point
            if outcome_point is not None:
                setattr(outcome_point, attribute, getattr(outcome_point, attribute) + 1)

        return [buckets[day] for day in sorted(buckets)]

    # ----------------------------------------------------------------------------------
    # Breakdowns
    # ----------------------------------------------------------------------------------

    async def by_company(
        self,
        user_id: uuid.UUID | str,
        limit: int = DEFAULT_BREAKDOWN_LIMIT,
    ) -> dict[str, int]:
        """Return applications per employer, busiest first.

        One grouped aggregate with an outer join, so applications whose company was purged
        still appear — under :data:`UNKNOWN_LABEL` — rather than vanishing from a total the
        user can cross-check against the dashboard.

        Args:
            user_id: The applicant.
            limit: How many employers to return. ``0`` or negative returns all of them.

        Returns:
            Employer name to count, in descending order (dictionaries preserve insertion
            order, so the client can render the list as given).

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        statement = (
            select(Company.name, func.count(Application.id).label("applications"))
            .select_from(Application)
            .outerjoin(Company, Application.company_id == Company.id)
            .where(Application.user_id == identifier)
            .group_by(Company.name)
            .order_by(func.count(Application.id).desc(), Company.name.asc())
        )
        if limit and int(limit) > 0:
            statement = statement.limit(int(limit))

        rows = (await self._session.execute(statement)).all()
        return {(name or UNKNOWN_LABEL): int(count or 0) for name, count in rows}

    async def by_provider(self, user_id: uuid.UUID | str) -> dict[str, int]:
        """Return applications per ATS provider, busiest first.

        Args:
            user_id: The applicant.

        Returns:
            Provider wire value (``"greenhouse"``, ``"lever"``, …) to count.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        rows = (
            await self._session.execute(
                select(JobPosting.provider, func.count(Application.id))
                .select_from(Application)
                .join(JobPosting, Application.posting_id == JobPosting.id)
                .where(Application.user_id == identifier)
                .group_by(JobPosting.provider)
                .order_by(func.count(Application.id).desc())
            )
        ).all()
        return {ATSProviderName(provider).value: int(count or 0) for provider, count in rows}

    async def outcomes(self, user_id: uuid.UUID | str) -> dict[str, int]:
        """Return what happened to everything the employer actually received.

        Args:
            user_id: The applicant.

        Returns:
            One entry per post-submit status (present or zero, so the shape is stable across
            users), plus ``submitted`` (everything sent), ``responded`` (any reply, in either
            direction) and ``awaiting`` (sent, still silent).

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        counts = await self._status_counts(identifier)

        submitted = sum(counts.get(status, 0) for status in _POST_SUBMIT_ORDERED)
        responded = sum(counts.get(status, 0) for status in _RESPONDED_STATES)
        outcomes: dict[str, int] = {
            status.value: counts.get(status, 0) for status in _POST_SUBMIT_ORDERED
        }
        outcomes["submitted"] = submitted
        outcomes["responded"] = responded
        outcomes["awaiting"] = max(
            0, submitted - responded - counts.get(ApplicationStatus.GHOSTED, 0)
        )
        return outcomes

    # ----------------------------------------------------------------------------------
    # Insights
    # ----------------------------------------------------------------------------------

    async def what_gets_interviews(self, user_id: uuid.UUID | str) -> list[InsightItem]:
        """Return observational findings about what preceded an interview.

        **Correlational, never causal, and never acted on.** These items describe what the
        data shows; nothing in ApplicantOS reads them back to adjust a template, a threshold
        or a provider list. A single user's job hunt produces a sample of tens, and an
        auto-tuner running on that sample would change the user's resume for reasons the user
        could not reconstruct.

        Every item therefore carries its ``sample_size``, an explicit ``confidence`` in
        ``context`` (:data:`CONFIDENCE_LOW` below
        :data:`~app.schemas.dashboard.MIN_INSIGHT_SAMPLE_SIZE` observations), and prose that
        says so when the sample is thin. An item is omitted only when the comparison it makes
        is vacuous — one provider, or no scores recorded — never merely because the numbers
        are small; a panel that hides everything until the data is good looks broken.

        Four questions, four aggregate queries: the baseline rate, rate by provider, rate by
        score band, and rate with versus without a cover letter.

        Args:
            user_id: The applicant.

        Returns:
            The findings, most-informative first. Empty when nothing has been submitted yet.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")

        baseline = await self._baseline_rate(identifier)
        submitted, interviewed = baseline
        if submitted == 0:
            return []

        insights: list[InsightItem] = [
            _insight(
                key="interview_rate",
                title=(f"{interviewed} of {submitted} submitted applications reached an interview"),
                detail="Your baseline. Every other insight below is measured against it.",
                metric=_ratio(interviewed, submitted),
                sample_size=submitted,
                sentiment="positive" if interviewed else "neutral",
                context={"interviews": interviewed, "submitted": submitted},
            )
        ]

        provider = await self._provider_insight(identifier)
        if provider is not None:
            insights.append(provider)

        score = await self._score_band_insight(identifier)
        if score is not None:
            insights.append(score)

        cover_letter = await self._cover_letter_insight(identifier)
        if cover_letter is not None:
            insights.append(cover_letter)

        logger.debug("analytics.insights", user_id=str(identifier), items=len(insights))
        return insights

    async def _baseline_rate(self, user_id: uuid.UUID) -> tuple[int, int]:
        """Return ``(submitted, interviewed)`` across everything the employer received.

        Args:
            user_id: The applicant.

        Returns:
            The denominator and numerator of the baseline interview rate.
        """
        row = (
            await self._session.execute(
                self._submitted_scope(user_id).with_only_columns(
                    func.count(Application.id),
                    _sum_when(Application.status.in_(INTERVIEW_OUTCOME_STATES)),
                )
            )
        ).one()
        return (int(row[0] or 0), int(row[1] or 0))

    async def _provider_insight(self, user_id: uuid.UUID) -> InsightItem | None:
        """Compare interview rates across the ATS providers the user applied through.

        Args:
            user_id: The applicant.

        Returns:
            The finding, or ``None`` when fewer than two providers have any submissions —
            a comparison with one participant is not a comparison.
        """
        rows = (
            await self._session.execute(
                self._submitted_scope(user_id)
                .join(JobPosting, Application.posting_id == JobPosting.id)
                .with_only_columns(
                    JobPosting.provider,
                    func.count(Application.id),
                    _sum_when(Application.status.in_(INTERVIEW_OUTCOME_STATES)),
                )
                .group_by(JobPosting.provider)
            )
        ).all()
        if len(rows) < 2:
            return None

        breakdown = {
            ATSProviderName(provider).value: {
                "submitted": int(total or 0),
                "interviews": int(interviews or 0),
                "rate": _ratio(int(interviews or 0), int(total or 0)),
            }
            for provider, total, interviews in rows
        }
        best_name, best = max(
            breakdown.items(), key=lambda item: (item[1]["rate"], item[1]["submitted"])
        )
        sample = int(best["submitted"])
        return _insight(
            key="interviews_by_provider",
            title=(
                f"{best_name.capitalize()} applications reached an interview most often "
                f"({_percent(best['rate'])})"
            ),
            detail=(
                "Which board a posting came from is not why it replied — the roles, not the "
                "boards, differ. This says where your replies came from, not where to apply."
            ),
            metric=float(best["rate"]),
            sample_size=sample,
            sentiment="positive" if best["interviews"] else "neutral",
            context={"best": best_name, "by_provider": breakdown},
        )

    async def _score_band_insight(self, user_id: uuid.UUID) -> InsightItem | None:
        """Compare interview rates across score bands.

        Conditional aggregation over :data:`SCORE_BANDS` rather than ``GROUP BY`` on a ``CASE``
        expression: three bands produce six columns in one row, which is both portable and
        cheaper than a grouped scan.

        Args:
            user_id: The applicant.

        Returns:
            The finding, or ``None`` when no submitted application has a score, or when only
            one band has any.
        """
        columns: list[Any] = []
        for _key, _label, low, high in SCORE_BANDS:
            in_band = (JobScore.normalized >= low) & (JobScore.normalized <= high)
            columns.append(_sum_when(in_band))
            columns.append(_sum_when(in_band & Application.status.in_(INTERVIEW_OUTCOME_STATES)))

        row = (
            await self._session.execute(
                self._submitted_scope(user_id)
                .join(
                    JobScore,
                    (JobScore.posting_id == Application.posting_id)
                    & (JobScore.user_id == Application.user_id),
                )
                .with_only_columns(*columns)
            )
        ).one()

        breakdown: dict[str, dict[str, Any]] = {}
        for index, (key, label, _low, _high) in enumerate(SCORE_BANDS):
            total = int(row[index * 2] or 0)
            interviews = int(row[index * 2 + 1] or 0)
            if total:
                breakdown[key] = {
                    "label": label,
                    "submitted": total,
                    "interviews": interviews,
                    "rate": _ratio(interviews, total),
                }
        if len(breakdown) < 2:
            return None

        best_key, best = max(
            breakdown.items(), key=lambda item: (item[1]["rate"], item[1]["submitted"])
        )
        return _insight(
            key="interviews_by_score",
            title=(
                f"Postings scored {best['label'].lower()} replied most often "
                f"({_percent(best['rate'])})"
            ),
            detail=(
                "The score is our own estimate of fit, so this measures how well that "
                "estimate tracked reality — not a threshold to raise."
            ),
            metric=float(best["rate"]),
            sample_size=int(best["submitted"]),
            sentiment="positive" if best["interviews"] else "neutral",
            context={"best": best_key, "by_score_band": breakdown},
        )

    async def _cover_letter_insight(self, user_id: uuid.UUID) -> InsightItem | None:
        """Compare interview rates with and without a cover letter.

        Args:
            user_id: The applicant.

        Returns:
            The finding, or ``None`` unless both groups have at least one submission — with
            only one side there is nothing to compare against.
        """
        has_letter = Application.cover_letter_id.is_not(None)
        row = (
            await self._session.execute(
                self._submitted_scope(user_id).with_only_columns(
                    _sum_when(has_letter),
                    _sum_when(has_letter & Application.status.in_(INTERVIEW_OUTCOME_STATES)),
                    _sum_when(~has_letter),
                    _sum_when(~has_letter & Application.status.in_(INTERVIEW_OUTCOME_STATES)),
                )
            )
        ).one()

        with_total, with_interviews, without_total, without_interviews = (
            int(value or 0) for value in row
        )
        if with_total == 0 or without_total == 0:
            return None

        with_rate = _ratio(with_interviews, with_total)
        without_rate = _ratio(without_interviews, without_total)
        better = with_rate >= without_rate
        sample = with_total if better else without_total
        return _insight(
            key="interviews_by_cover_letter",
            title=(
                f"Applications {'with' if better else 'without'} a cover letter replied more "
                f"often ({_percent(with_rate if better else without_rate)} versus "
                f"{_percent(without_rate if better else with_rate)})"
            ),
            detail=(
                "Cover letters are written when the form asks for one, so the two groups are "
                "different kinds of employer — this is a description of your applications, "
                "not advice about letters."
            ),
            metric=float(with_rate if better else without_rate),
            sample_size=sample,
            sentiment="positive" if (with_interviews or without_interviews) else "neutral",
            context={
                "with_cover_letter": {
                    "submitted": with_total,
                    "interviews": with_interviews,
                    "rate": with_rate,
                },
                "without_cover_letter": {
                    "submitted": without_total,
                    "interviews": without_interviews,
                    "rate": without_rate,
                },
            },
        )

    # ----------------------------------------------------------------------------------
    # Composite
    # ----------------------------------------------------------------------------------

    async def full_overview(
        self,
        user_id: uuid.UUID | str,
        days: int = DEFAULT_TIMESERIES_DAYS,
    ) -> AnalyticsOverview:
        """Return every analytics panel in one payload.

        Bundled so the analytics screen paints in one request; the individual methods back the
        narrower endpoints for clients refreshing a single panel.

        Args:
            user_id: The applicant.
            days: Width of the activity window, clamped as in :meth:`timeseries`.

        Returns:
            The whole payload, stamped with ``generated_at`` so the client can show staleness.

        Raises:
            LookupError: If *user_id* is malformed.
        """
        identifier = _as_uuid(user_id, "user id")
        window = max(1, min(int(days or DEFAULT_TIMESERIES_DAYS), MAX_TIMESERIES_DAYS))
        counts = await self._status_counts(identifier)

        submitted = sum(counts.get(status, 0) for status in _POST_SUBMIT_ORDERED)
        responded = sum(counts.get(status, 0) for status in _RESPONDED_STATES)
        interviewed = sum(counts.get(status, 0) for status in INTERVIEW_OUTCOME_STATES)

        mean_duration = await self._session.scalar(
            select(func.avg(Application.duration_seconds)).where(
                Application.user_id == identifier,
                Application.duration_seconds.is_not(None),
            )
        )
        mean_score = await self._session.scalar(
            select(func.avg(JobScore.normalized))
            .select_from(Application)
            .join(
                JobScore,
                (JobScore.posting_id == Application.posting_id)
                & (JobScore.user_id == Application.user_id),
            )
            .where(Application.user_id == identifier)
        )

        return AnalyticsOverview(
            stats=await self.overview(identifier),
            funnel=await self.funnel(identifier),
            timeseries=await self.timeseries(identifier, window),
            insights=await self.what_gets_interviews(identifier),
            by_provider=await self.by_provider(identifier),
            by_company=await self.by_company(identifier),
            by_status={status.value: count for status, count in counts.items()},
            response_rate=_ratio(responded, submitted),
            interview_rate=_ratio(interviewed, submitted),
            offer_rate=_ratio(counts.get(ApplicationStatus.OFFER, 0), submitted),
            avg_application_seconds=(float(mean_duration) if mean_duration is not None else None),
            avg_score=float(mean_score) if mean_score is not None else None,
            tokens_used=await self._tokens_used(identifier, window),
            window_days=window,
            generated_at=utcnow(),
        )

    async def _tokens_used(self, user_id: uuid.UUID, days: int) -> dict[str, int]:
        """Sum the LLM token counters recorded on the user's runs in the window.

        Merged in Python rather than in SQL because ``token_usage`` is a JSON document whose
        keys are model names, and there is no portable way to sum across JSON object keys. The
        row count is bounded by the number of runs in the window, which is small.

        Args:
            user_id: The applicant.
            days: Width of the window in days.

        Returns:
            Usage bucket to total tokens; empty when nothing ran in the window.
        """
        since = utcnow() - dt.timedelta(days=days)
        rows = (
            await self._session.execute(
                select(RunSession.token_usage).where(
                    RunSession.user_id == user_id, RunSession.started_at >= since
                )
            )
        ).all()

        totals: dict[str, int] = {}
        for (usage,) in rows:
            if not isinstance(usage, Mapping):
                continue
            for bucket, count in usage.items():
                try:
                    totals[str(bucket)] = totals.get(str(bucket), 0) + int(count)
                except (TypeError, ValueError):
                    logger.debug("analytics.token_usage_unparseable", bucket=str(bucket))
        return totals

    # ----------------------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------------------

    async def _status_counts(self, user_id: uuid.UUID) -> dict[ApplicationStatus, int]:
        """Return this user's applications grouped by status.

        The single query behind :meth:`overview`, :meth:`funnel`, :meth:`outcomes` and
        :meth:`full_overview`: every one of them is a different arrangement of the same six
        numbers, and computing them separately would guarantee they eventually disagree.

        Args:
            user_id: The applicant.

        Returns:
            Status to count, omitting statuses with no rows.
        """
        rows = (
            await self._session.execute(
                select(Application.status, func.count(Application.id))
                .where(Application.user_id == user_id)
                .group_by(Application.status)
            )
        ).all()
        return {ApplicationStatus(status): int(count or 0) for status, count in rows}

    def _submitted_scope(self, user_id: uuid.UUID) -> Select[Any]:
        """Return the base query over everything the employer actually received.

        Every insight measures against submitted applications, never against drafts: a resume
        that was never sent cannot have failed to get an interview.

        Args:
            user_id: The applicant.

        Returns:
            A ``SELECT`` over :class:`~app.models.application.Application`, ready for
            ``with_only_columns`` to replace its projection.
        """
        return select(Application.id).where(
            Application.user_id == user_id,
            Application.status.in_(_POST_SUBMIT_ORDERED),
        )


# ======================================================================================
# Helpers
# ======================================================================================


def _sum_when(condition: ColumnElement[bool]) -> ColumnElement[int]:
    """Return ``SUM(CASE WHEN <condition> THEN 1 ELSE 0 END)``.

    Conditional aggregation is how a rate and its denominator are read in one pass. ``COUNT``
    with a ``FILTER`` clause would be tidier but is not portable to SQLite.

    Args:
        condition: The predicate to count rows for.

    Returns:
        The aggregate expression.
    """
    return func.sum(case((condition, 1), else_=0))


def _insight(
    *,
    key: str,
    title: str,
    detail: str,
    metric: float,
    sample_size: int,
    sentiment: InsightSentiment,
    context: Mapping[str, Any],
) -> InsightItem:
    """Build one finding, with its confidence and its caveat attached.

    The confidence label goes in ``context`` because
    :class:`~app.schemas.dashboard.InsightItem` has no field for it, and the caveat goes in the
    prose because a rate rendered without its sample size gets read as a fact.

    Args:
        key: Stable identifier the client can pin or dismiss.
        title: One-line finding.
        detail: Supporting explanation, extended with the small-sample note when it applies.
        metric: The number the finding is about.
        sample_size: Observations behind it.
        sentiment: How it should read.
        context: Structured backing data, so the client can deep-link to the evidence.

    Returns:
        The populated item.
    """
    size = max(0, int(sample_size))
    body = detail
    if size < MIN_INSIGHT_SAMPLE_SIZE:
        body = f"{detail} {_PROVISIONAL_NOTE.format(n=size)}"
    return InsightItem(
        key=key,
        title=title,
        detail=body,
        sentiment=sentiment,
        metric=float(metric),
        sample_size=size,
        context={**dict(context), "confidence": _confidence(size)},
    )


def _confidence(sample_size: int) -> str:
    """Return the confidence label for a sample of this size.

    Args:
        sample_size: Observations behind a finding.

    Returns:
        :data:`CONFIDENCE_LOW` below
        :data:`~app.schemas.dashboard.MIN_INSIGHT_SAMPLE_SIZE`, :data:`CONFIDENCE_HIGH` at or
        above :data:`CONFIDENT_INSIGHT_SAMPLE_SIZE`, :data:`CONFIDENCE_MEDIUM` between them.
        Never a p-value: the data is observational and self-selected, so a formal significance
        claim would mislead more than a blunt label.
    """
    if sample_size < MIN_INSIGHT_SAMPLE_SIZE:
        return CONFIDENCE_LOW
    if sample_size < CONFIDENT_INSIGHT_SAMPLE_SIZE:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_HIGH


def _percent(rate: float) -> str:
    """Render a unit-interval rate as a whole-number percentage for insight prose.

    Args:
        rate: A fraction in ``[0, 1]``.

    Returns:
        A string such as ``"12%"``. Prose is the one place this module formats a number, and
        it does so because a sentence cannot contain a raw float.
    """
    return f"{round(rate * 100)}%"


def _ratio(numerator: int, denominator: int) -> float:
    """Return ``numerator / denominator``, clamped to ``[0, 1]``.

    Args:
        numerator: Top of the fraction.
        denominator: Bottom; ``0`` yields ``0.0`` rather than raising.

    Returns:
        The rate. Clamped because every rate field in
        :mod:`app.schemas.dashboard` declares ``le=1.0``, and an accounting quirk — an
        application prepared before its posting was scored — must not fail validation and take
        the dashboard down.
    """
    if denominator <= 0:
        return 0.0
    return round(min(1.0, max(0.0, numerator / denominator)), _RATE_PRECISION)


def _start_of_utc_day(now: dt.datetime | None = None) -> dt.datetime:
    """Return midnight UTC of the day *now* falls in.

    Args:
        now: The instant to bucket. Defaults to the current UTC time.

    Returns:
        An aware UTC datetime at ``00:00:00``.
    """
    moment = now if now is not None else utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_local_day(day: dt.date) -> dt.datetime:
    """Return the UTC instant at which *day* begins in the server's local timezone.

    Args:
        day: A local calendar date.

    Returns:
        An aware UTC datetime, ready to bind against a
        :class:`~app.database.types.UTCDateTime` column.
    """
    local = dt.datetime.combine(day, dt.time.min).astimezone()
    return local.astimezone(dt.UTC)


def _local_day(value: dt.datetime | None) -> dt.date | None:
    """Return the server-local calendar date a timestamp falls on.

    Args:
        value: The timestamp, or ``None``.

    Returns:
        The local date, or ``None``. A naive value is read as UTC, which is what
        :class:`~app.database.types.UTCDateTime` guarantees it is.
    """
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    return moment.astimezone().date()


def _as_uuid(value: uuid.UUID | str, label: str) -> uuid.UUID:
    """Coerce an identifier to a :class:`~uuid.UUID`.

    Args:
        value: The identifier, already a UUID or its string form.
        label: What the identifier names, used in the error message.

    Returns:
        The parsed UUID.

    Raises:
        LookupError: If the value is not a well-formed UUID. Malformed and missing are the
            same outcome for a caller — ``404`` — so they raise the same class.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise LookupError(f"{value!r} is not a valid {label}") from exc
