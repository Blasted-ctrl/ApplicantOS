/**
 * The application shell (`docs/UI.md` §5.1).
 *
 * **The shell never unmounts.** Titlebar, sidebar, status bar, command palette, cheatsheet and
 * every global dialog live here, in the root route; only `<Outlet/>` swaps. That single fact
 * is the largest contributor to "navigation feels instant" — there is no page transition to
 * animate because there is no page to transition, just a content region whose contents change
 * between one frame and the next (§6.6).
 *
 * It is also where the keyboard lives. Exactly one scope owns the `g` navigation namespace
 * (§9.3): a second would open two chord windows for one press and consume the second key
 * twice. The same handler map is what the native menu dispatches into, so a menu item and its
 * accelerator run one function rather than two implementations that drift.
 *
 * Onboarding is the one route that takes the window over: titlebar only, no sidebar, no status
 * bar. It is a first-run flow, and the chrome it would otherwise sit inside is full of
 * destinations that have nothing in them yet.
 */

import { Outlet, useNavigate, useRouterState } from '@tanstack/react-router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { Announcer } from '@/components/announcer';
import { appActionsContext, type AppActions } from '@/components/app-actions';
import { BackendBanner } from '@/components/backend-state';
import { CommandMenu } from '@/components/command-menu';
import { DiscoverDialog, StartRunDialog, StopRunDialog } from '@/components/run-controls';
import { ShortcutsDialog } from '@/components/shortcuts-dialog';
import { Sidebar } from '@/components/sidebar';
import { StatusBar } from '@/components/status-bar';
import { Titlebar } from '@/components/titlebar';
import { ConfirmDialog } from '@/components/ui';
import {
  useBackendStatus,
  useDiscoverPostings,
  useOnboardingStatus,
  useReadySignal,
  useSafetyState,
  useStartSession,
  useStopSession,
} from '@/hooks';
import { useMenuActions } from '@/hooks/use-backend';
import { useShortcuts } from '@/hooks/use-shortcuts';
import { useTheme } from '@/hooks/use-theme';
import { useActiveSession } from '@/hooks/use-sessions';
import { resetCache } from '@/lib/query/persist';
import { appDataDir, openPath, restartBackend } from '@/lib/tauri';
import { reconnectEvents } from '@/lib/query/ws';
import { resetBaseUrl } from '@/lib/tauri';
import { useUiStore } from '@/stores/ui';

/** Focus the current screen's search field, if it has one (`/`). */
function focusPageSearch(): void {
  const input = document.querySelector<HTMLInputElement>('[data-page-search]');
  input?.focus();
  input?.select();
}

/** The persistent shell. Rendered by the root route; every screen is its `<Outlet/>`. */
export function AppShell() {
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const onboarding = pathname.startsWith('/onboarding');

  const toggleSidebar = useUiStore((state) => state.toggleSidebar);
  const toggleDetailPane = useUiStore((state) => state.toggleDetailPane);
  const togglePalette = useUiStore((state) => state.togglePalette);
  const setPaletteOpen = useUiStore((state) => state.setPaletteOpen);
  const cheatsheetOpen = useUiStore((state) => state.cheatsheetOpen);
  const setCheatsheetOpen = useUiStore((state) => state.setCheatsheetOpen);
  const { cycleTheme } = useTheme();

  const [startOpen, setStartOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [discoverOpen, setDiscoverOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);

  const safety = useSafetyState();
  const activeSession = useActiveSession();
  const startSession = useStartSession();
  const stopSession = useStopSession();
  const discoverPostings = useDiscoverPostings();
  const onboardingStatus = useOnboardingStatus();

  useBackendStatus();
  useReadySignal();

  // First run lands in the wizard exactly once. A ref rather than a dependency on the
  // pathname, because a user who deliberately navigates out of onboarding must not be dragged
  // back into it on the next render.
  const redirected = useRef(false);
  useEffect(() => {
    if (redirected.current) return;
    if (onboardingStatus.data?.complete === false && pathname === '/') {
      redirected.current = true;
      void navigate({ to: '/onboarding' });
    }
  }, [navigate, onboardingStatus.data, pathname]);

  const actions = useMemo<AppActions>(
    () => ({
      startRun: () => {
        setStartOpen(true);
      },
      stopRun: () => {
        if (activeSession !== null) setStopOpen(true);
      },
      discover: () => {
        setDiscoverOpen(true);
      },
      resetCache: () => {
        setResetOpen(true);
      },
      showShortcuts: () => {
        setCheatsheetOpen(true);
      },
    }),
    [activeSession, setCheatsheetOpen],
  );

  const openSettings = useCallback(() => {
    void navigate({ to: '/settings' });
  }, [navigate]);

  useShortcuts(
    {
      'command.palette': togglePalette,
      'search.focus': focusPageSearch,
      'sidebar.toggle': toggleSidebar,
      'detail.toggle': toggleDetailPane,
      'theme.toggle': cycleTheme,
      'nav.back': () => {
        window.history.back();
      },
      'nav.forward': () => {
        window.history.forward();
      },
      'escape.home': () => {
        void navigate({ to: '/' });
      },
      'help.shortcuts': () => {
        setCheatsheetOpen(true);
      },
      'settings.open': openSettings,
      'postings.discover': actions.discover,
      'session.start': actions.startRun,
      'session.stop': actions.stopRun,
      'go.dashboard': () => {
        void navigate({ to: '/' });
      },
      'go.applications': () => {
        void navigate({ to: '/applications' });
      },
      'go.postings': () => {
        void navigate({ to: '/postings' });
      },
      'go.reviews': () => {
        void navigate({ to: '/reviews' });
      },
      'go.knowledge': () => {
        void navigate({ to: '/knowledge' });
      },
      'go.resumes': () => {
        void navigate({ to: '/resumes' });
      },
      'go.sessions': () => {
        void navigate({ to: '/sessions' });
      },
      'go.analytics': () => {
        void navigate({ to: '/analytics' });
      },
      'go.logs': () => {
        void navigate({ to: '/logs' });
      },
      'go.settings': openSettings,
    },
    { chords: true },
  );

  const menuHandlers = useMemo<Readonly<Record<string, () => void>>>(
    () => ({
      settings: openSettings,
      'postings.discover': actions.discover,
      'session.start': actions.startRun,
      'session.stop': actions.stopRun,
      'command.palette': () => {
        setPaletteOpen(true);
      },
      'sidebar.toggle': toggleSidebar,
      'detail.toggle': toggleDetailPane,
      'theme.toggle': cycleTheme,
      'nav.back': () => {
        window.history.back();
      },
      'nav.forward': () => {
        window.history.forward();
      },
      'app.reload': () => {
        window.location.reload();
      },
      'cache.reset': actions.resetCache,
      'help.shortcuts': () => {
        setCheatsheetOpen(true);
      },
      'help.safety': openSettings,
      'data.open': () => {
        void appDataDir().then((path) => openPath(path));
      },
      'backend.restart': () => {
        void restartBackend().then(() => {
          resetBaseUrl();
          reconnectEvents();
        });
      },
    }),
    [
      actions,
      cycleTheme,
      openSettings,
      setCheatsheetOpen,
      setPaletteOpen,
      toggleDetailPane,
      toggleSidebar,
    ],
  );

  useMenuActions(menuHandlers);

  return (
    <appActionsContext.Provider value={actions}>
      <div className="flex h-full flex-col bg-chrome">
        <a href="#page-content" className="skip-link">
          Skip to content
        </a>

        <Titlebar onOpenSettings={openSettings} />
        <BackendBanner />

        <div className="flex min-h-0 flex-1">
          {!onboarding && <Sidebar />}
          <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-page">
            <Outlet />
          </main>
        </div>

        {!onboarding && <StatusBar />}

        <Announcer />

        <CommandMenu
          onStartRun={actions.startRun}
          onStopRun={actions.stopRun}
          onDiscover={actions.discover}
          onResetCache={actions.resetCache}
          onShowShortcuts={actions.showShortcuts}
        />

        <ShortcutsDialog open={cheatsheetOpen} onOpenChange={setCheatsheetOpen} />

        <StartRunDialog
          open={startOpen}
          onOpenChange={setStartOpen}
          autoApplyEnabled={safety.autoApplyEnabled}
          dryRun={safety.dryRun}
          starting={startSession.isPending}
          onStart={(request) => {
            startSession.mutate(request, {
              onSuccess: () => {
                setStartOpen(false);
              },
            });
          }}
        />

        <StopRunDialog
          open={stopOpen}
          onOpenChange={setStopOpen}
          session={activeSession}
          stopping={stopSession.isPending}
          onStop={() => {
            if (activeSession !== null) {
              stopSession.mutate(activeSession.id, {
                onSuccess: () => {
                  setStopOpen(false);
                },
              });
            }
          }}
        />

        <DiscoverDialog
          open={discoverOpen}
          onOpenChange={setDiscoverOpen}
          discovering={discoverPostings.isPending}
          onDiscover={(request) => {
            discoverPostings.mutate(request, {
              onSuccess: () => {
                setDiscoverOpen(false);
              },
            });
          }}
        />

        <ConfirmDialog
          open={resetOpen}
          onOpenChange={setResetOpen}
          title="Reset the local cache?"
          description="Every persisted query is dropped and refetched from the backend. Nothing on the server changes, and no application is affected — this costs one slow launch."
          confirmLabel="Reset the Cache"
          onConfirm={() => {
            setResetOpen(false);
            void resetCache();
          }}
        />
      </div>
    </appActionsContext.Provider>
  );
}
