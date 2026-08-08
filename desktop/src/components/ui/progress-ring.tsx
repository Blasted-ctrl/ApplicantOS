/**
 * ProgressRing and IndeterminateBar — `custom` (`docs/UI.md` §7.22).
 *
 * These two are a matched pair, and choosing between them is the four-way loading split from
 * §10.10 in miniature:
 *
 * **ProgressRing is determinate only.** If the percentage is not genuinely known, this is the
 * wrong component — a ring that sits at an invented 40% is worse than an honest indeterminate
 * bar, because it makes a promise about how long the wait is. `progress === null` is a real
 * state and the caller must render {@link IndeterminateBar} for it.
 *
 * **IndeterminateBar lives in the chrome, never over the content.** It is 2px, pinned to the
 * bottom edge of the toolbar, and it appears for any background refetch. **Content stays on
 * screen and stays interactive** while it runs — that is the whole point, and it is why
 * `docs/UI.md` §10.1 forbids branching on `isFetching`: the bar is the entire affordance.
 */

import { cn } from '@/lib/utils';

/** Ring diameters and their stroke widths (§7.22). */
const RING_SIZES = {
  sm: { diameter: 16, stroke: 2 },
  md: { diameter: 24, stroke: 3 },
  lg: { diameter: 40, stroke: 3 },
} as const;

/** Props for {@link ProgressRing}. */
export interface ProgressRingProps extends React.ComponentPropsWithoutRef<'div'> {
  /** 0–1. A genuinely known fraction — never an estimate. */
  value: number;
  size?: keyof typeof RING_SIZES;
  /** Show the percentage in the centre. Legible at 40px only, so it is ignored below that. */
  showLabel?: boolean;
  /** Announced description, e.g. `Indexing GitHub repositories`. */
  label?: string;
}

/**
 * A determinate progress ring.
 *
 * Starts at twelve o'clock (the `-90°` rotation) because a ring that starts at three o'clock
 * reads as an arbitrary arc rather than as a clock face.
 */
export function ProgressRing({
  value,
  size = 'md',
  showLabel = false,
  label,
  className,
  ...props
}: ProgressRingProps) {
  const { diameter, stroke } = RING_SIZES[size];
  const fraction = Math.min(Math.max(value, 0), 1);
  const radius = (diameter - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const percent = Math.round(fraction * 100);

  return (
    <div
      className={cn('relative inline-flex shrink-0 items-center justify-center', className)}
      role="progressbar"
      aria-valuenow={percent}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
      style={{ width: diameter, height: diameter }}
      {...props}
    >
      <svg width={diameter} height={diameter} className="-rotate-90" aria-hidden="true">
        <circle
          cx={diameter / 2}
          cy={diameter / 2}
          r={radius}
          fill="none"
          stroke="var(--state-track)"
          strokeWidth={stroke}
        />
        <circle
          cx={diameter / 2}
          cy={diameter / 2}
          r={radius}
          fill="none"
          stroke="var(--accent)"
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - fraction)}
          style={{ transition: 'stroke-dashoffset 300ms var(--ease-out)' }}
        />
      </svg>
      {showLabel && size === 'lg' && (
        <span className="absolute font-mono text-micro tabular-nums text-secondary">
          {percent}
        </span>
      )}
    </div>
  );
}

/** Props for {@link IndeterminateBar}. */
export interface IndeterminateBarProps extends React.ComponentPropsWithoutRef<'div'> {
  /** Whether anything is in flight. Renders an empty 2px track when false, so nothing shifts. */
  active: boolean;
  /** Announced description for the region this bar belongs to. */
  label?: string;
}

/**
 * The 2px background-fetch bar.
 *
 * Pinned to the bottom edge of the toolbar by its parent — never an overlay, and never a
 * content replacement. The track is always in the layout even when inactive, so the toolbar's
 * height does not change when a refetch starts.
 *
 * 433% is the translate that carries a 30%-wide child fully past the right edge, and the
 * keyframe lives in `styles/globals.css` inside `no-preference` so it does not exist for a
 * user who opted out of motion.
 */
export function IndeterminateBar({ active, label, className, ...props }: IndeterminateBarProps) {
  return (
    <div
      className={cn('relative h-0.5 w-full overflow-hidden', active && 'bg-state-track', className)}
      role={active ? 'progressbar' : undefined}
      aria-label={active ? (label ?? 'Loading') : undefined}
      aria-busy={active || undefined}
      {...props}
    >
      {active && (
        <span className="indeterminate-segment absolute inset-y-0 left-0 w-[30%] rounded-full bg-accent" />
      )}
    </div>
  );
}
