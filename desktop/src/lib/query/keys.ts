/**
 * The query-key factory (`docs/UI.md` §10.4).
 *
 * Every key in the app is built here. Two properties make that worth the indirection:
 *
 * **Prefix matching works.** `qk.applications()` is a prefix of both `qk.applicationList(f)`
 * and `qk.applicationDetail(id)`, so one `setQueriesData({ queryKey: [...qk.applications(),
 * 'list'] })` reaches every filtered list at once — which is what makes a WebSocket event
 * patch twelve cached pages without knowing what any of them were filtered by.
 *
 * **Filters are normalised before they are hashed.** TanStack hashes keys with a stable
 * stringify that sorts object keys, so `{a, b}` and `{b, a}` are the same key — **but
 * `q: ''` and `q: undefined` hash differently**, and silently fragment the cache into
 * unreachable duplicates. That is precisely what makes a "cached" app still show spinners:
 * every keystroke that clears the search box mints a key nothing will ever look up again.
 * {@link normalizeFilters} is therefore not optional, and no key here takes a raw object.
 */

import { MAX_PAGE_LIMIT, PAGE_LIMIT } from '@/lib/api/types';
import type {
  ApplicationFilters,
  EntityFilters,
  FactFilters,
  GraphFilters,
  LogFilters,
  PageParams,
  PluginFilters,
  PostingFilters,
  SignalFilters,
  Uuid,
} from '@/lib/api/types';
import { clamp } from '@/lib/utils';

/** A normalised filter object: no `undefined`, no `null`, no empty strings. */
export type NormalizedFilters = Readonly<Record<string, unknown>>;

/**
 * Strip the values that fragment the cache, and pin `limit` to the fixed page size.
 *
 * `undefined`, `null` and `''` all mean "not filtering", and all three hash differently. They
 * are dropped so that every way of expressing "no filter" lands on one key.
 *
 * `limit` gets its own rule: an absent limit and an explicit `limit: 50` are the same
 * request, so the default is dropped rather than recorded. Anything else is clamped into the
 * range the server will actually honour, so a client bug cannot mint a key shape per page
 * size — and cannot ask for an unbounded scan of a table that grows without bound.
 */
export function normalizeFilters(filters: object | undefined): NormalizedFilters {
  if (filters === undefined) return {};
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === '') continue;
    if (key === 'limit') {
      const limit = clamp(Math.trunc(Number(value)), 1, MAX_PAGE_LIMIT);
      if (limit === PAGE_LIMIT) continue;
      out[key] = limit;
      continue;
    }
    if (key === 'offset') {
      const offset = Math.max(0, Math.trunc(Number(value)));
      if (offset === 0) continue;
      out[key] = offset;
      continue;
    }
    out[key] = value;
  }
  return out;
}

/**
 * The key factory. Every family is reachable by prefix from the one above it.
 *
 * Root is `['aos']` rather than `[]` so that `queryClient.removeQueries({ queryKey: qk.all })`
 * cannot take anything a future library put in the same cache with it.
 */
export const qk = {
  all: ['aos'] as const,

  // ── Health ────────────────────────────────────────────────────────────────────────
  health: () => [...qk.all, 'health'] as const,
  readiness: () => [...qk.all, 'readiness'] as const,

  // ── Postings ──────────────────────────────────────────────────────────────────────
  postings: () => [...qk.all, 'postings'] as const,
  postingList: (filters: PostingFilters = {}) =>
    [...qk.postings(), 'list', normalizeFilters(filters)] as const,
  postingDetail: (id: Uuid) => [...qk.postings(), 'detail', id] as const,

  // ── Applications ──────────────────────────────────────────────────────────────────
  applications: () => [...qk.all, 'applications'] as const,
  /** Prefix shared by every filtered list, for a single `setQueriesData` fan-out. */
  applicationLists: () => [...qk.applications(), 'list'] as const,
  applicationList: (filters: ApplicationFilters = {}) =>
    [...qk.applicationLists(), normalizeFilters(filters)] as const,
  applicationDetail: (id: Uuid) => [...qk.applications(), 'detail', id] as const,
  applicationArtifacts: (id: Uuid) => [...qk.applications(), 'artifacts', id] as const,

  // ── Review queue ──────────────────────────────────────────────────────────────────
  reviews: () => [...qk.all, 'reviews'] as const,
  reviewList: (filters: ApplicationFilters = {}) =>
    [...qk.reviews(), 'list', normalizeFilters(filters)] as const,

  // ── Sessions ──────────────────────────────────────────────────────────────────────
  sessions: () => [...qk.all, 'sessions'] as const,
  sessionList: (params: PageParams = {}) =>
    [...qk.sessions(), 'list', normalizeFilters(params)] as const,
  sessionDetail: (id: Uuid) => [...qk.sessions(), 'detail', id] as const,

  // ── Analytics ─────────────────────────────────────────────────────────────────────
  analyticsAll: () => [...qk.all, 'analytics'] as const,
  /** `kind` is one of `overview` | `funnel` | `insights`. */
  analytics: (kind: string, days?: number) =>
    [...qk.analyticsAll(), kind, normalizeFilters(days === undefined ? {} : { days })] as const,
  timeseries: (days: number) => [...qk.analyticsAll(), 'timeseries', days] as const,

  // ── Knowledge ─────────────────────────────────────────────────────────────────────
  knowledge: () => [...qk.all, 'knowledge'] as const,
  knowledgeSources: (params: PageParams = {}) =>
    [...qk.knowledge(), 'sources', normalizeFilters(params)] as const,
  knowledgeFacts: (filters: FactFilters = {}) =>
    [...qk.knowledge(), 'facts', normalizeFilters(filters)] as const,
  knowledgeEntities: (filters: EntityFilters = {}) =>
    [...qk.knowledge(), 'entities', normalizeFilters(filters)] as const,
  knowledgeGraph: (filters: GraphFilters = {}) =>
    [...qk.knowledge(), 'graph', normalizeFilters(filters)] as const,
  knowledgeStats: () => [...qk.knowledge(), 'stats'] as const,
  knowledgeSearch: (query: string, options: { kinds?: string; limit?: number } = {}) =>
    [...qk.knowledge(), 'search', query, normalizeFilters(options)] as const,

  // ── Resumes ───────────────────────────────────────────────────────────────────────
  resumes: () => [...qk.all, 'resumes'] as const,
  resumeList: (params: PageParams = {}) =>
    [...qk.resumes(), 'list', normalizeFilters(params)] as const,
  resumeVersion: (id: Uuid) => [...qk.resumes(), 'version', id] as const,

  // ── Profile and settings ──────────────────────────────────────────────────────────
  settingsAll: () => [...qk.all, 'settings'] as const,
  settings: () => [...qk.settingsAll(), 'runtime'] as const,
  profile: () => [...qk.settingsAll(), 'profile'] as const,
  preferences: () => [...qk.settingsAll(), 'preferences'] as const,
  plugins: (filters: PluginFilters = {}) =>
    [...qk.settingsAll(), 'plugins', normalizeFilters(filters)] as const,
  scoringRules: () => [...qk.settingsAll(), 'scoring-rules'] as const,

  // ── Onboarding ────────────────────────────────────────────────────────────────────
  onboarding: () => [...qk.all, 'onboarding'] as const,
  onboardingStatus: () => [...qk.onboarding(), 'status'] as const,
  onboardingSteps: () => [...qk.onboarding(), 'steps'] as const,

  // ── Logs (historical search only — the live tail is a ring buffer, §7.21) ──────────
  logs: (filters: LogFilters = {}) =>
    [...qk.all, 'logs', normalizeFilters(filters)] as const,

  // ── Status sync ───────────────────────────────────────────────────────────────────
  tracking: () => [...qk.all, 'tracking'] as const,
  trackingAccounts: () => [...qk.tracking(), 'accounts'] as const,
  trackingSignals: (filters: SignalFilters = {}) =>
    [...qk.tracking(), 'signals', normalizeFilters(filters)] as const,
  trackingStats: () => [...qk.tracking(), 'stats'] as const,
} as const;

/**
 * The shape of any key this factory produces.
 *
 * Deliberately structural rather than a union of the factory's return types: TanStack's own
 * `QueryKey` is `readonly unknown[]`, and a narrower alias here would fight every generic in
 * the library without catching anything the factory does not already guarantee.
 */
export type QueryKey = readonly unknown[];
