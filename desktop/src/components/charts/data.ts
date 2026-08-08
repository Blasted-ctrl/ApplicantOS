/**
 * The charts' data layer: shapes, derivations, and the table view every chart owes (§11.4).
 *
 * Split from the chart components for the reason `components/ui/variants.ts` is split from its
 * button — Fast Refresh only preserves state for a module whose exports are all components —
 * but it earns the split on its own merits too. **Every one of these functions is pure and
 * testable without a DOM**, and they are the half of a chart that can be wrong in a way nobody
 * sees: a mislabelled bucket, a conversion computed against the wrong denominator, a summary
 * sentence that says something the marks do not.
 *
 * The `*Table` builders are not optional. §11.4 makes a real `<table>` of the same data the
 * accessibility relief channel for every chart form, and it is doubly load-bearing in light
 * theme, where three of the eight validated series slots fall below 3:1 on white.
 */

import { ATS_PROVIDER_NAMES, type ATSProviderName, type FunnelStage, type TimeseriesPoint } from '@/lib/api/types';
import { formatDate, formatNumber, formatPercent, providerLabel } from '@/lib/utils';

import type { ChartSeries, ChartTable } from './chart-card';

// ══════════════════════════════════════════════════════════════════════════════════════
// Daily activity
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * The four disjoint daily events, bottom of the stack first.
 *
 * They wear the **status** palette rather than the categorical slots (§11.2 rule 3): the
 * series *are* application states, and giving "rejected" a categorical hue would make it a
 * different red from every other rejection in the product.
 */
export const ACTIVITY_SERIES: readonly ChartSeries[] = [
  { key: 'submitted', label: 'Submitted', color: 'var(--st-success)' },
  { key: 'interviews', label: 'Interview', color: 'var(--st-interview)' },
  { key: 'offers', label: 'Offer', color: 'var(--st-offer)' },
  { key: 'rejections', label: 'Rejected', color: 'var(--st-rejected)' },
];

/** The activity series as the mandatory table view. */
export function activityTable(points: readonly TimeseriesPoint[]): ChartTable {
  return {
    columns: ['Day', 'Submitted', 'Interview', 'Offer', 'Rejected'],
    rows: points.map((point) => [
      formatDate(point.date),
      point.submitted,
      point.interviews,
      point.offers,
      point.rejections,
    ]),
  };
}

/** One sentence stating what the activity chart shows, for its `aria-label`. */
export function activitySummary(points: readonly TimeseriesPoint[]): string {
  if (points.length === 0) return 'No days in the selected window.';
  const submitted = points.reduce((total, point) => total + point.submitted, 0);
  const outcomes = points.reduce(
    (total, point) => total + point.interviews + point.offers + point.rejections,
    0,
  );
  return `${String(submitted)} submitted and ${String(outcomes)} outcomes recorded across ${String(points.length)} days.`;
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Funnel
// ══════════════════════════════════════════════════════════════════════════════════════

/** The funnel as the mandatory table view. */
export function funnelTable(stages: readonly FunnelStage[]): ChartTable {
  return {
    columns: ['Stage', 'Count', 'From previous', 'Share of total'],
    rows: stages.map((stage) => [
      stage.label,
      formatNumber(stage.count),
      formatPercent(stage.conversion_rate),
      formatPercent(stage.share_of_total),
    ]),
  };
}

/** One sentence stating the funnel's shape. */
export function funnelSummary(stages: readonly FunnelStage[]): string {
  const first = stages[0];
  const last = stages[stages.length - 1];
  if (first === undefined || last === undefined) return 'No funnel data yet.';
  return `${formatNumber(first.count)} ${first.label.toLowerCase()} narrowing to ${formatNumber(last.count)} ${last.label.toLowerCase()}.`;
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Emphasis bands
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * Samples below this cannot carry a conclusion, however extreme the rate.
 *
 * A 100% interview rate over two applications is not a finding, and a chart that draws it at
 * full width without saying `n = 2` is the exact failure the product's insight rules exist to
 * prevent.
 */
export const MIN_CONFIDENT_SAMPLE = 8;

/** One band of an emphasis chart. */
export interface EmphasisDatum {
  key: string;
  label: string;
  /** 0–1. */
  rate: number;
  /** How many observations the rate is computed from. */
  sample: number;
}

/** The bands as the mandatory table view. */
export function emphasisTable(data: readonly EmphasisDatum[], measure: string): ChartTable {
  return {
    columns: ['Band', measure, 'Sample'],
    rows: data.map((datum) => [datum.label, formatPercent(datum.rate), datum.sample]),
  };
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Score histogram
// ══════════════════════════════════════════════════════════════════════════════════════

/** Bucket width on the 0–100 score scale. */
export const BUCKET_SIZE = 10;

/** One bucket. */
export interface HistogramBucket {
  /** Inclusive lower bound. */
  from: number;
  /** Exclusive upper bound, except the last bucket which includes 100. */
  to: number;
  count: number;
}

/** Bucket a list of normalised scores into fixed 10-point bins. Empty bins are kept. */
export function bucketScores(scores: readonly number[]): HistogramBucket[] {
  const buckets: HistogramBucket[] = [];
  for (let from = 0; from < 100; from += BUCKET_SIZE) {
    buckets.push({ from, to: from + BUCKET_SIZE, count: 0 });
  }
  for (const score of scores) {
    const index = Math.min(Math.floor(Math.max(score, 0) / BUCKET_SIZE), buckets.length - 1);
    const bucket = buckets[index];
    if (bucket !== undefined) bucket.count += 1;
  }
  return buckets;
}

/** A bucket's axis label. */
export function bucketLabel(bucket: HistogramBucket): string {
  return `${String(bucket.from)}–${String(bucket.to === 100 ? 100 : bucket.to - 1)}`;
}

/** The buckets as the mandatory table view. */
export function histogramTable(buckets: readonly HistogramBucket[]): ChartTable {
  return {
    columns: ['Score band', 'Postings'],
    rows: buckets.map((bucket) => [bucketLabel(bucket), bucket.count]),
  };
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Response dot plot
// ══════════════════════════════════════════════════════════════════════════════════════

/** One observation on the time-to-outcome plot. */
export interface ResponsePoint {
  id: string;
  label: string;
  /** Whole days from submission to outcome. */
  days: number;
  /** Colour of the dot — the outcome's status family. */
  color: string;
  /** The outcome word, for the tooltip. */
  outcome: string;
}

/**
 * The median of a list of numbers, or `null` when it is empty.
 *
 * The median rather than the mean, deliberately: over a handful of responses, one employer who
 * took ninety days drags a mean to a value no application actually landed on.
 */
export function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[middle] ?? null;
  const low = sorted[middle - 1];
  const high = sorted[middle];
  if (low === undefined || high === undefined) return null;
  return (low + high) / 2;
}

/** The observations as the mandatory table view. */
export function responseTable(points: readonly ResponsePoint[]): ChartTable {
  return {
    columns: ['Application', 'Outcome', 'Days'],
    rows: points.map((point) => [point.label, point.outcome, point.days]),
  };
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Providers
// ══════════════════════════════════════════════════════════════════════════════════════

/** One provider row. */
export interface ProviderDatum {
  provider: ATSProviderName;
  count: number;
}

/**
 * Turn `AnalyticsOverview.by_provider` into typed rows, busiest first.
 *
 * Unknown keys are dropped rather than rendered: a provider this build has no permanent colour
 * slot for would otherwise be drawn in a generated hue, which §11.2 forbids outright.
 */
export function toProviderData(byProvider: Readonly<Record<string, number>>): ProviderDatum[] {
  const known = new Set<string>(ATS_PROVIDER_NAMES);
  return Object.entries(byProvider)
    .filter(([provider]) => known.has(provider))
    .map(([provider, count]) => ({ provider: provider as ATSProviderName, count }))
    .sort((left, right) => right.count - left.count);
}

/** The providers as the mandatory table view. */
export function providerTable(data: readonly ProviderDatum[]): ChartTable {
  const total = data.reduce((sum, datum) => sum + datum.count, 0);
  return {
    columns: ['Provider', 'Applications', 'Share'],
    rows: data.map((datum) => [
      providerLabel(datum.provider),
      formatNumber(datum.count),
      formatPercent(total === 0 ? 0 : datum.count / total),
    ]),
  };
}
