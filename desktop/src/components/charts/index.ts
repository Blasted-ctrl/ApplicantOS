/**
 * The chart barrel (`docs/UI.md` §11).
 *
 * Every form in the product and nothing else. The bans are as load-bearing as the forms: no
 * pie, no donut, no dual axis, no gauge, no radar, no racing bars — there is no module behind
 * this barrel that could draw one, which is the cheapest possible enforcement.
 *
 * Components come from their own files; the shapes, the derivations and the `*Table` builders
 * for the mandatory table view all live in `./data`, which is pure and has no DOM in it.
 */

export {
  ChartCard,
  type ChartCardProps,
  type ChartSeries,
  type ChartTable,
} from './chart-card';
export { TooltipSurface, type TooltipRow } from './chart-tooltip';
export { makeTooltip } from './make-tooltip';

export { ActivityChart, type ActivityChartProps } from './activity-chart';
export { FunnelBar, type FunnelBarProps } from './funnel-bar';
export { EmphasisBar, type EmphasisBarProps } from './emphasis-bar';
export { ScoreHistogram, type ScoreHistogramProps } from './histogram';
export { ResponseDotPlot, type ResponseDotPlotProps } from './dot-plot';
export { ProviderBar, type ProviderBarProps } from './provider-bar';

export {
  ACTIVITY_SERIES,
  BUCKET_SIZE,
  MIN_CONFIDENT_SAMPLE,
  activitySummary,
  activityTable,
  bucketLabel,
  bucketScores,
  emphasisTable,
  funnelSummary,
  funnelTable,
  histogramTable,
  median,
  providerTable,
  responseTable,
  toProviderData,
  type EmphasisDatum,
  type HistogramBucket,
  type ProviderDatum,
  type ResponsePoint,
} from './data';
