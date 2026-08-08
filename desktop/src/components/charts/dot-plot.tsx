/**
 * Time-to-outcome as a dot plot with a median line (`docs/UI.md` §11.1 — "how long until a
 * response?").
 *
 * A dot plot rather than a histogram because the question is about *typical* and *spread* at
 * the same time, and because the population is small enough that every observation can be
 * drawn. The median is the only summary line: a mean over a handful of responses where one
 * employer took ninety days would sit somewhere no application actually landed.
 *
 * Each dot is one application, positioned by the days between its submission and the day it
 * reached a terminal outcome, and jittered vertically only so that two applications on the
 * same day do not hide each other. **The vertical axis carries no meaning and is therefore not
 * drawn** — an axis with no scale is an invitation to read one.
 */

import {
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts';

import { CHART_CHROME } from '@/lib/chart/series';

import type { TooltipProps } from 'recharts';

import { TooltipSurface } from './chart-tooltip';
import { median, type ResponsePoint } from './data';

/** Deterministic vertical jitter — the same application always lands in the same place. */
function jitter(id: string): number {
  let hash = 0;
  for (let index = 0; index < id.length; index += 1) {
    hash = (hash * 31 + id.charCodeAt(index)) % 997;
  }
  return 0.2 + (hash / 997) * 0.6;
}

function renderTooltip(props: TooltipProps<number, string>) {
  if (props.active !== true) return null;
  const entry = (props.payload ?? [])[0];
  const datum = entry?.payload as ResponsePoint | undefined;
  if (datum === undefined) return null;
  return (
    <TooltipSurface
      heading={datum.label}
      rows={[
        { key: 'days', label: 'Days to outcome', value: String(datum.days) },
        { key: 'outcome', label: 'Outcome', value: datum.outcome, color: datum.color },
      ]}
    />
  );
}

/** Props for {@link ResponseDotPlot}. */
export interface ResponseDotPlotProps {
  points: readonly ResponsePoint[];
  height?: number;
}

/** One dot per application, with the median marked. */
export function ResponseDotPlot({ points, height = 200 }: ResponseDotPlotProps) {
  const data = points.map((point) => ({ ...point, y: jitter(point.id) }));
  const middle = median(points.map((point) => point.days));

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ScatterChart margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} horizontal={false} stroke={CHART_CHROME.grid} />
        <XAxis
          type="number"
          dataKey="days"
          name="Days"
          allowDecimals={false}
          tickLine={false}
          axisLine={{ stroke: CHART_CHROME.axis }}
          tick={{ fill: CHART_CHROME.ink, fontSize: 11, fontFamily: 'var(--font-mono)' }}
        />
        <YAxis type="number" dataKey="y" domain={[0, 1]} hide />
        <ZAxis range={[64, 64]} />
        <Tooltip cursor={{ stroke: 'var(--border-strong)', strokeWidth: 1 }} content={renderTooltip} />
        {middle !== null && (
          <ReferenceLine
            x={middle}
            stroke="var(--border-strong)"
            strokeWidth={1}
            label={{
              value: `median ${String(Math.round(middle))}d`,
              position: 'insideTopRight',
              fill: CHART_CHROME.ink,
              fontSize: 11,
              fontFamily: 'var(--font-mono)',
            }}
          />
        )}
        <Scatter
          data={data}
          isAnimationActive={false}
          shape="circle"
          fill="var(--accent)"
          stroke={CHART_CHROME.surface}
          strokeWidth={2}
        />
      </ScatterChart>
    </ResponsiveContainer>
  );
}
