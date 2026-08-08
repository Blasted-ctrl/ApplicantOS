/**
 * Score distribution as a histogram (`docs/UI.md` §11.1 — "how are scores distributed?").
 *
 * **One hue, from the sequential ramp, with a threshold tick at `min_score`.** A histogram is
 * a magnitude scale; borrowing red and green from the status palette would make a 62 read as
 * a failure rather than as a number below the line the user drew themselves. The threshold is
 * the only annotation, and it is the whole reason the chart is worth a card: it answers "is my
 * bar set where the mass of my opportunities actually is?"
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { CHART_CHROME, sequentialColor } from '@/lib/chart/series';

import { BUCKET_SIZE, bucketLabel, type HistogramBucket } from './data';
import { makeTooltip } from './make-tooltip';

const renderTooltip = makeTooltip((label) => `Score ${String(label)}`);

/** Props for {@link ScoreHistogram}. */
export interface ScoreHistogramProps {
  buckets: readonly HistogramBucket[];
  /** `preferences.min_score`, drawn as the threshold tick. */
  threshold: number;
  height?: number;
}

/** The distribution. */
export function ScoreHistogram({ buckets, threshold, height = 200 }: ScoreHistogramProps) {
  const data = buckets.map((bucket) => ({
    band: bucketLabel(bucket),
    count: bucket.count,
    ramp: bucket.from / 90,
  }));

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke={CHART_CHROME.grid} strokeWidth={1} />
        <XAxis
          dataKey="band"
          tickLine={false}
          axisLine={{ stroke: CHART_CHROME.axis }}
          interval={0}
          minTickGap={0}
          tick={{ fill: CHART_CHROME.ink, fontSize: 11, fontFamily: 'var(--font-mono)' }}
        />
        <YAxis
          allowDecimals={false}
          width={34}
          tickLine={false}
          axisLine={false}
          tickCount={5}
          tick={{ fill: CHART_CHROME.ink, fontSize: 11, fontFamily: 'var(--font-mono)' }}
        />
        <Tooltip cursor={{ fill: 'var(--state-hover)' }} content={renderTooltip} />
        <ReferenceLine
          x={`${String(Math.floor(threshold / BUCKET_SIZE) * BUCKET_SIZE)}–${String(Math.floor(threshold / BUCKET_SIZE) * BUCKET_SIZE + BUCKET_SIZE - 1)}`}
          stroke="var(--score-threshold)"
          strokeWidth={1}
          label={{
            value: `min ${String(threshold)}`,
            position: 'top',
            fill: CHART_CHROME.ink,
            fontSize: 11,
            fontFamily: 'var(--font-mono)',
          }}
        />
        <Bar
          dataKey="count"
          name="Postings"
          maxBarSize={CHART_CHROME.maxBarThickness}
          radius={[4, 4, 0, 0]}
          stroke={CHART_CHROME.surface}
          strokeWidth={CHART_CHROME.surfaceGap}
          isAnimationActive={false}
        >
          {data.map((entry) => (
            <Cell key={entry.band} fill={sequentialColor(entry.ramp)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
