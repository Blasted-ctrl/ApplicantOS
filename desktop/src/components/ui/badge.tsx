/**
 * Badge — `shadcn+` (`docs/UI.md` §7.6).
 *
 * `[ 6px ][ StatusDot 8px (optional) ][ 6px ][ label ][ 6px ]`, 20px tall, `--radius-full`,
 * 12px weight 510. The background is the status colour at 12% (14% for review, danger and
 * interview, which are louder by design) and the border is the same colour at 22%.
 *
 * **`offer` is the only solid-filled badge in the product** (§2.4 rule 4). Exactly one status
 * is allowed to be as loud as the primary action, and it is the one the user is doing all of
 * this for.
 *
 * Two rules the API enforces rather than documents: a badge **always carries a text label**,
 * and a badge is **never clickable** — if it needs to be clickable it is a `chip` Button.
 * There is no `onClick` prop here, which is the cheapest way to keep that true.
 */

import { cva, type VariantProps } from 'class-variance-authority';
import type { ReactNode } from 'react';

import type { ApplicationStatus } from '@/lib/api/types';
import { cn, statusTone, type StatusTone } from '@/lib/utils';

import { StatusDot } from './status-dot';

/** Height and type scale (§4.2). */
const badgeVariants = cva(
  cn(
    'inline-flex shrink-0 select-none items-center gap-1.5 rounded-full',
    'border font-medium whitespace-nowrap',
  ),
  {
    variants: {
      size: {
        sm: 'h-[18px] px-1.5 text-micro tracking-normal normal-case',
        md: 'h-5 px-1.5 text-mini',
        lg: 'h-[22px] px-2 text-mini',
      },
    },
    defaultVariants: { size: 'md' },
  },
);

/** Props for {@link Badge}. */
export interface BadgeProps
  extends Omit<React.ComponentPropsWithoutRef<'span'>, 'color'>,
    VariantProps<typeof badgeVariants> {
  /**
   * The visual treatment, from `statusTone` / `postingStatusTone` / `sessionStatusTone` /
   * `indexStatusTone` / `checkpointStatusTone` / `signalKindTone` in `lib/utils.ts`.
   *
   * Taking a resolved tone rather than a raw enum is what lets one component serve all six
   * vocabularies without a switch per vocabulary — and what stops a seventh from inventing a
   * tenth colour family.
   */
  tone: StatusTone;
  /** Overrides `tone.label`. Use only when the surrounding context already says the noun. */
  children?: ReactNode;
  /** A leading mark. Not a StatusDot — see {@link StatusBadge} for that. */
  icon?: ReactNode;
}

/** A labelled status badge. */
export function Badge({ tone, size, className, children, icon, style, ...props }: BadgeProps) {
  const solid = tone.solid;
  return (
    <span
      className={cn(badgeVariants({ size }), className)}
      style={{
        color: solid ? 'var(--fg-on-status)' : tone.text,
        backgroundColor: solid
          ? tone.color
          : `color-mix(in oklab, ${tone.color} ${String(tone.wash * 100)}%, transparent)`,
        borderColor: solid
          ? 'transparent'
          : `color-mix(in oklab, ${tone.color} 22%, transparent)`,
        ...style,
      }}
      {...props}
    >
      {icon}
      {children ?? tone.label}
    </span>
  );
}

/** Props for {@link StatusBadge}. */
export interface StatusBadgeProps extends Omit<BadgeProps, 'tone' | 'icon'> {
  status: ApplicationStatus;
  /** Show the StatusDot before the label. Off in dense table cells where the dot is separate. */
  showDot?: boolean;
}

/**
 * The application-lifecycle badge: dot plus label.
 *
 * The dot is `aria-hidden` because the label beside it names the state — the assistive
 * announcement is the word, and the dot is the peripheral-vision channel for everyone else.
 */
export function StatusBadge({ status, showDot = true, ...props }: StatusBadgeProps) {
  const tone = statusTone(status);
  return (
    <Badge
      tone={tone}
      icon={showDot ? <StatusDot status={status} size="sm" /> : undefined}
      {...props}
    />
  );
}
