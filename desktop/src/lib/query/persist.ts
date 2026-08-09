/**
 * Two-layer cache persistence and hydration (`docs/UI.md` §10.5).
 *
 * The goal is stated as a budget: cold start to *first meaningful paint with real data* in
 * ≤800ms p50. Skeletons do not count. Reaching it takes two different mechanisms, because
 * they solve two different problems.
 *
 * **Layer 1 — a synchronous localStorage hot snapshot.** `localStorage.getItem` is
 * synchronous and sub-millisecond at 500 KB, so calling {@link hydrateHotSnapshot} at module
 * scope *before* `createRoot().render()` means the very first React render already has data:
 * zero frames of empty state. IndexedDB is asynchronous, so even a perfect IDB persister
 * costs at least one frame of nothing — which is the frame the user sees as a flash.
 *
 * **Layer 2 — a per-query IndexedDB persister for the long tail.** Every other query
 * restores lazily, the first time something observes it.
 *
 * **Do not use `persistQueryClient` + `createAsyncStoragePersister`.** That serialises the
 * entire dehydrated client under one key and rewrites the whole blob on every cache
 * mutation. With a WebSocket calling `setQueryData` continuously it pins the CPU and hammers
 * the disk — the documented pathology in TanStack/query#58799 (~25% idle CPU and ~5 MB/s of
 * sustained writes from a 45 MB blob). The per-query persister writes one key per query hash,
 * so a frame that touches one application rewrites one key.
 *
 * **Never gate the tree on `useIsRestoring`.** Render the full shell immediately and let
 * restoration fill in; gating just moves the blank-then-pop flash earlier in the boot.
 */

import { experimental_createQueryPersister } from '@tanstack/query-persist-client-core';
import { dehydrate, hydrate, type DehydratedState } from '@tanstack/react-query';
import { clear, createStore, del, entries, get, set, type UseStore } from 'idb-keyval';

import { API_SCHEMA_VERSION } from '@/lib/api/types';

import { DASHBOARD_WINDOW_DAYS, isPersistable, queryClient, WEEK } from './client';
import { qk } from './keys';

/**
 * Cache identity.
 *
 * A restored entry written by a build whose DTOs differed is not stale, it is wrong, and
 * rendering it produces `undefined` where the types promise a value. Bumping either half
 * makes every persisted byte unreadable, which is the intended outcome.
 */
export const BUSTER = `${__APP_VERSION__}:${API_SCHEMA_VERSION}`;

/** localStorage key holding the synchronous hot snapshot. */
const HOT_KEY = 'aos-hot';

/**
 * Ceiling on the hot snapshot.
 *
 * Chosen because `localStorage.getItem` stays sub-millisecond at this size — the whole point
 * of layer 1 is that reading it costs less than one frame, and a 4 MB snapshot would not.
 */
const HOT_MAX_BYTES = 500 * 1024;

/** IndexedDB database and store names for the long tail. */
const IDB_DATABASE = 'applicantos';
const IDB_STORE = 'qcache';

/** Key prefix for persisted queries, so the store is inspectable in devtools. */
const IDB_PREFIX = 'aos-qc';

/** localStorage key holding the backend build the cache was written against. */
const BUILD_KEY = 'aos-backend-build';

/** The store handle, created lazily so importing this module does not open a database. */
let store: UseStore | null = null;

function idbStore(): UseStore {
  store ??= createStore(IDB_DATABASE, IDB_STORE);
  return store;
}

/**
 * `idb-keyval` wrapped in the four methods the persister asks for.
 *
 * **`entries` is not optional in practice.** It is the only way `persisterGc` can walk the
 * store, so without it every expired or busted entry stays in IndexedDB forever — and a
 * development build does not degrade quietly, it *throws* on the first `persisterGc()` call,
 * which is once per launch. Keys are narrowed to strings because the persister prefix-matches
 * them; `idb-keyval` types them as `IDBValidKey`, and only string keys are ever written here.
 */
const idbStorage = {
  getItem: (key: string): Promise<unknown> => get(key, idbStore()),
  setItem: (key: string, value: unknown): Promise<void> => set(key, value, idbStore()),
  removeItem: (key: string): Promise<void> => del(key, idbStore()),
  entries: async (): Promise<[string, unknown][]> => {
    const stored = await entries<IDBValidKey, unknown>(idbStore());
    return stored
      .filter((pair): pair is [string, unknown] => typeof pair[0] === 'string')
      .map(([key, value]) => [key, value]);
  },
};

/**
 * The per-query persister.
 *
 * `maxAge` is one week and **must stay ≤ `gcTime`** (`docs/UI.md` §10.2): restored entries
 * adopt hydration's default `gcTime` otherwise and are collected five minutes later.
 * `serialize`/`deserialize` are identity because IndexedDB stores structured clones — running
 * `JSON.stringify` over every entry on the way in would be pure cost.
 */
export const persister = experimental_createQueryPersister<unknown>({
  storage: idbStorage,
  maxAge: WEEK,
  buster: BUSTER,
  prefix: IDB_PREFIX,
  serialize: (persistedQuery) => persistedQuery,
  deserialize: (cached) => cached as never,
  filters: { predicate: (query) => isPersistable(query.queryKey) },
});

// ══════════════════════════════════════════════════════════════════════════════════════
// Layer 1 — the synchronous hot snapshot
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * Query keys that belong in the hot snapshot, in priority order.
 *
 * This is the set the first paint needs and nothing else: the dashboard's figures, the first
 * unfiltered page of applications, the review queue, the sidebar's counts, and the two
 * settings objects the titlebar's safety chip reads. Everything past this list restores from
 * IndexedDB a few milliseconds later, by which time it is on screen anyway.
 */
function hotKeyHashes(): string[] {
  return [
    JSON.stringify(qk.analytics('overview', DASHBOARD_WINDOW_DAYS)),
    JSON.stringify(qk.applicationList({})),
    JSON.stringify(qk.reviewList({})),
    JSON.stringify(qk.preferences()),
    JSON.stringify(qk.settings()),
    JSON.stringify(qk.sessionList({})),
    JSON.stringify(qk.postingList({})),
  ];
}

/** The stored envelope, so a snapshot from an older build is recognised and dropped. */
interface HotSnapshot {
  buster: string;
  at: number;
  state: DehydratedState;
}

/**
 * Restore the hot snapshot into the query client, synchronously.
 *
 * **Call this at module scope in `main.tsx`, before `createRoot().render()`.** Called later,
 * it still works, but the frame it exists to save has already been painted empty.
 *
 * Never throws: a corrupt or foreign snapshot is dropped and the app cold-starts normally,
 * which is slower but correct. Failing the boot over a cache entry would be the wrong trade.
 *
 * @returns Whether a usable snapshot was applied.
 */
export function hydrateHotSnapshot(): boolean {
  try {
    const raw = window.localStorage.getItem(HOT_KEY);
    if (raw === null) return false;

    const snapshot = JSON.parse(raw) as Partial<HotSnapshot>;
    if (snapshot.buster !== BUSTER || snapshot.state === undefined) {
      window.localStorage.removeItem(HOT_KEY);
      return false;
    }

    hydrate(queryClient, snapshot.state);
    return true;
  } catch {
    // A quota error, a private-mode localStorage, or malformed JSON. All of them mean
    // "no snapshot", and none of them is worth a broken launch.
    try {
      window.localStorage.removeItem(HOT_KEY);
    } catch {
      /* localStorage is unavailable entirely; there is nothing to clean up. */
    }
    return false;
  }
}

/**
 * Write the hot snapshot.
 *
 * Called on `visibilitychange → hidden` and on `pagehide`, which between them cover every way
 * this window stops being looked at, including the one that matters — the user quitting.
 *
 * Oversized snapshots are trimmed from the *end* of the priority list rather than rejected,
 * because a snapshot holding only the dashboard is still worth a frame.
 */
export function writeHotSnapshot(): void {
  try {
    const wanted = new Set(hotKeyHashes());
    const state = dehydrate(queryClient, {
      shouldDehydrateQuery: (query) =>
        query.state.status === 'success' &&
        isPersistable(query.queryKey) &&
        wanted.has(JSON.stringify(query.queryKey)),
      shouldDehydrateMutation: () => false,
    });

    // Preserve priority order so trimming drops the least important entries first.
    const order = hotKeyHashes();
    const ranked = [...state.queries].sort(
      (a, b) =>
        indexOrLast(order, JSON.stringify(a.queryKey)) -
        indexOrLast(order, JSON.stringify(b.queryKey)),
    );

    let queries = ranked;
    let payload = serializeSnapshot({ buster: BUSTER, at: Date.now(), state: { ...state, queries } });
    while (payload.length > HOT_MAX_BYTES && queries.length > 1) {
      queries = queries.slice(0, -1);
      payload = serializeSnapshot({ buster: BUSTER, at: Date.now(), state: { ...state, queries } });
    }

    if (payload.length > HOT_MAX_BYTES) {
      window.localStorage.removeItem(HOT_KEY);
      return;
    }
    window.localStorage.setItem(HOT_KEY, payload);
  } catch {
    /* Quota or serialisation failure. The IndexedDB layer still has everything. */
  }
}

function serializeSnapshot(snapshot: HotSnapshot): string {
  return JSON.stringify(snapshot);
}

function indexOrLast(order: readonly string[], hash: string): number {
  const index = order.indexOf(hash);
  return index === -1 ? order.length : index;
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Wiring
// ══════════════════════════════════════════════════════════════════════════════════════

/** Whether {@link installPersistence} has already run, so a Fast Refresh cannot double-bind. */
let installed = false;

/**
 * Attach the per-query persister and start writing hot snapshots.
 *
 * Idempotent, and safe to call after `hydrateHotSnapshot()` — the two layers never contend,
 * because the persister only restores a query the hot snapshot did not already populate.
 *
 * @returns A teardown function, for tests and for Fast Refresh.
 */
export function installPersistence(): () => void {
  if (installed) return () => {};
  installed = true;

  const defaults = queryClient.getDefaultOptions();
  queryClient.setDefaultOptions({
    ...defaults,
    queries: { ...defaults.queries, persister: persister.persisterFn },
  });

  const onHidden = (): void => {
    if (document.visibilityState === 'hidden') writeHotSnapshot();
  };
  const onPageHide = (): void => {
    writeHotSnapshot();
  };

  document.addEventListener('visibilitychange', onHidden);
  window.addEventListener('pagehide', onPageHide);

  // Drop expired entries once, off the critical path. `persisterGc` walks the store, so it
  // waits until the first frame has been painted rather than competing with it. A rejection
  // is swallowed rather than left to become an unhandled rejection: an uncollected cache
  // entry is a housekeeping miss, and it must never surface as an application error.
  requestAnimationFrame(() => {
    persister.persisterGc().catch(() => {
      /* IndexedDB is unavailable or blocked; the sweep simply repeats next launch. */
    });
  });

  return () => {
    document.removeEventListener('visibilitychange', onHidden);
    window.removeEventListener('pagehide', onPageHide);
    installed = false;
  };
}

/** Erase the hot snapshot and the IndexedDB store. Does not touch the in-memory cache. */
export async function clearPersistedCache(): Promise<void> {
  try {
    window.localStorage.removeItem(HOT_KEY);
  } catch {
    /* Nothing to clear. */
  }
  try {
    await clear(idbStore());
  } catch {
    /* The store may not exist yet, which is the desired end state anyway. */
  }
}

/**
 * Drop every cached entry and every persisted byte.
 *
 * Called from `Settings → Reset Local Cache`, and by {@link verifyBackendBuild} when the
 * backend turns out to be a different build than the one the cache was written against.
 */
export async function resetCache(): Promise<void> {
  queryClient.clear();
  await clearPersistedCache();
}

/**
 * Compare the running backend's version against the one the cache was written against.
 *
 * A backend upgrade can change a DTO without the renderer's `API_SCHEMA_VERSION` moving —
 * the two ship together in a bundle, but a developer running a newer sidecar against an older
 * renderer is exactly the case that produces baffling half-populated screens. Cheap to check
 * at boot, and the failure it prevents is expensive to debug.
 *
 * @param version - `HealthReport.version` from `GET /health`.
 * @returns Whether the cache was cleared.
 */
export async function verifyBackendBuild(version: string): Promise<boolean> {
  let previous: string | null = null;
  try {
    previous = window.localStorage.getItem(BUILD_KEY);
  } catch {
    return false;
  }

  if (previous === version) return false;

  try {
    window.localStorage.setItem(BUILD_KEY, version);
  } catch {
    /* Unwritable storage; the check simply repeats next launch. */
  }

  // A first run has nothing to invalidate — there is no cache to be wrong.
  if (previous === null) return false;

  await resetCache();
  return true;
}
