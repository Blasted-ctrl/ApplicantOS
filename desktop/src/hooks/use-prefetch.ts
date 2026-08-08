/**
 * Prefetch on intent (`docs/UI.md` §10.6).
 *
 * The rule this implements is that a row the pointer is resting on is a row that is about to
 * be opened, so the request for its detail should already be in flight by the time the click
 * lands. Three details are what make it work rather than backfire:
 *
 * **Both `onMouseEnter` and `onFocus`.** Keyboard users tabbing through a table get the same
 * warm cache as mouse users. Attaching only the pointer events would make the keyboard path —
 * the one this product optimises for everywhere else — the slow one.
 *
 * **A 60ms delay.** A mouse sweeping across fifty rows fires one request, not fifty. Below
 * about 50ms the sweep still fires; much above it, the deliberate hover misses its window.
 *
 * **`prefetchQuery`, not `fetchQuery`.** `prefetchQuery` returns `Promise<void>` and never
 * throws, so a prefetch that fails cannot surface as an unhandled rejection or a toast for
 * something the user did not ask for. Its `staleTime` is the throttle: the call is a no-op
 * when the cached data is fresher than that.
 */

import { useCallback, useEffect, useRef } from 'react';

import { queryClient } from '@/lib/query/client';

/** The minimum shape `queryClient.prefetchQuery` accepts. */
export interface PrefetchableOptions {
  queryKey: readonly unknown[];
  queryFn: (context: never) => unknown;
  staleTime?: number;
}

/** Handlers to spread onto the element whose hover should warm the cache. */
export interface PrefetchHandlers {
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onFocus: () => void;
  onBlur: () => void;
}

/** Hover dwell before the request goes out. */
const INTENT_DELAY_MS = 60;

/** Freshness floor for a prefetch: data newer than this is not re-fetched. */
const PREFETCH_STALE_MS = 60_000;

/**
 * Warm a query when the user shows intent.
 *
 * @param options - A `queryOptions` object from `lib/query/options.ts`. Passing the *same*
 *   object the component consumes is what guarantees the loader and the component share a
 *   key; a hand-built key here would warm an entry nothing ever reads.
 * @param enabled - Set false to disable warming, e.g. for a disabled row.
 */
export function usePrefetch(
  options: PrefetchableOptions | null,
  enabled = true,
): PrefetchHandlers {
  const timer = useRef<number | null>(null);

  const cancel = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const warm = useCallback(() => {
    if (!enabled || options === null || timer.current !== null) return;
    timer.current = window.setTimeout(() => {
      timer.current = null;
      void queryClient.prefetchQuery({
        ...(options as Parameters<typeof queryClient.prefetchQuery>[0]),
        staleTime: PREFETCH_STALE_MS,
      });
    }, INTENT_DELAY_MS);
  }, [enabled, options]);

  // A row unmounted mid-hover — a virtualiser recycling it, or a filter change — must not
  // leave a timer that fires against a component that is gone.
  useEffect(() => cancel, [cancel]);

  return { onMouseEnter: warm, onMouseLeave: cancel, onFocus: warm, onBlur: cancel };
}

/**
 * Prefetch the neighbouring pages of a list.
 *
 * §10.6: when the current page is full, the next one is prefetched; when the offset is past
 * zero, the previous one is. A next-page click then becomes a pure cache read, which is the
 * difference between paging feeling like scrolling and paging feeling like navigating.
 *
 * @param makeOptions - Builds the options for a given offset.
 * @param offset - The current offset.
 * @param limit - The page size.
 * @param itemCount - Rows in the current page. A short page means there is no next one.
 * @param ready - False while the current page is placeholder data; prefetching from a
 *   placeholder would page off a list the server has not confirmed.
 */
export function usePagePrefetch(
  makeOptions: (offset: number) => PrefetchableOptions,
  offset: number,
  limit: number,
  itemCount: number,
  ready: boolean,
): void {
  useEffect(() => {
    if (!ready) return;
    const targets: number[] = [];
    if (itemCount === limit) targets.push(offset + limit);
    if (offset > 0) targets.push(Math.max(0, offset - limit));
    for (const target of targets) {
      void queryClient.prefetchQuery({
        ...(makeOptions(target) as Parameters<typeof queryClient.prefetchQuery>[0]),
        staleTime: PREFETCH_STALE_MS,
      });
    }
  }, [makeOptions, offset, limit, itemCount, ready]);
}
