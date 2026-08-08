/**
 * Analytics — the dashboard's figures and the charts on `/analytics`.
 *
 * `GET /analytics/overview` returns stats, funnel, timeseries and insights in one response,
 * which is why the dashboard is a single query rather than four: one request, one cache
 * entry, one hot-snapshot key, and no possibility of the hero figure and the funnel
 * disagreeing because they were fetched a second apart.
 *
 * The narrower endpoints exist for the analytics screen, where the user picks a window and
 * only the timeseries changes. `staleTime` is 30 seconds across the family and the whole
 * family is invalidated by `session.finished`, so a run that lands while the user is looking
 * at the dashboard updates without polling.
 */

import { useQuery } from '@tanstack/react-query';

import type { AnalyticsOverview, DashboardStats } from '@/lib/api/types';
import {
  analyticsOverviewOptions,
  funnelOptions,
  insightsOptions,
  timeseriesOptions,
} from '@/lib/query/options';

/** `GET /analytics/overview`. The dashboard's entire payload. */
export function useAnalyticsOverview(days = 30) {
  return useQuery(analyticsOverviewOptions(days));
}

/**
 * Just the counter tiles.
 *
 * Module-scope selector, not an inline arrow: a fresh function every render defeats both the
 * subscription narrowing and structural sharing, so the tiles would re-render on every field
 * of the overview rather than on the seven numbers they show.
 */
const selectStats = (overview: AnalyticsOverview): DashboardStats => overview.stats;

/** The dashboard's counter tiles, narrowed so unrelated fields do not re-render them. */
export function useDashboardStats(days = 30) {
  return useQuery({ ...analyticsOverviewOptions(days), select: selectStats });
}

/** `GET /analytics/funnel` — discovery → offer, as ordered stages. */
export function useFunnel() {
  return useQuery(funnelOptions());
}

/** `GET /analytics/timeseries` — one point per day. */
export function useTimeseries(days = 30) {
  return useQuery(timeseriesOptions(days));
}

/** `GET /analytics/insights` — observational findings, each with its own sample size. */
export function useInsights() {
  return useQuery(insightsOptions());
}
