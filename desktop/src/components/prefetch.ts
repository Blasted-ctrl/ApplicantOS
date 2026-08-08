/**
 * The adapter between a `queryOptions` object and the prefetch hook.
 *
 * `usePrefetch` (§10.6) asks for the minimum shape `queryClient.prefetchQuery` accepts: a key
 * and a fetcher. A `queryOptions()` object carries both plus a freshness policy, and its
 * `queryFn` is typed as optional because TanStack allows a default fetcher — which the two
 * shapes disagree about even though every options object in this app has one.
 *
 * Narrowing that disagreement here, once, is the point. The alternative is the same assertion
 * repeated at every table that warms a row, and an assertion repeated six times is an
 * assertion nobody checks the seventh time.
 */

import type { PrefetchableOptions } from '@/hooks/use-prefetch';

/** The part of a `queryOptions()` object this adapter reads. */
interface QueryOptionsLike {
  queryKey: readonly unknown[];
  queryFn?: unknown;
}

/**
 * Present a `queryOptions()` object as something `prefetchQuery` will take.
 *
 * `staleTime` is deliberately dropped: `usePrefetch` supplies its own 60-second floor as the
 * throttle, and carrying the family's policy through would make a `staleTime: Infinity` family
 * un-warmable — the prefetch would be a no-op forever and the row would open cold.
 *
 * @param options - A `queryOptions()` object from `lib/query/options.ts`. Passing the same
 *   object the screen consumes is what guarantees loader, prefetch and component share a key.
 */
export function prefetchable(options: QueryOptionsLike): PrefetchableOptions {
  return {
    queryKey: options.queryKey,
    queryFn: options.queryFn as PrefetchableOptions['queryFn'],
  };
}
