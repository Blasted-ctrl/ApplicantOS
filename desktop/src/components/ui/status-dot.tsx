/**
 * StatusDot — `custom` (`docs/UI.md` §7.7).
 *
 * ```
 *   ●        static, filled           terminal + settled  (confirmed, failed, rejected, …)
 *   ○        static, 1.5px ring       terminal + waiting  (draft, ready, submitted)
 *   ◌        static, dashed ring      dead                (abandoned, ghosted)
 *   ◉))      filled + pulsing ring    in flight           (preparing, submitting, needs_review)
 * ```
 *
 * Four binding rules, all enforced here rather than left to call sites:
 *
 * 1. **Animate only non-terminal states.** Terminal states are static. This is what makes a
 *    forty-row table readable in peripheral vision without reading a single word.
 * 2. **Never pair a StatusDot with a separate spinner.** The dot *is* the spinner.
 * 3. **`aria-hidden` when adjacent text names the state**, otherwise a `role="img"` with a
 *    label — a status colour never appears without a name attached (§2.7 rule 3).
 * 4. **Reserved for `ApplicationStatus`.** `PostingStatus`, `SessionStatus`, `IndexStatus`
 *    and `CheckpointStatus` reuse the same nine colour families but get a Badge, never the
 *    animated dot.
 *
 * The pulse is an `::after` ring defined in `styles/globals.css` inside
 * `@media (prefers-reduced-motion: no-preference)`, so for a user who opted out it does not
 * exist at all — the dot degrades to a static fill at 35% opacity, which still reads as
 * "different from the settled ones" without motion.
 */

import type { ApplicationStatus } from '@/lib/api/types';
import { cn, statusTone, type DotShape } from '@/lib/utils';

/** Dot diameters (§4.2). */
const SIZES = { sm: 6, md: 8, lg: 10 } as const;

/** Props for {@link StatusDot}. */
export interface StatusDotProps extends Omit<React.ComponentPropsWithoutRef<'span'>, 'color'> {
  status: ApplicationStatus;
  size?: keyof typeof SIZES;
  /**
   * Whether adjacent text already names this state.
   *
   * `true` — the default — hides the dot from assistive technology, because a badge that
   * reads "Submitting" does not need "Status: Submitting" read twice. Set `false` for a bare
   * dot in a dense cell, and it takes a `role="img"` with a label instead.
   */
  labelled?: boolean;
}

/** Shape classes for the three dot forms. */
function shapeClasses(shape: DotShape): string {
  switch (shape) {
    case 'filled':
      return 'border-0';
    case 'ring':
      return 'border-[1.5px] bg-transparent';
    case 'dashed':
      // `--st-dim` is legal as a mark at 3.07:1 but never as label text (§2.6); the badge
      // that accompanies this dot renders its word in `--fg-muted`. See `statusTone`.
      return 'border border-dashed bg-transparent';
    default: {
      const exhaustive: never = shape;
      return exhaustive;
    }
  }
}

/**
 * The status dot for one application.
 *
 * Sizes the pulse ring off the dot's own diameter so `sm` and `lg` pulse proportionally
 * rather than all producing an 8px ring.
 */
export function StatusDot({
  status,
  size = 'md',
  labelled = true,
  className,
  style,
  ...props
}: StatusDotProps) {
  const tone = statusTone(status);
  const diameter = SIZES[size];

  return (
    <span
      className={cn(
        'relative inline-block shrink-0 rounded-full',
        shapeClasses(tone.dot),
        tone.animated && 'status-dot-pulse',
        // The ring is drawn by an ::after pseudo-element that inherits these box metrics, so
        // it is absolutely positioned and can never affect flow (§10.13).
        tone.animated &&
          'after:absolute after:inset-0 after:rounded-full after:border-0 after:bg-[currentColor] after:content-[""]',
        className,
      )}
      style={{
        width: diameter,
        height: diameter,
        color: tone.color,
        backgroundColor: tone.dot === 'filled' ? tone.color : undefined,
        borderColor: tone.dot === 'filled' ? undefined : tone.color,
        ...style,
      }}
      {...(labelled
        ? { 'aria-hidden': true }
        : { role: 'img', 'aria-label': `Status: ${tone.label}` })}
      {...props}
    />
  );
}
