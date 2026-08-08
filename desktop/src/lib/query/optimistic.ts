/**
 * The optimistic-mutation primitives (`docs/UI.md` §10.9).
 *
 * Every optimistic mutation in the app follows one shape: cancel in-flight refetches, capture
 * the old value, write the new one, and keep the old one so `onError` can put it back.
 *
 * **Capturing the old value at mutation time is what makes optimistic UI safe rather than
 * reckless.** Without it there is nothing to roll back to and the "optimism" is just a race
 * whose loser is the user's data. These helpers exist so no call site can skip that step by
 * accident: {@link captureLists} returns the thing {@link restoreLists} needs, and the
 * mutation cannot compile without threading it through the context.
 *
 * **Not every mutation may be optimistic.** `POST /postings/{id}/apply`,
 * `POST /reviews/{id}/resolve`, `POST /sessions/start|stop` and `POST /onboarding/complete`
 * touch the outside world and are guarded server-side by `docs/CONTRACTS.md` §19. They flip
 * to an in-flight status locally and wait for the real event; showing "Submitted"
 * optimistically and rolling back would be a lie about something irreversible.
 */

import type { Page, Uuid } from '@/lib/api/types';

import { queryClient } from './client';

/** A captured list entry: its key and the data that was in it. */
export type ListSnapshot = readonly [readonly unknown[], unknown];

/**
 * Patch one row inside every cached page under a key prefix.
 *
 * Untouched rows keep their **exact object reference**. `structuralSharing` can only preserve
 * identity for values it is handed unchanged, so the `!==` early return is the mechanism, not
 * a micro-optimisation on top of one: a blanket `{...item}` spread re-renders every memoised
 * row in a fifty-row table on every event, which is the difference between 60fps and 12fps
 * while a run is going.
 *
 * A page whose rows did not change is returned by reference too, so an event about an
 * application on page 4 does not invalidate the render of page 1.
 */
export function patchListRow<T extends { id: Uuid }>(
  keyPrefix: readonly unknown[],
  id: Uuid,
  patch: Partial<T>,
): void {
  queryClient.setQueriesData<Page<T>>({ queryKey: keyPrefix }, (old) => {
    if (old === undefined) return old;
    let changed = false;
    const items = old.items.map((item) => {
      if (item.id !== id) return item;
      changed = true;
      return { ...item, ...patch };
    });
    return changed ? { ...old, items } : old;
  });
}

/**
 * Remove one row from every cached page under a key prefix, adjusting the totals.
 *
 * Used by the archive and dismiss flows, where the row must leave the list on the same frame
 * as the click. `total` is decremented so the header's count and the pagination arithmetic
 * agree with what is on screen.
 */
export function removeListRow<T extends { id: Uuid }>(
  keyPrefix: readonly unknown[],
  id: Uuid,
): void {
  queryClient.setQueriesData<Page<T>>({ queryKey: keyPrefix }, (old) => {
    if (old === undefined) return old;
    const items = old.items.filter((item) => item.id !== id);
    if (items.length === old.items.length) return old;
    return { ...old, items, total: Math.max(0, old.total - 1) };
  });
}

/**
 * Capture every cached page under a prefix, for rollback.
 *
 * Captures *all* matching keys rather than the one the component is looking at, because a
 * single optimistic edit is written into every cached filter view — and a rollback that
 * restored only one of them would leave the others holding the failed value.
 */
export function captureLists(keyPrefix: readonly unknown[]): ListSnapshot[] {
  return queryClient
    .getQueriesData({ queryKey: keyPrefix })
    .map(([key, data]) => [key, data] as ListSnapshot);
}

/** Put back what {@link captureLists} captured. */
export function restoreLists(snapshots: readonly ListSnapshot[] | undefined): void {
  if (snapshots === undefined) return;
  for (const [key, data] of snapshots) queryClient.setQueryData(key, data);
}

/**
 * Cancel in-flight fetches for a key so a response already on the wire cannot land *after*
 * the optimistic write and silently undo it.
 *
 * This is the step that is most often skipped and the one whose absence produces the
 * intermittent "my edit reverted for a second" bug, because it only reproduces when a refetch
 * happens to be in flight.
 */
export async function cancelFor(keys: readonly (readonly unknown[])[]): Promise<void> {
  await Promise.all(keys.map((queryKey) => queryClient.cancelQueries({ queryKey })));
}
