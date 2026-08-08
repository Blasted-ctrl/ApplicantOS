/**
 * Shared `queryOptions` objects — one per query family.
 *
 * `docs/UI.md` §10.6 requires a route loader and the component it renders to consume the
 * *same* options object: the loader calls `ensureQueryData` on it, the component calls
 * `useQuery`/`useSuspenseQuery` on it, and because the key and the `queryFn` come from one
 * place they cannot drift. A mismatched key between loader and component is the classic cause
 * of "the loader warmed the cache but the component still suspends", and it is invisible in
 * review because both sides look right on their own.
 *
 * Loaders must call **`ensureQueryData`, never `fetchQuery`**: the former returns cached data
 * immediately and ignores `staleTime`, which is what makes a revisit instant.
 *
 * Every family carries the `staleTime` and `gcTime` from the binding table in §10.3, so no
 * call site chooses a freshness policy. `placeholderData: keepPreviousData` is attached to
 * the list families for the same reason it exists in §10.12 — a filter change must dim the
 * current rows, never empty the table.
 */

import { keepPreviousData, queryOptions } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
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

import { GC, STALE } from './client';
import { qk } from './keys';

// ── Health ──────────────────────────────────────────────────────────────────────────

/** `GET /health` — liveness and the backend build id. */
export const healthOptions = () =>
  queryOptions({
    queryKey: qk.health(),
    queryFn: ({ signal }) => api.getHealth(signal),
    staleTime: STALE.health,
    gcTime: GC.health,
    retry: false,
  });

/** `GET /ready` — per-dependency readiness, for the backend-unavailable screen. */
export const readinessOptions = () =>
  queryOptions({
    queryKey: qk.readiness(),
    queryFn: ({ signal }) => api.getReadiness(signal),
    staleTime: STALE.health,
    gcTime: GC.health,
    retry: false,
  });

// ── Postings ────────────────────────────────────────────────────────────────────────

/** `GET /postings`. Invalidated by `posting.discovered` and patched by `posting.scored`. */
export const postingListOptions = (filters: PostingFilters = {}) =>
  queryOptions({
    queryKey: qk.postingList(filters),
    queryFn: ({ signal }) => api.listPostings(filters, signal),
    staleTime: STALE.postingList,
    gcTime: GC.postingList,
    placeholderData: keepPreviousData,
  });

/** `GET /postings/{id}`. */
export const postingDetailOptions = (id: Uuid) =>
  queryOptions({
    queryKey: qk.postingDetail(id),
    queryFn: ({ signal }) => api.getPosting(id, signal),
    staleTime: STALE.postingDetail,
    gcTime: GC.postingDetail,
  });

// ── Applications ────────────────────────────────────────────────────────────────────

/** `GET /applications`. Patched in place by every `application.*` frame. */
export const applicationListOptions = (filters: ApplicationFilters = {}) =>
  queryOptions({
    queryKey: qk.applicationList(filters),
    queryFn: ({ signal }) => api.listApplications(filters, signal),
    staleTime: STALE.applicationList,
    gcTime: GC.applicationList,
    placeholderData: keepPreviousData,
  });

/** `GET /applications/{id}`. */
export const applicationDetailOptions = (id: Uuid) =>
  queryOptions({
    queryKey: qk.applicationDetail(id),
    queryFn: ({ signal }) => api.getApplication(id, signal),
    staleTime: STALE.applicationDetail,
    gcTime: GC.applicationDetail,
  });

/** `GET /applications/{id}/artifacts`. */
export const applicationArtifactsOptions = (id: Uuid) =>
  queryOptions({
    queryKey: qk.applicationArtifacts(id),
    queryFn: ({ signal }) => api.listApplicationArtifacts(id, undefined, signal),
    staleTime: STALE.applicationArtifacts,
    gcTime: GC.applicationArtifacts,
  });

// ── Review queue ────────────────────────────────────────────────────────────────────

/**
 * `GET /reviews`.
 *
 * The one family with a short `staleTime` (15s): a stale review count is the only staleness
 * in the product that is user-visible harm, because it is the number the sidebar badge shows
 * and the number the user acts on first thing in the morning.
 */
export const reviewListOptions = (filters: ApplicationFilters = {}) =>
  queryOptions({
    queryKey: qk.reviewList(filters),
    queryFn: ({ signal }) => api.listReviews(filters, signal),
    staleTime: STALE.reviews,
    gcTime: GC.reviews,
    placeholderData: keepPreviousData,
  });

// ── Sessions ────────────────────────────────────────────────────────────────────────

/** `GET /sessions`. */
export const sessionListOptions = (params: PageParams = {}) =>
  queryOptions({
    queryKey: qk.sessionList(params),
    queryFn: ({ signal }) => api.listSessions(params, signal),
    staleTime: STALE.sessions,
    gcTime: GC.sessions,
    placeholderData: keepPreviousData,
  });

/** `GET /sessions/{id}`. */
export const sessionDetailOptions = (id: Uuid) =>
  queryOptions({
    queryKey: qk.sessionDetail(id),
    queryFn: ({ signal }) => api.getSession(id, signal),
    staleTime: STALE.sessions,
    gcTime: GC.sessions,
  });

// ── Analytics ───────────────────────────────────────────────────────────────────────

/** `GET /analytics/overview` — the dashboard's whole payload in one request. */
export const analyticsOverviewOptions = (days = 30) =>
  queryOptions({
    queryKey: qk.analytics('overview', days),
    queryFn: ({ signal }) => api.getAnalyticsOverview(days, signal),
    staleTime: STALE.analytics,
    gcTime: GC.analytics,
  });

/** `GET /analytics/funnel`. */
export const funnelOptions = () =>
  queryOptions({
    queryKey: qk.analytics('funnel'),
    queryFn: ({ signal }) => api.getFunnel(signal),
    staleTime: STALE.analytics,
    gcTime: GC.analytics,
  });

/** `GET /analytics/timeseries`. */
export const timeseriesOptions = (days = 30) =>
  queryOptions({
    queryKey: qk.timeseries(days),
    queryFn: ({ signal }) => api.getTimeseries(days, signal),
    staleTime: STALE.timeseries,
    gcTime: GC.timeseries,
    placeholderData: keepPreviousData,
  });

/** `GET /analytics/insights`. */
export const insightsOptions = () =>
  queryOptions({
    queryKey: qk.analytics('insights'),
    queryFn: ({ signal }) => api.getInsights(signal),
    staleTime: STALE.analytics,
    gcTime: GC.analytics,
  });

// ── Knowledge ───────────────────────────────────────────────────────────────────────

/** `GET /knowledge/sources`. */
export const knowledgeSourcesOptions = (params: PageParams = {}) =>
  queryOptions({
    queryKey: qk.knowledgeSources(params),
    queryFn: ({ signal }) => api.listSources(params, signal),
    staleTime: STALE.knowledge,
    gcTime: GC.knowledge,
  });

/** `GET /knowledge/facts`. */
export const factsOptions = (filters: FactFilters = {}) =>
  queryOptions({
    queryKey: qk.knowledgeFacts(filters),
    queryFn: ({ signal }) => api.listFacts(filters, signal),
    staleTime: STALE.knowledge,
    gcTime: GC.knowledge,
    placeholderData: keepPreviousData,
  });

/** `GET /knowledge/entities`. */
export const entitiesOptions = (filters: EntityFilters = {}) =>
  queryOptions({
    queryKey: qk.knowledgeEntities(filters),
    queryFn: ({ signal }) => api.listEntities(filters, signal),
    staleTime: STALE.knowledge,
    gcTime: GC.knowledge,
    placeholderData: keepPreviousData,
  });

/** `GET /knowledge/graph` — capped server-side, so this is a slice, not the whole graph. */
export const knowledgeGraphOptions = (filters: GraphFilters = {}) =>
  queryOptions({
    queryKey: qk.knowledgeGraph(filters),
    queryFn: ({ signal }) => api.getGraph(filters, signal),
    staleTime: STALE.knowledgeGraph,
    gcTime: GC.knowledgeGraph,
  });

/** `GET /knowledge/stats`. */
export const knowledgeStatsOptions = () =>
  queryOptions({
    queryKey: qk.knowledgeStats(),
    queryFn: ({ signal }) => api.getKnowledgeStats(signal),
    staleTime: STALE.knowledge,
    gcTime: GC.knowledge,
  });

/**
 * `GET /knowledge/search`.
 *
 * `enabled` is false for an empty query rather than the caller guarding: an empty search is
 * not a request with no results, it is not a request.
 */
export const knowledgeSearchOptions = (
  query: string,
  options: { kinds?: string; limit?: number } = {},
) =>
  queryOptions({
    queryKey: qk.knowledgeSearch(query, options),
    queryFn: ({ signal }) => api.searchKnowledge(query, options, signal),
    enabled: query.trim() !== '',
    staleTime: STALE.knowledge,
    gcTime: GC.knowledge,
    placeholderData: keepPreviousData,
  });

// ── Resumes ─────────────────────────────────────────────────────────────────────────

/** `GET /resumes`. */
export const resumeListOptions = (params: PageParams = {}) =>
  queryOptions({
    queryKey: qk.resumeList(params),
    queryFn: ({ signal }) => api.listResumes(params, signal),
    staleTime: STALE.resumes,
    gcTime: GC.resumes,
  });

/** `GET /resumes/versions/{id}`. */
export const resumeVersionOptions = (id: Uuid) =>
  queryOptions({
    queryKey: qk.resumeVersion(id),
    queryFn: ({ signal }) => api.getResumeVersion(id, signal),
    staleTime: STALE.resumes,
    gcTime: GC.resumes,
  });

// ── Profile and settings ────────────────────────────────────────────────────────────

/** `GET /profile`. */
export const profileOptions = () =>
  queryOptions({
    queryKey: qk.profile(),
    queryFn: ({ signal }) => api.getProfile(signal),
    staleTime: STALE.settings,
    gcTime: GC.settings,
  });

/** `GET /profile/preferences`. */
export const preferencesOptions = () =>
  queryOptions({
    queryKey: qk.preferences(),
    queryFn: ({ signal }) => api.getPreferences(signal),
    staleTime: STALE.settings,
    gcTime: GC.settings,
  });

/** `GET /settings` — including the two halves of the kill switch. */
export const settingsOptions = () =>
  queryOptions({
    queryKey: qk.settings(),
    queryFn: ({ signal }) => api.getSettings(signal),
    staleTime: STALE.settings,
    gcTime: GC.settings,
  });

/** `GET /settings/plugins`. Changes only on restart, hence `staleTime: Infinity`. */
export const pluginsOptions = (filters: PluginFilters = {}) =>
  queryOptions({
    queryKey: qk.plugins(filters),
    queryFn: ({ signal }) => api.listPlugins(filters, signal),
    staleTime: STALE.plugins,
    gcTime: GC.plugins,
  });

/** `GET /settings/scoring-rules`. */
export const scoringRulesOptions = () =>
  queryOptions({
    queryKey: qk.scoringRules(),
    queryFn: ({ signal }) => api.getScoringRules(signal),
    staleTime: STALE.settings,
    gcTime: GC.settings,
  });

// ── Onboarding ──────────────────────────────────────────────────────────────────────

/**
 * `GET /onboarding/status`.
 *
 * `staleTime: 0` and never persisted: a persisted wizard state would resurrect a step the
 * user already completed, which is the one place in the app where stale data would ask the
 * user to redo work.
 */
export const onboardingStatusOptions = () =>
  queryOptions({
    queryKey: qk.onboardingStatus(),
    queryFn: ({ signal }) => api.getOnboardingStatus(signal),
    staleTime: STALE.onboarding,
    gcTime: GC.onboarding,
  });

/** `GET /onboarding/steps`. */
export const onboardingStepsOptions = () =>
  queryOptions({
    queryKey: qk.onboardingSteps(),
    queryFn: ({ signal }) => api.getOnboardingSteps(signal),
    staleTime: STALE.onboarding,
    gcTime: GC.onboarding,
  });

// ── Logs ────────────────────────────────────────────────────────────────────────────

/** `GET /logs` — historical search. The live tail is the ring buffer, not this. */
export const logsOptions = (filters: LogFilters = {}) =>
  queryOptions({
    queryKey: qk.logs(filters),
    queryFn: ({ signal }) => api.listLogs(filters, signal),
    staleTime: STALE.logs,
    gcTime: GC.logs,
    placeholderData: keepPreviousData,
  });

// ── Status sync ─────────────────────────────────────────────────────────────────────

/** `GET /tracking/accounts`. */
export const emailAccountsOptions = () =>
  queryOptions({
    queryKey: qk.trackingAccounts(),
    queryFn: ({ signal }) => api.listEmailAccounts(signal),
    staleTime: STALE.tracking,
    gcTime: GC.tracking,
  });

/** `GET /tracking/signals`. */
export const signalsOptions = (filters: SignalFilters = {}) =>
  queryOptions({
    queryKey: qk.trackingSignals(filters),
    queryFn: ({ signal }) => api.listSignals(filters, signal),
    staleTime: STALE.tracking,
    gcTime: GC.tracking,
    placeholderData: keepPreviousData,
  });

/** `GET /tracking/stats`. */
export const trackingStatsOptions = () =>
  queryOptions({
    queryKey: qk.trackingStats(),
    queryFn: ({ signal }) => api.getTrackingStats(signal),
    staleTime: STALE.tracking,
    gcTime: GC.tracking,
  });
