/**
 * StatTile — `custom` (`docs/UI.md` §7.9, restated as a chart contract in §11.5).
 *
 * ```
 * ┌──────────────────────────────┐  92px tall, --radius-lg
 * │ Applications submitted       │  label   13 / 400 / --fg-muted, sentence case
 * │ 1,284          ▲ 12%         │  value   28 / 590 / --font-mono / proportional-nums
 * │                              │  delta   12 / 510 / direction-coloured + ▲▼ glyph
 * │  ▁▂▃▅▆▇▆▅▇█                  │  sparkline 12 pts, 28px, full-bleed
 * └──────────────────────────────┘
 * ```
 *
 * Three rules that are easy to break and expensive to un-break:
 *
 * **The value never animates.** No count-up, ever. Users read the number, not the animation,
 * and a live figure that ticks up on every WebSocket frame is unreadable. It swaps.
 *
 * **`proportional-nums`, not `tabular-nums`.** This is the documented exception to the global
 * tabular default (§3.5): tabular figures make a large standalone number look loose. Tabular
 * is for columns, and a stat tile is not one.
 *
 * **The delta's colour is direction × whether up is good.** Submissions up is
 * `--st-success`; failures up is `--st-danger`. Never green-for-up unconditionally — and it
 * is always paired with a ▲/▼ glyph, because colour is never the only channel.
 *
 * **Usage rule:** three or four per row, never five or more. A tile with no delta and no
 * sparkline is not a tile — put that number in the page-header subtitle instead.
 */

import type { ReactNode } from 'react';

import { cn, compactNumber, deltaTone } from '@/lib/utils';

import { Skeleton } from './skeleton';
import { Tooltip } from './tooltip';

/** Props for {@link Sparkline}. */
export interface SparklineProps {
  /** Twelve points is the documented shape. Fewer renders fine; more is noise at 28px. */
  points: readonly number[];
  className?: string;
}

/**
 * The tile's sparkline: 2px line, no axes, no gridlines, no labels, no tooltip.
 *
 * It answers "which way", not "how much" — which is why the history is `--fg-muted` at 55%
 * and only the final segment and the end dot wear `--accent-text`. Drawn as inline SVG rather
 * than through the chart library, because pulling Recharts into a stat tile costs more render
 * time than the tile is worth (§11).
 */
export function Sparkline({ points, className }: SparklineProps) {
  if (points.length < 2) return null;

  const width = 100;
  const height = 28;
  const inset = 2;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;

  const coords = points.map((point, index) => {
    const x = (index / (points.length - 1)) * width;
    const y = height - inset - ((point - min) / span) * (height - inset * 2);
    return [x, y] as const;
  });

  const path = coords
    .map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`)
    .join(' ');

  const last = coords[coords.length - 1];
  const penultimate = coords[coords.length - 2];
  const tail =
    last !== undefined && penultimate !== undefined
      ? `M${penultimate[0].toFixed(2)},${penultimate[1].toFixed(2)} L${last[0].toFixed(2)},${last[1].toFixed(2)}`
      : '';

  return (
    <svg
      viewBox={`0 0 ${String(width)} ${String(height)}`}
      preserveAspectRatio="none"
      className={cn('block h-7 w-full', className)}
      aria-hidden="true"
    >
      <path
        d={path}
        fill="none"
        stroke="var(--fg-muted)"
        strokeOpacity="0.55"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
      {tail !== '' && (
        <path
          d={tail}
          fill="none"
          stroke="var(--accent-text)"
          strokeWidth="2"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      )}
      {last !== undefined && (
        <circle cx={last[0]} cy={last[1]} r="2" fill="var(--accent-text)" vectorEffect="non-scaling-stroke" />
      )}
    </svg>
  );
}

/** Props for {@link StatTile}. */
export interface StatTileProps extends React.ComponentPropsWithoutRef<'div'> {
  /** Sentence case, no trailing colon. */
  label: string;
  /** The figure. Numbers compact above 10 000; strings render verbatim. */
  value: number | string;
  /** Signed ratio: `0.12` renders as `▲ 12%`. */
  delta?: number | null;
  /** Whether an increase is good *for this metric*. Failures rising is not. */
  upIsGood?: boolean;
  /** Names the comparison period in the delta's tooltip — never left for the reader to guess. */
  deltaPeriod?: string;
  /** Twelve points. Omit for a tile that has a delta instead. */
  sparkline?: readonly number[];
  /** Rendered instead of the value while the query has never had data. */
  loading?: boolean;
  /** An action or a badge, top-right. */
  action?: ReactNode;
}

/** One KPI tile. */
export function StatTile({
  label,
  value,
  delta,
  upIsGood = true,
  deltaPeriod = 'vs. the previous period',
  sparkline,
  loading = false,
  action,
  className,
  ...props
}: StatTileProps) {
  const tone = deltaTone(delta, upIsGood);
  const display = typeof value === 'number' ? compactNumber(value) : value;

  return (
    <div
      className={cn(
        'relative flex h-[92px] flex-col overflow-hidden rounded-lg border border-default bg-surface shadow-raised',
        className,
      )}
      {...props}
    >
      <div className="flex items-start justify-between gap-2 px-4 pt-3">
        <span className="truncate text-sm text-muted">{label}</span>
        {action}
      </div>

      <div className="flex items-baseline gap-2 px-4">
        {loading ? (
          <Skeleton className="mt-1 h-7 w-20" />
        ) : (
          <>
            <span className="nums-proportional font-mono text-2xl font-semibold text-primary">
              {display}
            </span>
            {tone.direction !== 'flat' && (
              <Tooltip content={deltaPeriod}>
                <span
                  className="inline-flex items-center gap-0.5 text-mini font-medium"
                  style={{ color: tone.color }}
                >
                  <span aria-hidden="true">{tone.glyph}</span>
                  {tone.label}
                </span>
              </Tooltip>
            )}
          </>
        )}
      </div>

      {/* Full-bleed to the tile's inner radius, pinned to the bottom edge. */}
      {sparkline !== undefined && sparkline.length > 1 && (
        <div className="mt-auto">
          <Sparkline points={sparkline} />
        </div>
      )}
    </div>
  );
}

/** The tile's skeleton, matching its box metrics exactly so nothing shifts on fill. */
export function StatTileSkeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        'flex h-[92px] flex-col gap-2 rounded-lg border border-default bg-surface p-4 shadow-raised',
        className,
      )}
    >
      <Skeleton className="h-3 w-24" />
      <Skeleton className="h-7 w-20" />
      <Skeleton className="mt-auto h-7 w-full" />
    </div>
  );
}
