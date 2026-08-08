/**
 * The route tree (`docs/UI.md` §10.6, §10.7).
 *
 * Code-based rather than file-based routing, because the tree is fixed at eleven destinations
 * and a generated route file would be one more artefact to keep in sync with `navigation.ts`
 * and the keymap. Every route's component is imported eagerly and **nothing is code-split**
 * except the two heavy leaves §10.7 names: code splitting optimises network transfer, which
 * does not exist in a packaged desktop app, and all it can buy here is a per-route chunk fetch
 * that becomes the one visible navigation delay.
 *
 * Three router options carry the instant-feel contract:
 *
 * - **`defaultPreload: 'intent'` with a 50ms delay** — hovering or focusing a link starts its
 *   loader, and the delay means a pointer sweep does not start eleven of them.
 * - **`defaultPreloadStaleTime: 0`** — TanStack *Query* owns freshness. Letting the router
 *   cache loader results too would give the app two answers to "is this stale".
 * - **`defaultPendingMs: 1000` with a 500ms floor** — with a warm cache the pending component
 *   is unreachable, and if it ever is reached it must not flash and vanish.
 *
 * Loaders call `ensureQueryData` against the *same* `queryOptions` object the screen consumes.
 * That shared object is what guarantees the key the loader warms is the key the component
 * looks up; a hand-built key on either side is the classic cause of "the loader ran and the
 * component still suspended".
 *
 * **They start the request and do not wait for it.** `ensureQueryData` returns cached data
 * immediately when there is any, so on a warm cache awaiting it costs nothing — but on a cold
 * one an awaited loader blanks the content region until the network answers, and §10.14 budgets
 * a route change at one frame. Every screen already gates its own content on `isPending`
 * (§10.1), so the loader's whole job is to get the request in flight *earlier* than the
 * component could — which is exactly what preload-on-intent buys and what awaiting would throw
 * away.
 */

import {
  createRootRoute,
  createRoute,
  createRouter,
  type AnyRoute,
} from '@tanstack/react-router';

import { AppShell } from '@/app';
import { NotFoundRoute } from '@/routes/not-found';
import { AnalyticsRoute } from '@/routes/analytics';
import { ApplicationDetailRoute } from '@/routes/applications/$id';
import { ApplicationsRoute } from '@/routes/applications';
import { DashboardRoute } from '@/routes';
import { KnowledgeRoute } from '@/routes/knowledge';
import { LogsRoute } from '@/routes/logs';
import { OnboardingRoute } from '@/routes/onboarding';
import { PostingsRoute } from '@/routes/postings';
import { ResumesRoute } from '@/routes/resumes';
import { ReviewsRoute } from '@/routes/reviews';
import { SessionsRoute } from '@/routes/sessions';
import { SettingsRoute } from '@/routes/settings';
import { TrackingRoute } from '@/routes/tracking';
import { DASHBOARD_WINDOW_DAYS, queryClient } from '@/lib/query/client';
import {
  analyticsOverviewOptions,
  applicationDetailOptions,
  applicationListOptions,
  emailAccountsOptions,
  knowledgeStatsOptions,
  onboardingStepsOptions,
  postingListOptions,
  resumeListOptions,
  reviewListOptions,
  sessionListOptions,
  settingsOptions,
} from '@/lib/query/options';

/** How long a hover or focus must dwell before the router starts a route's loader. */
const PRELOAD_DELAY_MS = 50;

/** Below this, a route's pending component never appears at all. */
const PENDING_MS = 1_000;

/** Once it does appear, it stays at least this long. */
const PENDING_MIN_MS = 500;

/** The shell. Never unmounts; only its `<Outlet/>` changes. */
const rootRoute = createRootRoute({
  component: AppShell,
  notFoundComponent: NotFoundRoute,
});

/**
 * Swallow a settled warm-up.
 *
 * **A loader must never throw.** An error boundary thrown over a screen full of perfectly good
 * cached data, because the network blinked, is strictly worse than the stale-but-visible
 * alternative — and the screen's own query already knows how to report a failure in place.
 */
const swallow = (): undefined => undefined;

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: DashboardRoute,
  loader: () => {
    void queryClient.ensureQueryData(analyticsOverviewOptions(DASHBOARD_WINDOW_DAYS)).then(swallow, swallow);
  },
});

const onboardingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/onboarding',
  component: OnboardingRoute,
  loader: () => {
    void queryClient.ensureQueryData(onboardingStepsOptions()).then(swallow, swallow);
  },
});

const applicationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/applications',
  component: ApplicationsRoute,
  loader: () => {
    void queryClient.ensureQueryData(applicationListOptions({})).then(swallow, swallow);
  },
});

const applicationDetail = createRoute({
  getParentRoute: () => rootRoute,
  path: '/applications/$id',
  loader: ({ params }) => {
    void queryClient.ensureQueryData(applicationDetailOptions(params.id)).then(swallow, swallow);
  },
  component: function ApplicationDetailScreen() {
    const { id } = applicationDetail.useParams();
    return <ApplicationDetailRoute id={id} />;
  },
});

const postingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/postings',
  component: PostingsRoute,
  loader: () => {
    void queryClient.ensureQueryData(postingListOptions({})).then(swallow, swallow);
  },
});

const reviewsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reviews',
  component: ReviewsRoute,
  loader: () => {
    void queryClient.ensureQueryData(reviewListOptions({})).then(swallow, swallow);
  },
});

const knowledgeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/knowledge',
  component: KnowledgeRoute,
  loader: () => {
    void queryClient.ensureQueryData(knowledgeStatsOptions()).then(swallow, swallow);
  },
});

const resumesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/resumes',
  component: ResumesRoute,
  loader: () => {
    void queryClient.ensureQueryData(resumeListOptions({})).then(swallow, swallow);
  },
});

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/sessions',
  component: SessionsRoute,
  loader: () => {
    void queryClient.ensureQueryData(sessionListOptions({})).then(swallow, swallow);
  },
});

const analyticsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/analytics',
  component: AnalyticsRoute,
  loader: () => {
    void queryClient.ensureQueryData(analyticsOverviewOptions(30)).then(swallow, swallow);
  },
});

const trackingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/tracking',
  component: TrackingRoute,
  loader: () => {
    void queryClient.ensureQueryData(emailAccountsOptions()).then(swallow, swallow);
  },
});

const logsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/logs',
  component: LogsRoute,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/settings',
  component: SettingsRoute,
  loader: () => {
    void queryClient.ensureQueryData(settingsOptions()).then(swallow, swallow);
  },
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  onboardingRoute,
  applicationsRoute,
  applicationDetail,
  postingsRoute,
  reviewsRoute,
  knowledgeRoute,
  resumesRoute,
  sessionsRoute,
  analyticsRoute,
  trackingRoute,
  logsRoute,
  settingsRoute,
] as AnyRoute[]);

/** The application's router. */
export const router = createRouter({
  routeTree,
  context: { queryClient },
  defaultPreload: 'intent',
  defaultPreloadDelay: PRELOAD_DELAY_MS,
  // TanStack Query owns freshness; a second cache with its own staleness would be a second
  // answer to the same question.
  defaultPreloadStaleTime: 0,
  defaultPendingMs: PENDING_MS,
  defaultPendingMinMs: PENDING_MIN_MS,
  defaultStaleTime: Number.POSITIVE_INFINITY,
  scrollRestoration: true,
  defaultNotFoundComponent: NotFoundRoute,
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}
