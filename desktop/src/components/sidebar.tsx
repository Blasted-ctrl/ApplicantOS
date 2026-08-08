/**
 * The sidebar (`docs/UI.md` §5.3).
 *
 * 232px expanded, 52px collapsed, `--bg-chrome` — **darker than the content region**, because
 * the content plane is the lit surface and chrome recedes rather than shrinks. It never
 * scrolls: the destination table is fixed and short by design.
 *
 * Two behaviours are worth naming:
 *
 * **The review badge is the only count that changes colour.** It turns `--st-review` and gains
 * a wash the moment the queue is non-empty, and it carries a visually-hidden live region —
 * §12.5 lists it as the one count in the product worth interrupting a screen reader for.
 *
 * **Rows warm their destination on intent.** A row the pointer rests on for 60ms is a row
 * about to be clicked, so its screen's primary query is already in flight by the time the
 * click lands (§10.6). The warm-up is a `prefetchQuery` against the same `queryOptions` object
 * the screen consumes, so the key cannot drift from the one the screen will look up.
 */

import { useNavigate, useRouterState } from '@tanstack/react-router';
import { PanelLeft } from 'lucide-react';
import { useCallback, useRef } from 'react';

import { SidebarGroupLabel, SidebarItem } from '@/components/ui';
import { useReviewCount } from '@/hooks';
import { queryClient } from '@/lib/query/client';
import {
  analyticsOverviewOptions,
  applicationListOptions,
  emailAccountsOptions,
  knowledgeStatsOptions,
  postingListOptions,
  resumeListOptions,
  reviewListOptions,
  sessionListOptions,
  settingsOptions,
} from '@/lib/query/options';
import { SHORTCUTS_BY_ID } from '@/lib/shortcuts';
import { cn } from '@/lib/utils';
import { useUiStore } from '@/stores/ui';

import {
  DESTINATIONS,
  isActivePath,
  NAV_GROUPS,
  SETTINGS_DESTINATION,
  type AppPath,
  type Destination,
} from './navigation';

/** Hover dwell before a destination's data is warmed. Matches §10.6's row-prefetch delay. */
const INTENT_DELAY_MS = 60;

/** Freshness floor for a warm-up: fresher data is not refetched. */
const WARM_STALE_MS = 60_000;

/**
 * The query each destination paints from first.
 *
 * Only the *primary* query per screen: warming a screen's five secondary queries on hover
 * would turn a mouse sweep down the sidebar into thirty requests, which is the failure mode
 * §10.6's delay exists to prevent in the first place.
 */
const WARM: Readonly<Record<AppPath, (() => void) | undefined>> = {
  '/': () => {
    void queryClient.prefetchQuery({ ...analyticsOverviewOptions(30), staleTime: WARM_STALE_MS });
  },
  '/applications': () => {
    void queryClient.prefetchQuery({ ...applicationListOptions({}), staleTime: WARM_STALE_MS });
  },
  '/postings': () => {
    void queryClient.prefetchQuery({ ...postingListOptions({}), staleTime: WARM_STALE_MS });
  },
  '/reviews': () => {
    void queryClient.prefetchQuery({ ...reviewListOptions({}), staleTime: WARM_STALE_MS });
  },
  '/knowledge': () => {
    void queryClient.prefetchQuery({ ...knowledgeStatsOptions(), staleTime: WARM_STALE_MS });
  },
  '/resumes': () => {
    void queryClient.prefetchQuery({ ...resumeListOptions({}), staleTime: WARM_STALE_MS });
  },
  '/sessions': () => {
    void queryClient.prefetchQuery({ ...sessionListOptions({}), staleTime: WARM_STALE_MS });
  },
  '/analytics': () => {
    void queryClient.prefetchQuery({ ...analyticsOverviewOptions(30), staleTime: WARM_STALE_MS });
  },
  '/tracking': () => {
    void queryClient.prefetchQuery({ ...emailAccountsOptions(), staleTime: WARM_STALE_MS });
  },
  '/logs': undefined,
  '/settings': () => {
    void queryClient.prefetchQuery({ ...settingsOptions(), staleTime: WARM_STALE_MS });
  },
  '/onboarding': undefined,
};

/** The primary navigation rail. */
export function Sidebar() {
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const collapsed = useUiStore((state) => state.sidebarCollapsed);
  const toggleSidebar = useUiStore((state) => state.toggleSidebar);
  const reviewCount = useReviewCount();
  const timer = useRef<number | null>(null);

  const cancelWarm = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const warm = useCallback(
    (path: AppPath) => {
      const run = WARM[path];
      if (run === undefined || timer.current !== null) return;
      timer.current = window.setTimeout(() => {
        timer.current = null;
        run();
      }, INTENT_DELAY_MS);
    },
    [],
  );

  const open = useCallback(
    (path: AppPath) => {
      cancelWarm();
      void navigate({ to: path });
    },
    [cancelWarm, navigate],
  );

  const row = (destination: Destination) => {
    const isReviews = destination.path === '/reviews';
    const shortcut =
      destination.shortcutId === undefined
        ? undefined
        : SHORTCUTS_BY_ID.get(destination.shortcutId)?.combo;

    return (
      <SidebarItem
        key={destination.path}
        icon={destination.icon}
        label={destination.label}
        active={isActivePath(destination.path, pathname)}
        collapsed={collapsed}
        {...(isReviews && reviewCount > 0
          ? { count: reviewCount, countIsUrgent: true }
          : {})}
        {...(shortcut === undefined ? {} : { shortcut })}
        onMouseEnter={() => {
          warm(destination.path);
        }}
        onMouseLeave={cancelWarm}
        onFocus={() => {
          warm(destination.path);
        }}
        onBlur={cancelWarm}
        onClick={() => {
          open(destination.path);
        }}
      />
    );
  };

  return (
    <nav
      aria-label="Primary"
      className={cn(
        'flex shrink-0 flex-col gap-0.5 border-r border-subtle bg-chrome p-2',
        'transition-[width] duration-[140ms] ease-out-quad',
      )}
      style={{ width: collapsed ? 52 : 232 }}
    >
      {NAV_GROUPS.map((group) => {
        const rows = DESTINATIONS.filter((destination) => destination.group === group);
        if (rows.length === 0) return null;
        return (
          <div key={group} className="contents">
            {!collapsed && <SidebarGroupLabel>{group}</SidebarGroupLabel>}
            {collapsed && <span className="my-1 h-px bg-state-divider" aria-hidden="true" />}
            {rows.map(row)}
          </div>
        );
      })}

      <span className="flex-1" />

      {/* §12.5 — the one count worth interrupting a screen reader for. */}
      <span aria-live="polite" className="sr-only">
        {reviewCount === 0
          ? 'Nothing needs review'
          : `${String(reviewCount)} ${reviewCount === 1 ? 'item needs' : 'items need'} review`}
      </span>

      {row(SETTINGS_DESTINATION)}

      <SidebarItem
        icon={PanelLeft}
        label={collapsed ? 'Expand' : 'Collapse'}
        collapsed={collapsed}
        onClick={toggleSidebar}
      />
    </nav>
  );
}
