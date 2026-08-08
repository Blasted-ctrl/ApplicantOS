/**
 * ScoreBar — `custom` (`docs/UI.md` §7.19).
 *
 * ```
 *    ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░│░░░░░  82
 *    └── fill --score-3 ┘ └ threshold tick at min_score
 * ```
 *
 * **The bar never appears without its number.** The number is mono, right beside the bar,
 * with `min-width: 3ch` so a two-digit and a three-digit score occupy the same box. Colour is
 * the third channel, never the only one (§12.2).
 *
 * **The threshold tick is what turns a decorative bar into an instrument.** One 1px mark at
 * `min_score%` answers the only question the bar is really being asked — would the agent
 * apply to this? — and without it a 68 and a 72 look identical.
 *
 * Width is **not animated in a table** (§7.19). Fifty bars animating their width on every
 * scored posting is fifty layout passes per frame. The detail panel is allowed one 400ms
 * grow on first paint, because it is one element and it is the subject of the screen.
 */

import { useEffect, useState } from 'react';

import { clamp, cn, scoreBand, scoreColor, scoreVerdict } from '@/lib/utils';

/** Props for {@link ScoreBar}. */
export interface ScoreBarProps extends React.ComponentPropsWithoutRef<'div'> {
  /** `JobScore.normalized`, 0–100. */
  value: number;
  /** `UserPreferences.min_score`. Drawn as the threshold tick. Defaults to the product default. */
  threshold?: number;
  /** `sm` is the 4px table-cell form; `lg` is the 6px detail-panel form. */
  size?: 'sm' | 'lg';
  /** Fill the available width instead of the fixed 72px table-cell width. */
  fluid?: boolean;
  /** Hide the numeric value. Only legal when the number is rendered in an adjacent cell. */
  hideValue?: boolean;
  /** Grow the fill from zero on first paint. Detail panel only — never in a table. */
  animate?: boolean;
}

/** A score, as a bar plus its number. */
export function ScoreBar({
  value,
  threshold = 70,
  size = 'sm',
  fluid = false,
  hideValue = false,
  animate = false,
  className,
  ...props
}: ScoreBarProps) {
  const score = clamp(Math.round(value), 0, 100);
  const [width, setWidth] = useState(animate ? 0 : score);

  useEffect(() => {
    if (!animate) {
      setWidth(score);
      return;
    }
    // One frame at zero, then the real width, so the transition has something to run from.
    const handle = requestAnimationFrame(() => {
      setWidth(score);
    });
    return () => {
      cancelAnimationFrame(handle);
    };
  }, [animate, score]);

  return (
    <div
      className={cn('flex items-center gap-2', className)}
      role="meter"
      aria-valuenow={score}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`Score ${String(score)} of 100 — ${scoreVerdict(score)}`}
      {...props}
    >
      <div
        className={cn(
          'relative shrink-0 overflow-hidden rounded-full bg-state-track',
          size === 'lg' ? 'h-1.5' : 'h-1',
          fluid ? 'w-full' : 'w-[72px]',
        )}
      >
        <div
          className="h-full rounded-full"
          style={{
            width: `${String(width)}%`,
            backgroundColor: scoreColor(score),
            transition: animate ? 'width 400ms var(--ease-drawer)' : undefined,
          }}
        />
        {/* Above the fill, so the mark stays readable at any score. Absolutely positioned,
            so it can never affect flow. */}
        <span
          aria-hidden="true"
          className="absolute top-1/2 h-2 w-px -translate-y-1/2"
          style={{
            left: `${String(clamp(threshold, 0, 100))}%`,
            backgroundColor: 'var(--score-threshold)',
          }}
        />
      </div>
      {!hideValue && (
        <span className="min-w-[3ch] text-right font-mono text-mini tabular-nums text-primary">
          {score}
        </span>
      )}
    </div>
  );
}

/** One row of a score's arithmetic, for the detail panel's breakdown. */
export interface ScoreComponentRowProps {
  label: string;
  /** Signed contribution. Negative components render in danger with a `−` prefix. */
  points: number;
  /** Largest absolute contribution in the breakdown, so the mini bars share a scale. */
  scale: number;
  /** A hard negative zeroes the score outright, and says so. */
  hardNegative?: boolean;
  detail?: string | null;
}

/**
 * One `ScoreComponent`: label, mono value, mini bar.
 *
 * All components share one scale so their bars are comparable — a breakdown where each row
 * normalises to its own width says nothing about which rule actually decided the score.
 */
export function ScoreComponentRow({
  label,
  points,
  scale,
  hardNegative = false,
  detail,
}: ScoreComponentRowProps) {
  const negative = points < 0;
  const magnitude = scale === 0 ? 0 : clamp((Math.abs(points) / scale) * 100, 0, 100);
  const band = scoreBand(magnitude);

  return (
    <div className="flex items-center gap-3 py-1">
      <span className="min-w-0 flex-1 truncate text-sm text-secondary" title={detail ?? label}>
        {label}
        {hardNegative && <span className="ml-1.5 text-mini text-st-danger">hard negative</span>}
      </span>
      <span
        className={cn(
          'w-[6ch] shrink-0 text-right font-mono text-mini tabular-nums',
          negative ? 'text-st-danger' : 'text-primary',
        )}
      >
        {negative ? '−' : ''}
        {Math.abs(points).toFixed(1)}
      </span>
      <span className="h-1 w-[72px] shrink-0 overflow-hidden rounded-full bg-state-track">
        <span
          className="block h-full rounded-full"
          style={{
            width: `${String(magnitude)}%`,
            backgroundColor: negative ? 'var(--st-danger)' : `var(--score-${String(band)})`,
          }}
        />
      </span>
    </div>
  );
}
