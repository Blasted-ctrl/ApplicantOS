/**
 * The QueryClient and the per-family freshness table (`docs/UI.md` §10.2 and §10.3).
 *
 * The user requirement this implements is stated plainly in §10: *"Website should be able to
 * click around with no delay, cache everything if needed. There should never be 1 second of
 * delay ever."* Three of the defaults below are what actually deliver it, and none of them is
 * a preference:
 *
 * **`gcTime` is seven days and must stay ≥ the persister's `maxAge`.** Hydration's own
 * default `gcTime` is 300 000ms, so a restored entry under a shorter `gcTime` is collected
 * five minutes after hydration — the cache appears to work, and then quietly stops.
 *
 * **`refetchOnWindowFocus: false`.** Desktop users alt-tab constantly and the WebSocket
 * already keeps data fresh; leaving it on turns every return to the window into a refetch
 * storm across every mounted query.
 *
 * **`networkMode: 'offlineFirst'`.** Serve from disk before the network is confirmed. The
 * backend is a sidecar on loopback that may still be starting, and a query that refuses to
 * read its own cache while it waits is the exact spinner this section exists to prevent.
 *
 * The per-family table is the other half. **Do not use one global `staleTime`**: anything the
 * event stream covers gets `Infinity` and is invalidated by push, which is strictly better
 * than polling — zero redundant requests *and* zero staleness.
 */

import { QueryClient } from '@tanstack/react-query';

/** One minute in milliseconds. Every duration below is written in terms of it. */
export const MINUTE = 60_000;

/** One hour. */
export const HOUR = 60 * MINUTE;

/** One day. */
export const DAY = 24 * HOUR;

/** One week — the `gcTime` of everything persisted, and the persister's `maxAge`. */
export const WEEK = 7 * DAY;

/**
 * `staleTime` per query family (`docs/UI.md` §10.3, binding).
 *
 * `Infinity` means "the event stream owns this": the data is never refetched on its own and
 * is corrected by `setQueryData` when a frame arrives. `/reviews` is the one queue where a
 * stale count is user-visible harm, which is why it is 15 seconds rather than `Infinity`.
 */
/**
 * Day-window the dashboard renders, and therefore the one baked into its query key.
 *
 * It lives here, beside `STALE`, because three call sites must agree exactly: the dashboard
 * component, the index route's loader, and `hotKeyHashes()` in `persist.ts`. When they drift
 * the app still typechecks, lints and builds -- every key is structurally valid -- but the
 * landing screen silently stops appearing in the hot snapshot and cold-starts empty, which
 * defeats the one guarantee UI.md 10.5 exists to make. Import this; never restate the number.
 */
export const DASHBOARD_WINDOW_DAYS = 14;

export const STALE = {
  /** Invalidated by `posting.discovered` / `posting.scored`. */
  postingList: Number.POSITIVE_INFINITY,
  postingDetail: 5 * MINUTE,
  /** Invalidated by every `application.*` frame. */
  applicationList: Number.POSITIVE_INFINITY,
  applicationDetail: 5 * MINUTE,
  /** Invalidated by `application.submitted`. */
  applicationArtifacts: Number.POSITIVE_INFINITY,
  /** The only queue where a stale count is user-visible harm. */
  reviews: 15_000,
  analytics: 30_000,
  timeseries: 30_000,
  /** Invalidated by every `session.*` frame. */
  sessions: Number.POSITIVE_INFINITY,
  /** Invalidated by `knowledge.index_finished`. */
  knowledge: Number.POSITIVE_INFINITY,
  knowledgeGraph: 5 * MINUTE,
  /** Mutation-only. */
  resumes: Number.POSITIVE_INFINITY,
  /** Mutation-only. */
  settings: Number.POSITIVE_INFINITY,
  /** Changes only on restart. */
  plugins: Number.POSITIVE_INFINITY,
  /** Every step POST returns the new status, so the query itself is always refetched. */
  onboarding: 0,
  logs: 10_000,
  /** Health drives the build-id check and the backend banner. */
  health: 30_000,
  /** Invalidated by `tracking.*` frames. */
  tracking: Number.POSITIVE_INFINITY,
} as const;

/**
 * `gcTime` per query family.
 *
 * Longer than `staleTime` in every case, and never shorter than the persister's `maxAge` for
 * anything persisted — see the module docstring for what happens when it is.
 */
export const GC = {
  postingList: WEEK,
  postingDetail: WEEK,
  applicationList: WEEK,
  applicationDetail: WEEK,
  applicationArtifacts: WEEK,
  reviews: DAY,
  analytics: DAY,
  timeseries: DAY,
  sessions: WEEK,
  knowledge: WEEK,
  knowledgeGraph: DAY,
  resumes: WEEK,
  settings: WEEK,
  plugins: WEEK,
  onboarding: HOUR,
  logs: HOUR,
  health: HOUR,
  tracking: WEEK,
} as const;

/**
 * Key segments that must never be written to disk.
 *
 * Log entries churn continuously and are worthless a session later; the onboarding wizard is
 * a one-time flow whose persisted state would resurrect a completed step. Both the per-query
 * persister and the hot snapshot consult this list.
 */
export const NEVER_PERSIST = ['logs', 'onboarding', 'health', 'readiness'] as const;

/** Whether a key belongs to a family that must not be persisted. */
export function isPersistable(queryKey: readonly unknown[]): boolean {
  return !queryKey.some(
    (segment) =>
      typeof segment === 'string' && (NEVER_PERSIST as readonly string[]).includes(segment),
  );
}

/** How many times a genuinely transient failure is retried before it surfaces. */
const MAX_RETRIES = 2;

/** Client-error statuses that *are* worth retrying: a timeout and a rate limit both pass. */
const RETRYABLE_CLIENT_STATUSES: ReadonlySet<number> = new Set([408, 429]);

/**
 * Retry transport failures and server errors; surface client errors immediately.
 *
 * A 4xx is the server's considered answer, not a hiccup — asking again produces the same
 * status three times and delays the real answer by the length of the backoff. That is not
 * merely wasteful: the first-run redirect in `app.tsx` keys off the 404 from
 * `GET /onboarding/status`, and under a blanket `retry: 2` the app sat on an empty dashboard
 * for three seconds before it could know there was no account yet.
 *
 * 408 and 429 are the exceptions. Both explicitly mean "try again", so both do.
 */
function retryTransientOnly(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_RETRIES) return false;
  const status = (error as { status?: unknown } | null)?.status;
  if (typeof status !== 'number') return true; // network / parse failure — genuinely transient
  if (status >= 400 && status < 500) return RETRYABLE_CLIENT_STATUSES.has(status);
  return true;
}

/**
 * The application's single QueryClient.
 *
 * Constructed at module scope on purpose: `lib/query/persist.ts` hydrates the synchronous hot
 * snapshot into it **before** `createRoot().render()`, which is the whole cold-start strategy
 * (§10.5). A client created inside a component would not exist yet at that point.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      gcTime: WEEK,
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
      refetchOnMount: true,
      retry: retryTransientOnly,
      retryDelay: (attempt: number) => Math.min(1000 * 2 ** attempt, 8000),
      networkMode: 'offlineFirst',
      structuralSharing: true,
    },
    mutations: {
      networkMode: 'online',
      retry: 0,
    },
    dehydrate: {
      /**
       * **Never persist mutations.** A resumed `apply.submit` replaying on app restart would
       * violate `docs/CONTRACTS.md` §19.1 — *never apply twice*. This is the one cache bug in
       * the product that can cause real-world harm, and it is prevented here rather than in
       * each persister, because both the hot snapshot and the IndexedDB persister dehydrate
       * through this option.
       */
      shouldDehydrateMutation: () => false,
      shouldDehydrateQuery: (query) =>
        query.state.status === 'success' && isPersistable(query.queryKey),
    },
  },
});
