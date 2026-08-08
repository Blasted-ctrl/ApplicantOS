/**
 * Theme selection and the resolved value.
 *
 * The choice is one of three states (`docs/UI.md` §2.3); the *resolved* theme is what the
 * pixels actually are, and the two differ whenever the choice is `system`. Components that
 * need to know "is this dark right now" — a chart that picks a series palette, an image with
 * a baked-in background — need the resolved value, and getting it by reading the store would
 * be wrong on every machine set to follow the OS.
 */

import { useEffect, useState } from 'react';

import { applyThemeAttribute, useUiStore, type ThemeChoice } from '@/stores/ui';

/** What the pixels are, as opposed to what the user asked for. */
export type ResolvedTheme = 'light' | 'dark';

const MEDIA_QUERY = '(prefers-color-scheme: light)';

function systemTheme(): ResolvedTheme {
  if (typeof window === 'undefined') return 'dark';
  return window.matchMedia(MEDIA_QUERY).matches ? 'light' : 'dark';
}

/** Theme state: the stored choice, the resolved value, and the two ways to change it. */
export interface UseThemeResult {
  /** What the user chose. `system` is a real choice, not the absence of one. */
  theme: ThemeChoice;
  /** What is actually rendering. */
  resolved: ResolvedTheme;
  setTheme: (theme: ThemeChoice) => void;
  /** Cycle dark → light → system. Bound to `⌘⇧L` and the titlebar toggle. */
  cycleTheme: () => void;
}

/**
 * Read and change the theme.
 *
 * The OS listener stays subscribed even when the choice is explicit: a user who switches
 * from `dark` back to `system` must land on the current OS value immediately, not on the one
 * that was current when the component mounted.
 */
export function useTheme(): UseThemeResult {
  const theme = useUiStore((s) => s.theme);
  const setTheme = useUiStore((s) => s.setTheme);
  const cycleTheme = useUiStore((s) => s.cycleTheme);
  const [system, setSystem] = useState<ResolvedTheme>(systemTheme);

  useEffect(() => {
    const media = window.matchMedia(MEDIA_QUERY);
    const onChange = (event: MediaQueryListEvent): void => {
      setSystem(event.matches ? 'light' : 'dark');
    };
    media.addEventListener('change', onChange);
    return () => {
      media.removeEventListener('change', onChange);
    };
  }, []);

  // The store's rehydration already wrote the attribute; this keeps it correct across a Fast
  // Refresh, which replays module state without replaying the rehydration callback.
  useEffect(() => {
    applyThemeAttribute(theme);
  }, [theme]);

  return {
    theme,
    resolved: theme === 'system' ? system : theme,
    setTheme,
    cycleTheme,
  };
}
