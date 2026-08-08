/**
 * Chrome state: theme, sidebar, command palette, detail pane, density.
 *
 * Everything here is *client* state — it never came from the server and never goes back to
 * it — which is exactly the line `docs/CONTRACTS.md` §18 draws: all server state goes through
 * TanStack Query, and a zustand store that cached a DTO would be a second, unsynchronised
 * copy of it. Nothing in this file is a DTO.
 *
 * The theme is the one piece with an external consumer. `src-tauri/src/store.rs` reads it out
 * of the Tauri store *before the window is shown*, to pick the window background colour and
 * avoid a white flash on launch (`docs/UI.md` §5.2). So a theme change writes three places:
 * the `data-theme` attribute (immediate), localStorage (so the next renderer boot is correct
 * before React runs), and the Tauri store (so the next *window* is correct before the
 * renderer runs). Dropping any one of them puts a white frame back.
 */

import { load } from '@tauri-apps/plugin-store';
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';

import { isTauri } from '@/lib/tauri';

/** Three real states, per `docs/UI.md` §2.3. `system` is a choice, not the absence of one. */
export type ThemeChoice = 'light' | 'dark' | 'system';

/** Table row density (`docs/UI.md` §4.2): 30px, 36px, 44px. */
export type Density = 'compact' | 'default' | 'comfortable';

/** Row height in pixels for each density. */
export const DENSITY_ROW_HEIGHT: Readonly<Record<Density, number>> = {
  compact: 30,
  default: 36,
  comfortable: 44,
};

/** Tauri store file the Rust shell reads window state and the theme from. */
const SHELL_STORE_FILE = 'window-state.json';

/** Key the shell reads the theme choice from. Its parser accepts `light` / `dark` / anything. */
const SHELL_THEME_KEY = 'theme';

interface UiState {
  theme: ThemeChoice;
  sidebarCollapsed: boolean;
  detailPaneOpen: boolean;
  density: Density;

  /** Transient: never persisted, because reopening a palette on launch is never wanted. */
  paletteOpen: boolean;
  cheatsheetOpen: boolean;
  /** The first key of a pending `g` chord, shown in the status bar while it waits. */
  pendingChord: string | null;

  setTheme: (theme: ThemeChoice) => void;
  /** Cycle dark → light → system. Bound to `⌘⇧L` and the titlebar toggle. */
  cycleTheme: () => void;
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleDetailPane: () => void;
  setDetailPaneOpen: (open: boolean) => void;
  setDensity: (density: Density) => void;
  setPaletteOpen: (open: boolean) => void;
  togglePalette: () => void;
  setCheatsheetOpen: (open: boolean) => void;
  setPendingChord: (key: string | null) => void;
}

/**
 * Write the theme where the DOM can see it.
 *
 * Absent the attribute the CSS follows `prefers-color-scheme`, which is what `system` means —
 * so `system` *removes* the attribute rather than resolving it to a value. Resolving it here
 * would freeze the choice at the moment it was made and stop tracking the OS.
 */
export function applyThemeAttribute(theme: ThemeChoice): void {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
}

/**
 * Mirror the choice into the Tauri store, best effort.
 *
 * Fire-and-forget on purpose: the renderer already applied the theme, and a store write that
 * failed costs one white frame on the *next* launch, not a broken app on this one.
 */
function syncThemeToShell(theme: ThemeChoice): void {
  if (!isTauri()) return;
  void (async () => {
    try {
      const store = await load(SHELL_STORE_FILE, { autoSave: true });
      await store.set(SHELL_THEME_KEY, theme);
      await store.save();
    } catch {
      /* The shell is absent or the store is unwritable. Neither is worth surfacing. */
    }
  })();
}

/** Chrome state. Theme, sidebar and density persist; every overlay flag does not. */
export const useUiStore = create<UiState>()(
  persist(
    (set, get) => ({
      theme: 'system',
      sidebarCollapsed: false,
      detailPaneOpen: true,
      density: 'default',
      paletteOpen: false,
      cheatsheetOpen: false,
      pendingChord: null,

      setTheme: (theme) => {
        applyThemeAttribute(theme);
        syncThemeToShell(theme);
        set({ theme });
      },

      cycleTheme: () => {
        const order: ThemeChoice[] = ['dark', 'light', 'system'];
        const current = order.indexOf(get().theme);
        const next = order[(current + 1) % order.length] ?? 'system';
        get().setTheme(next);
      },

      toggleSidebar: () => {
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed }));
      },
      setSidebarCollapsed: (sidebarCollapsed) => {
        set({ sidebarCollapsed });
      },
      toggleDetailPane: () => {
        set((s) => ({ detailPaneOpen: !s.detailPaneOpen }));
      },
      setDetailPaneOpen: (detailPaneOpen) => {
        set({ detailPaneOpen });
      },
      setDensity: (density) => {
        set({ density });
      },
      setPaletteOpen: (paletteOpen) => {
        set({ paletteOpen });
      },
      togglePalette: () => {
        set((s) => ({ paletteOpen: !s.paletteOpen }));
      },
      setCheatsheetOpen: (cheatsheetOpen) => {
        set({ cheatsheetOpen });
      },
      setPendingChord: (pendingChord) => {
        set({ pendingChord });
      },
    }),
    {
      name: 'aos-ui',
      storage: createJSONStorage(() => localStorage),
      version: 1,
      partialize: (state) => ({
        theme: state.theme,
        sidebarCollapsed: state.sidebarCollapsed,
        detailPaneOpen: state.detailPaneOpen,
        density: state.density,
      }),
      onRehydrateStorage: () => (state) => {
        // Runs synchronously with the localStorage backend, so the attribute is on the root
        // element before the first paint rather than one frame into it.
        if (state !== undefined) applyThemeAttribute(state.theme);
      },
    },
  ),
);

/** The row height the current density implies. */
export function useRowHeight(): number {
  return DENSITY_ROW_HEIGHT[useUiStore((s) => s.density)];
}
