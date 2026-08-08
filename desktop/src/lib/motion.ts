/**
 * The named transitions and variants of `docs/UI.md` §6.3.
 *
 * These are the **only** durations and curves in the product. A component that writes
 * `{ duration: 0.2 }` inline has invented a number, and the reason that matters is §6.1:
 * motion here is rationed by frequency, so a duration is a statement about how often the
 * surface is used, not a taste call.
 *
 * Three details in this file are load-bearing:
 *
 * 1. **Full `transform` strings, not `x`/`y`/`scale` shorthands.** Motion's shorthands drive
 *    a `requestAnimationFrame` loop on the main thread and drop frames when the renderer is
 *    busy — which, in this app, is exactly when the WebSocket is fanning out and a
 *    virtualised table is re-rendering. The full string takes the WAAPI path and runs off
 *    the main thread.
 * 2. **Never `scale(0)`.** Entrances start at `0.97`; nothing in the real world appears from
 *    nothing.
 * 3. **Exits are ~2/3 of enters, and only exits use `--ease-exit`.** The asymmetry is what
 *    makes a dismissal feel responsive rather than reluctant.
 *
 * `ease-in` is banned on UI (§6.2): it delays the initial movement, the exact moment the user
 * is watching, so a 300ms `ease-in` dropdown *feels* slower than a 300ms `ease-out` one.
 * There is no `ease-in` token here and there must not be one.
 */

import type { TargetAndTransition, Transition } from 'framer-motion';

/** Named transitions. Each names the surface it belongs to; do not reuse across surfaces. */
export const T = {
  /** Press feedback. Must be faster than hover. */
  press: { duration: 0.1, ease: [0.23, 1, 0.32, 1] } as Transition,
  /** Hover / colour. */
  hover: { duration: 0.14, ease: [0.25, 0.46, 0.45, 0.94] } as Transition,
  /** Popover, dropdown, tooltip, context menu — entering. */
  pop: { duration: 0.18, ease: [0.23, 1, 0.32, 1] } as Transition,
  /** …and exiting. */
  popOut: { duration: 0.12, ease: [0.4, 0, 1, 1] } as Transition,
  /** Dialog. */
  dialog: { duration: 0.24, ease: [0.23, 1, 0.32, 1] } as Transition,
  dialogOut: { duration: 0.16, ease: [0.4, 0, 1, 1] } as Transition,
  /** Sheet / drawer — the iOS curve. */
  sheet: { duration: 0.32, ease: [0.32, 0.72, 0, 1] } as Transition,
  sheetOut: { duration: 0.22, ease: [0.32, 0.72, 0, 1] } as Transition,
  /** Toast — slightly slower and softer than UI, on purpose. */
  toast: { duration: 0.4, ease: [0.32, 0.72, 0, 1] } as Transition,
  toastOut: { duration: 0.2, ease: [0.4, 0, 1, 1] } as Transition,
  /** Height / width auto-resize (accordion, expanding answer, log detail). */
  resize: { duration: 0.3, ease: [0.32, 0.72, 0, 1] } as Transition,

  /** Small snappy UI: segmented-control pill, tab indicator, switch knob. */
  springSnap: { type: 'spring', stiffness: 400, damping: 30 } as Transition,
  /** Panels and drawers driven by state rather than duration. */
  springPanel: { type: 'spring', stiffness: 300, damping: 30, mass: 0.8 } as Transition,
  /** Large / heavy surfaces. */
  springCard: { type: 'spring', stiffness: 220, damping: 28, mass: 1 } as Transition,
  /** Gestures (sheet drag-to-dismiss). Apple form — easier to reason about. */
  gesture: { type: 'spring', duration: 0.5, bounce: 0.2 } as Transition,
} as const;

/** One variant set, in the shape Motion's `initial` / `animate` / `exit` props take. */
export interface MotionVariantSet {
  initial: TargetAndTransition;
  animate: TargetAndTransition;
  exit: TargetAndTransition;
}

/** Canonical variants. Enter from below, exit upward — directional, never flickery. */
export const V = {
  fade: {
    initial: { opacity: 0 },
    animate: { opacity: 1 },
    exit: { opacity: 0 },
  },
  popIn: {
    initial: { opacity: 0, transform: 'scale(0.97) translateY(-2px)' },
    animate: { opacity: 1, transform: 'scale(1) translateY(0px)' },
    exit: { opacity: 0, transform: 'scale(0.98) translateY(-2px)' },
  },
  listRow: {
    initial: { opacity: 0, transform: 'translateY(8px)' },
    animate: { opacity: 1, transform: 'translateY(0px)' },
    exit: { opacity: 0, transform: 'translateY(-8px)' },
  },
  dialog: {
    initial: { opacity: 0, transform: 'scale(0.97)' },
    animate: { opacity: 1, transform: 'scale(1)' },
    exit: { opacity: 0, transform: 'scale(0.985)' },
  },
  sheetR: {
    initial: { transform: 'translateX(100%)' },
    animate: { transform: 'translateX(0%)' },
    exit: { transform: 'translateX(100%)' },
  },
} as const satisfies Record<string, MotionVariantSet>;

/**
 * The list-stagger cap (§6.5): 30ms per item, at most 6 items, at most 180ms in total.
 *
 * Allowed **only** on the Dashboard KPI row on first mount, Onboarding step content, and the
 * Review-queue card list on first mount. Forbidden on any table body, any virtualised list,
 * any list that re-renders on a WebSocket event, search results, and the command palette.
 */
export const STAGGER = {
  /** Seconds between children. */
  step: 0.03,
  /** Items past this index all use the same delay, so the total never exceeds 180ms. */
  maxItems: 6,
} as const;

/**
 * The delay for the nth item of a permitted stagger.
 *
 * Rows are interactive at `opacity: 0`, so a delayed row is never an unclickable row.
 */
export function staggerDelay(index: number): number {
  return Math.min(index, STAGGER.maxItems - 1) * STAGGER.step;
}

/**
 * Strip the transform from a variant set, keeping opacity.
 *
 * The `prefers-reduced-motion` policy is "less and gentler motion, not zero feedback"
 * (§6.7): opacity survives because it carries the comprehension cue, and the transform goes
 * because it is the part that causes discomfort. Use this in the bespoke branches Motion's
 * own `MotionConfig reducedMotion="user"` does not cover — a sheet, for instance, should fade
 * in place rather than slide.
 */
export function withoutTransform(variants: MotionVariantSet): MotionVariantSet {
  const drop = (target: TargetAndTransition): TargetAndTransition => {
    const { transform: _transform, ...rest } = target as TargetAndTransition & {
      transform?: unknown;
    };
    return rest;
  };
  return {
    initial: { opacity: 0, ...drop(variants.initial) },
    animate: { opacity: 1, ...drop(variants.animate) },
    exit: { opacity: 0, ...drop(variants.exit) },
  };
}

/**
 * Pick the right variant set for the user's motion preference.
 *
 * @param variants - The full-motion variant set.
 * @param reduce - The value of `useReducedMotion()`. `null` means "not yet resolved", which
 *   is treated as no preference — the same default the media query has.
 */
export function motionSafe(
  variants: MotionVariantSet,
  reduce: boolean | null,
): MotionVariantSet {
  return reduce === true ? withoutTransform(variants) : variants;
}
