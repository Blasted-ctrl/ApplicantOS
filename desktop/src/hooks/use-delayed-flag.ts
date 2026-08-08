/**
 * The skeleton-gating hook (`docs/UI.md` §7.16 and §10.10).
 *
 * A skeleton renders only after **400ms** of `isPending`, and once rendered it stays for at
 * least **500ms**. Both halves matter and they solve opposite problems:
 *
 * - Below 400ms the data has already arrived, and a skeleton that appears and vanishes inside
 *   a quarter of a second reads as a glitch. The shell already occupies the space, so
 *   rendering nothing is not rendering a hole.
 * - Without the 500ms floor, a request that lands at 420ms produces a 20ms flash — strictly
 *   worse than either extreme.
 *
 * Implemented once, here, because a threshold that lives in each component is a threshold
 * that is 300ms in one of them.
 */

import { useEffect, useRef, useState } from 'react';

/** Timing for {@link useDelayedFlag}. Both defaults are binding values from §10.10. */
export interface DelayedFlagOptions {
  /** How long `active` must hold before the flag turns on. */
  delay?: number;
  /** How long the flag stays on once it has turned on. */
  minDuration?: number;
}

/**
 * Turn `active` into a flag that is slow to rise and slow to fall.
 *
 * @param active - The raw condition, normally a query's `isPending`. Never `isFetching`:
 *   `docs/UI.md` §10.1 makes `isPending` the only value permitted to gate content.
 * @returns Whether the delayed affordance should render.
 */
export function useDelayedFlag(active: boolean, options: DelayedFlagOptions = {}): boolean {
  const { delay = 400, minDuration = 500 } = options;
  const [visible, setVisible] = useState(false);
  const shownAt = useRef<number | null>(null);

  useEffect(() => {
    if (active) {
      if (visible) return;
      const timer = window.setTimeout(() => {
        shownAt.current = Date.now();
        setVisible(true);
      }, delay);
      return () => {
        window.clearTimeout(timer);
      };
    }

    if (!visible) return;

    const elapsed = shownAt.current === null ? minDuration : Date.now() - shownAt.current;
    const remaining = Math.max(0, minDuration - elapsed);
    if (remaining === 0) {
      shownAt.current = null;
      setVisible(false);
      return;
    }
    const timer = window.setTimeout(() => {
      shownAt.current = null;
      setVisible(false);
    }, remaining);
    return () => {
      window.clearTimeout(timer);
    };
  }, [active, visible, delay, minDuration]);

  return visible;
}

/**
 * The button-spinner variant: 150ms delay, no minimum duration.
 *
 * Below 150ms the mutation resolved and a spinner is noise; there is no floor because a
 * button's spinner replaces an icon in place rather than replacing content, so its
 * disappearance does not move anything.
 */
export function useButtonSpinner(isPending: boolean): boolean {
  return useDelayedFlag(isPending, { delay: 150, minDuration: 0 });
}
