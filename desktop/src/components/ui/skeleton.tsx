/**
 * Skeleton — `shadcn+` (`docs/UI.md` §7.16).
 *
 * Two rules, both hard:
 *
 * **Every skeleton declares explicit width and height matching the real content**, so there
 * is zero layout shift on fill. That is why the canonical sizes below exist as named presets
 * rather than as a `className` the caller invents each time.
 *
 * **Never use a skeleton as an empty state.** A skeleton says "here is the shape of what is
 * coming"; if nothing is coming, that is a lie, and a permanently shimmering placeholder is
 * the worst signal a data dashboard can send. Use `EmptyState`.
 *
 * Gating is not this component's job — `useDelayedFlag(isPending, { delay: 400, minDuration:
 * 500 })` decides *whether* it renders, and with the persisted cache in §10.5 that decision
 * should come out `false` outside the first-ever launch.
 */

import { cn } from '@/lib/utils';

/** Canonical skeleton sizes (§7.16). Anything outside this table needs a reason. */
export const SKELETON_PRESETS = {
  /** Page title — `h-6 w-56`. */
  title: 'h-6 w-56',
  /** Page subtitle — one line, capped at a readable measure. */
  subtitle: 'h-4 w-full max-w-md',
  /** One 36px table row. */
  row: 'h-9 w-full',
  /** One stat tile. */
  tile: 'h-[92px] w-full rounded-lg',
  /** A card. */
  card: 'h-40 w-full rounded-lg',
  /** An avatar. */
  avatar: 'size-6 rounded-full',
  /** A single line of body text. */
  line: 'h-4 w-full',
} as const;

/** Props for {@link Skeleton}. */
export interface SkeletonProps extends React.ComponentPropsWithoutRef<'div'> {
  /** One of the canonical sizes. Omit only when the real content has a bespoke box. */
  preset?: keyof typeof SKELETON_PRESETS;
}

/**
 * A shimmering placeholder.
 *
 * `aria-hidden` because a screen reader has nothing to gain from "loading, loading, loading"
 * — the region's own `aria-busy` carries that, once.
 */
export function Skeleton({ preset, className, ...props }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={cn('skeleton', preset === undefined ? undefined : SKELETON_PRESETS[preset], className)}
      {...props}
    />
  );
}

/** Props for {@link SkeletonRows}. */
export interface SkeletonRowsProps extends React.ComponentPropsWithoutRef<'div'> {
  /** How many rows. Match the page size so the box does not change when data lands. */
  count?: number;
  /** Row height in pixels, from the current density (§4.2). */
  rowHeight?: number;
}

/**
 * A table body's worth of skeleton rows.
 *
 * The wrapper carries the same `min-height: rowHeight × count` the real table body does
 * (§7.10, anti-layout-shift), so a twelve-row page and a fifty-row page occupy the same box
 * whether or not the data has arrived.
 */
export function SkeletonRows({
  count = 12,
  rowHeight = 36,
  className,
  style,
  ...props
}: SkeletonRowsProps) {
  return (
    <div
      aria-hidden="true"
      className={cn('flex flex-col', className)}
      style={{ minHeight: rowHeight * count, ...style }}
      {...props}
    >
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="flex items-center px-3" style={{ height: rowHeight }}>
          <Skeleton className="h-3.5 w-full" />
        </div>
      ))}
    </div>
  );
}
