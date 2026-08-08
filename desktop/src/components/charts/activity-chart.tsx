/**
 * Daily activity as a stacked column (`docs/UI.md` §11.1 — "how did volume change day to
 * day?").
 *
 * **The series are application *states*, so they wear the status palette, not the categorical
 * slots** (§11.2 rule 3). A stacked column of submitted / interview / offer / rejected in
 * arbitrary hues would make "rejected" a random colour, and the whole point of §2.4 is that a
 * rejection is the same red everywhere in the product.
 *
 * The four series are genuinely disjoint *per day*, which is what makes stacking honest here:
 * the backend counts a submission on `submitted_at` and an outcome on the day the application
 * became that outcome, so one application can contribute at most one segment to any one
 * column. Stacking `discovered` on `scored` on `applied` — which are nested populations —
 * would have been a lie, and is why this chart is about outcomes rather than the funnel. The
 * funnel is a funnel (§11.6).
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import type { TimeseriesPoint } from '@/lib/api/types';
import { CHART_CHROME } from '@/lib/chart/series';
import { formatDate } from '@/lib/utils';

import { ACTIVITY_SERIES } from './data';
import { makeTooltip } from './make-tooltip';

/** Short axis label: `Aug 4`. */
function dayLabel(value: unknown): string {
  const date = typeof value === 'string' ? new Date(`${value}T00:00:00`) : null;
  if (date === null || Number.isNaN(date.getTime())) return String(value ?? '');
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(date);
}

const renderTooltip = makeTooltip((label) => formatDate(typeof label === 'string' ? label : null));

/** Props for {@link ActivityChart}. */
export interface ActivityChartProps {
  points: readonly TimeseriesPoint[];
  height?: number;
}

/** One column per day, stacked by outcome. */
export function ActivityChart({ points, height = 200 }: ActivityChartProps) {
  const data = points.map((point) => ({
    date: point.date,
    submitted: point.submitted,
    interviews: point.interviews,
    offers: point.offers,
    rejections: point.rejections,
  }));

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }} barGap={2}>
        <CartesianGrid
          vertical={false}
          stroke={CHART_CHROME.grid}
          strokeDasharray="0"
          strokeWidth={1}
        />
        <XAxis
          dataKey="date"
          tickFormatter={dayLabel}
          interval="preserveStartEnd"
          minTickGap={24}
          tickLine={false}
          axisLine={{ stroke: CHART_CHROME.axis }}
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
        {ACTIVITY_SERIES.map((series, index) => (
          <Bar
            key={series.key}
            dataKey={series.key}
            name={series.label}
            stackId="activity"
            fill={series.color}
            // The 2px separation between touching segments is painted in the surface colour
            // (§11.3) rather than drawn as a coloured stroke.
            stroke={CHART_CHROME.surface}
            strokeWidth={CHART_CHROME.surfaceGap}
            maxBarSize={CHART_CHROME.maxBarThickness}
            isAnimationActive={false}
            radius={index === ACTIVITY_SERIES.length - 1 ? [4, 4, 0, 0] : [0, 0, 0, 0]}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
