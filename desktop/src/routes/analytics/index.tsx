/**
 * Analytics (`docs/UI.md` §8.10, §11).
 *
 * Every form on this screen is justified in §11.1 by the question it answers, and the two
 * derived panels state their own sample. That is the rule that matters here: **an insight
 * carries its `sample_size` and its significance flag, and an n=4 pattern is never presented
 * as a finding.** The backend already computes `is_significant`; the client's job is to render
 * it as prominently as the claim it qualifies, not to bury it in a tooltip.
 *
 * `GET /analytics/insights` renders as a plain list of sentences with a mono figure — not as
 * an "AI insight card" with a sparkle icon. The finding is either worth reading or it is not,
 * and decoration cannot make the difference.
 */

import { useMemo, useState } from 'react';

import {
  ACTIVITY_SERIES,
  ActivityChart,
  activitySummary,
  activityTable,
  ChartCard,
  EmphasisBar,
  emphasisTable,
  FunnelBar,
  funnelSummary,
  funnelTable,
  MIN_CONFIDENT_SAMPLE,
  ProviderBar,
  ResponseDotPlot,
  ScoreHistogram,
  bucketScores,
  histogramTable,
  median,
  providerTable,
  responseTable,
  toProviderData,
  type EmphasisDatum,
  type ResponsePoint,
} from '@/components/charts';
import { Page, SectionHeading, ToolbarSpacer } from '@/components/page';
import { EmptyState, SegmentedControl, StatTile } from '@/components/ui';
import { useAnalyticsOverview, useApplications, usePostings, usePreferences } from '@/hooks';
import { useDelayedFlag } from '@/hooks/use-delayed-flag';
import { statusTone } from '@/lib/utils';
import { cn, formatPercent, orDash, scoreBand } from '@/lib/utils';

/** Windows the range control offers. Named in days, because that is what they are. */
const RANGES = [
  { value: '7', label: '7d' },
  { value: '30', label: '30d' },
  { value: '90', label: '90d' },
  { value: '365', label: '365d' },
] as const;

/** How many rows the derived panels are computed from. Stated on screen, never implied. */
const DERIVED_SAMPLE = 200;

/** Score bands, in the ordinal order `scoreBand` produces. */
const BAND_LABELS = ['0–39', '40–59', '60–69', '70–84', '85–100'] as const;

/** Statuses that mean an employer engaged. */
const ENGAGED = new Set(['interview', 'offer']);

/** Statuses that mean the application has a final answer. */
const RESOLVED = new Set(['rejected', 'interview', 'offer']);

/** Whole days between two timestamps, never negative. */
function daysBetween(from: string, to: string): number {
  const start = new Date(from).getTime();
  const end = new Date(to).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 0;
  return Math.max(0, Math.round((end - start) / 86_400_000));
}

/** The analytics screen. */
export function AnalyticsRoute() {
  const [range, setRange] = useState<(typeof RANGES)[number]['value']>('30');
  const days = Number.parseInt(range, 10);

  const overview = useAnalyticsOverview(days);
  const applications = useApplications({ limit: DERIVED_SAMPLE });
  const postings = usePostings({ limit: DERIVED_SAMPLE });
  const { data: preferences } = usePreferences();

  const showSkeletons = useDelayedFlag(overview.isPending);

  const stats = overview.data?.stats;
  const timeseries = overview.data?.timeseries ?? [];
  const funnel = overview.data?.funnel ?? [];
  const insights = overview.data?.insights ?? [];
  const providers = useMemo(
    () => toProviderData(overview.data?.by_provider ?? {}),
    [overview.data],
  );

  const appRows = useMemo(() => applications.data?.items ?? [], [applications.data]);

  /** Interview rate by score band, computed from the rows in cache. */
  const bands = useMemo<EmphasisDatum[]>(() => {
    const buckets = BAND_LABELS.map((label, index) => ({
      key: String(index),
      label,
      engaged: 0,
      sample: 0,
    }));
    for (const application of appRows) {
      if (application.score == null) continue;
      const bucket = buckets[scoreBand(application.score.normalized)];
      if (bucket === undefined) continue;
      bucket.sample += 1;
      if (ENGAGED.has(application.status)) bucket.engaged += 1;
    }
    return buckets.map((bucket) => ({
      key: bucket.key,
      label: bucket.label,
      rate: bucket.sample === 0 ? 0 : bucket.engaged / bucket.sample,
      sample: bucket.sample,
    }));
  }, [appRows]);

  /** Days from submission to a final answer, one point per resolved application. */
  const responses = useMemo<ResponsePoint[]>(
    () =>
      appRows
        .filter(
          (application) =>
            application.submitted_at != null && RESOLVED.has(application.status),
        )
        .map((application) => ({
          id: application.id,
          label: `${orDash(application.company?.name)} · ${orDash(application.posting?.title)}`,
          days: daysBetween(application.submitted_at ?? '', application.updated_at),
          color: statusTone(application.status).color,
          outcome: statusTone(application.status).label,
        })),
    [appRows],
  );

  const scoreBuckets = useMemo(
    () =>
      bucketScores(
        (postings.data?.items ?? [])
          .map((posting) => posting.score?.normalized)
          .filter((value): value is number => value !== undefined),
      ),
    [postings.data],
  );

  const scoredCount = scoreBuckets.reduce((total, bucket) => total + bucket.count, 0);
  const medianDays = median(responses.map((point) => point.days));

  return (
    <Page
      title="Analytics"
      subtitle={
        overview.data === undefined
          ? 'Loading from the local cache.'
          : `${String(stats?.total ?? 0)} applications · ${String(stats?.interviews ?? 0)} interviews · ${String(stats?.offers ?? 0)} offers · ${formatPercent(overview.data.interview_rate)} interview rate`
      }
      busy={overview.isFetching}
      toolbar={
        <>
          <SegmentedControl
            label="Analysis window"
            value={range}
            onValueChange={setRange}
            options={RANGES}
          />
          <ToolbarSpacer />
          <span className="font-mono text-mini tabular-nums text-muted">
            window {days} days
          </span>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            label="Applications"
            value={stats?.total ?? 0}
            loading={showSkeletons}
          />
          <StatTile
            label="Response rate"
            value={formatPercent(overview.data?.response_rate)}
            loading={showSkeletons}
          />
          <StatTile
            label="Interview rate"
            value={formatPercent(overview.data?.interview_rate)}
            loading={showSkeletons}
          />
          <StatTile
            label="Offer rate"
            value={formatPercent(overview.data?.offer_rate)}
            loading={showSkeletons}
          />
        </div>

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <ChartCard
            title="Applications over time"
            subtitle={`Day buckets across the last ${String(days)} days`}
            summary={activitySummary(timeseries)}
            series={ACTIVITY_SERIES}
            table={activityTable(timeseries)}
            height={280}
            isEmpty={timeseries.every(
              (point) =>
                point.submitted + point.interviews + point.offers + point.rejections === 0,
            )}
            emptyTitle="Nothing happened in this window"
            emptyDescription="Widen the range, or start a run."
          >
            <ActivityChart points={timeseries} height={280} />
          </ChartCard>

          <ChartCard
            title="Funnel"
            subtitle="Where postings drop out"
            summary={funnelSummary(funnel)}
            table={funnelTable(funnel)}
            height={280}
            isEmpty={funnel.length === 0 || funnel.every((stage) => stage.count === 0)}
            emptyTitle="Nothing has entered the funnel"
            emptyDescription="Discovery is the first stage."
          >
            <FunnelBar stages={funnel} />
          </ChartCard>

          <ChartCard
            title="Applications by provider"
            subtitle="Colour follows the provider, permanently"
            summary={
              providers.length === 0
                ? 'No applications recorded yet.'
                : `${String(providers.length)} providers, led by ${providers[0]?.provider ?? ''}.`
            }
            table={providerTable(providers)}
            isEmpty={providers.length === 0}
            emptyTitle="No applications by provider yet"
          >
            <ProviderBar data={providers} />
          </ChartCard>

          <ChartCard
            title="What gets interviews"
            subtitle={`Interview or offer rate by score band, from your ${String(appRows.length)} most recent applications`}
            summary={
              bands.every((band) => band.sample < MIN_CONFIDENT_SAMPLE)
                ? 'Not enough applications in any band to draw a conclusion yet.'
                : 'Interview rate by score band, with the sample size on every bar.'
            }
            table={emphasisTable(bands, 'Interview rate')}
            isEmpty={appRows.length === 0}
            emptyTitle="No applications to analyse"
            emptyDescription="This panel needs applications with scores attached."
            height={200}
          >
            <EmphasisBar
              data={bands}
              footnote={`Computed in this window from the ${String(appRows.length)} applications currently loaded — not from your whole history. Bands under ${String(MIN_CONFIDENT_SAMPLE)} observations are marked and are not treated as findings.`}
            />
          </ChartCard>

          <ChartCard
            title="Score distribution"
            subtitle={`${String(scoredCount)} scored postings, in 10-point bands`}
            summary={
              scoredCount === 0
                ? 'No scored postings yet.'
                : `${String(scoredCount)} scored postings, with your threshold marked at ${String(preferences?.min_score ?? 70)}.`
            }
            table={histogramTable(scoreBuckets)}
            isEmpty={scoredCount === 0}
            emptyTitle="No scored postings"
            emptyDescription="Scores are attached during a run."
          >
            <ScoreHistogram
              buckets={scoreBuckets}
              threshold={preferences?.min_score ?? 70}
            />
          </ChartCard>

          <ChartCard
            title="Time to a first answer"
            subtitle={`${String(responses.length)} applications that reached a final outcome`}
            summary={
              medianDays === null
                ? 'No application has reached a final outcome yet.'
                : `Median ${String(Math.round(medianDays))} days from submission to a final outcome, across ${String(responses.length)} applications.`
            }
            table={responseTable(responses)}
            isEmpty={responses.length === 0}
            emptyTitle="Nothing has come back yet"
            emptyDescription="This plot fills in as employers respond. Silence past your ghosting window is counted too."
          >
            <ResponseDotPlot points={responses} />
          </ChartCard>
        </div>

        <section>
          <SectionHeading>Insights</SectionHeading>
          {insights.length === 0 ? (
            <EmptyState
              title="No insights yet"
              description="Observations need a body of applications to be drawn from. They appear once there is enough history for a pattern to be more than a coincidence."
            />
          ) : (
            <ul className="flex flex-col">
              {insights.map((insight) => (
                <li
                  key={insight.key}
                  className="flex items-start gap-3 border-b border-state-divider py-2 last:border-b-0"
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      'mt-1.5 size-2 shrink-0 rounded-full',
                      insight.sentiment === 'positive' && 'bg-st-success',
                      insight.sentiment === 'negative' && 'bg-st-danger',
                      insight.sentiment === 'neutral' && 'bg-st-neutral',
                    )}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm text-primary">{insight.title}</span>
                    {insight.detail != null && insight.detail !== '' && (
                      <span className="block text-sm text-secondary">{insight.detail}</span>
                    )}
                  </span>
                  {insight.metric != null && (
                    <span className="shrink-0 font-mono text-mini tabular-nums text-primary">
                      {insight.metric.toFixed(insight.metric % 1 === 0 ? 0 : 2)}
                    </span>
                  )}
                  <span
                    className={cn(
                      'w-[92px] shrink-0 text-right font-mono text-micro tracking-normal',
                      insight.is_significant ? 'text-muted' : 'text-st-review',
                    )}
                    title={
                      insight.is_significant
                        ? `Drawn from ${String(insight.sample_size)} applications.`
                        : `Only ${String(insight.sample_size)} applications — too few to draw this conclusion from.`
                    }
                  >
                    n = {insight.sample_size}
                    {!insight.is_significant && <span className="block">low confidence</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Page>
  );
}
