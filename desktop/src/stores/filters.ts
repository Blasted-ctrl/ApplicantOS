/**
 * List filters, one slice per screen.
 *
 * Filters live in a store rather than in each route's local state for a reason that is really
 * a caching decision: a filter object is half of a query key, and if it were remounted with
 * the route it would reset on every navigation, minting a fresh key and re-fetching a list
 * the cache already holds. Keeping them here is what makes "go to Postings, come back to
 * Applications" land on the same key and therefore on the same cached page.
 *
 * Everything here is deliberately **not** persisted. A filter restored from a previous
 * session is the classic "why is my list empty" bug: the user sees a screen with rows
 * missing and no memory of having filtered anything. The toolbar's active chips are the only
 * honest record of a filter, and they are only honest within a session.
 *
 * `q` is stored raw. The deferred value that actually enters the query key is derived at the
 * call site with `useDeferredValue` (`docs/UI.md` §10.12), so typing never blocks on a
 * request and the input stays on the same frame as the keystroke.
 */

import { create } from 'zustand';

import type {
  ApplicationFilters,
  FactFilters,
  LogFilters,
  PostingFilters,
  SignalFilters,
} from '@/lib/api/types';

/** Filter state, one key per list screen. */
interface FiltersState {
  applications: ApplicationFilters;
  postings: PostingFilters;
  reviews: ApplicationFilters;
  facts: FactFilters;
  logs: LogFilters;
  signals: SignalFilters;

  setApplications: (patch: Partial<ApplicationFilters>) => void;
  setPostings: (patch: Partial<PostingFilters>) => void;
  setReviews: (patch: Partial<ApplicationFilters>) => void;
  setFacts: (patch: Partial<FactFilters>) => void;
  setLogs: (patch: Partial<LogFilters>) => void;
  setSignals: (patch: Partial<SignalFilters>) => void;

  /** Clear one screen's filters. What the "no results" empty state's primary action calls. */
  clear: (screen: FilterScreen) => void;
  /** Clear every screen's filters. Bound to the palette's `Clear all filters` action. */
  clearAll: () => void;
}

/** The screens that carry filters. */
export type FilterScreen = 'applications' | 'postings' | 'reviews' | 'facts' | 'logs' | 'signals';

/** Pagination always starts at the first page; the offset is not a user-facing filter. */
const EMPTY: Readonly<Record<FilterScreen, object>> = {
  applications: {},
  postings: {},
  reviews: {},
  facts: {},
  logs: {},
  signals: {},
};

/**
 * Merge a patch, dropping keys the caller cleared.
 *
 * A cleared filter is removed rather than set to `undefined`, so the object that reaches
 * `normalizeFilters` is already minimal — and so `Object.keys(filters).length` is a truthful
 * "how many filters are active" for the toolbar's chip row.
 *
 * Changing any filter resets `offset`, because page 4 of a different result set is not a
 * page the user asked for.
 */
function merge<T extends object>(current: T, patch: Partial<T>): T {
  const next: Record<string, unknown> = { ...(current as Record<string, unknown>) };
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined || value === null || value === '') delete next[key];
    else next[key] = value;
  }
  if (!('offset' in patch)) delete next['offset'];
  return next as T;
}

/** List filters. Session-scoped by design — see the module docstring. */
export const useFiltersStore = create<FiltersState>()((set) => ({
  applications: {},
  postings: {},
  reviews: {},
  facts: {},
  logs: {},
  signals: {},

  setApplications: (patch) => {
    set((s) => ({ applications: merge(s.applications, patch) }));
  },
  setPostings: (patch) => {
    set((s) => ({ postings: merge(s.postings, patch) }));
  },
  setReviews: (patch) => {
    set((s) => ({ reviews: merge(s.reviews, patch) }));
  },
  setFacts: (patch) => {
    set((s) => ({ facts: merge(s.facts, patch) }));
  },
  setLogs: (patch) => {
    set((s) => ({ logs: merge(s.logs, patch) }));
  },
  setSignals: (patch) => {
    set((s) => ({ signals: merge(s.signals, patch) }));
  },

  clear: (screen) => {
    set({ [screen]: EMPTY[screen] } as Pick<FiltersState, FilterScreen>);
  },
  clearAll: () => {
    set({ ...EMPTY } as Pick<FiltersState, FilterScreen>);
  },
}));

/**
 * How many filters are active on a screen, ignoring pagination.
 *
 * Drives two things §7.17 requires: the "Clear filters" affordance in the toolbar, and the
 * choice between the *blank slate* and the *no results* empty state — which must quote the
 * query and offer a one-click clear.
 */
export function activeFilterCount(filters: object): number {
  return Object.entries(filters).filter(
    ([key, value]) =>
      key !== 'limit' &&
      key !== 'offset' &&
      value !== undefined &&
      value !== null &&
      value !== '',
  ).length;
}
