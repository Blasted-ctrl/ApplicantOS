/**
 * The frameless titlebar (`docs/UI.md` §5.2).
 *
 * 38px, `--bg-chrome`, the whole bar draggable and every interactive child `.no-drag` — the
 * single most common frameless-window bug is a button that drags the window instead of
 * clicking. The platform insets are reserved rather than guessed: macOS keeps 78px on the
 * left for the traffic lights, Windows and Linux keep 138px on the right for the native
 * overlay controls declared in `tauri.conf.json`.
 *
 * **The safety chip is the point of this component.** P7 — safety states are louder than
 * success states — so whenever `dry_run` is on or `auto_apply_enabled` is off, a
 * non-dismissible pill sits beside the wordmark saying so. It has no close affordance by
 * design: the one thing a user must never be able to do is hide the fact that the kill switch
 * is engaged, and the one thing they must never *wonder* is whether tonight's run will
 * actually submit anything.
 */

import { Moon, Monitor, Settings as SettingsIcon, Sun } from 'lucide-react';

import { Button, KbdCombo, StatusDot, Tooltip } from '@/components/ui';
import { useSafetyState } from '@/hooks';
import { useTheme } from '@/hooks/use-theme';
import { SHORTCUTS_BY_ID } from '@/lib/shortcuts';
import { isApplePlatform } from '@/lib/utils';
import { useUiStore } from '@/stores/ui';

/** Props for {@link Titlebar}. */
export interface TitlebarProps {
  /** Opens Settings → Safety. The safety chip and the gear both call it. */
  onOpenSettings: () => void;
}

/** Icon and label for each of the theme's three states. */
const THEME_LABELS = {
  dark: { Icon: Moon, label: 'Theme: dark' },
  light: { Icon: Sun, label: 'Theme: light' },
  system: { Icon: Monitor, label: 'Theme: follow the system' },
} as const;

/** The window's own chrome. */
export function Titlebar({ onOpenSettings }: TitlebarProps) {
  const safety = useSafetyState();
  const { theme, cycleTheme } = useTheme();
  const togglePalette = useUiStore((state) => state.togglePalette);
  const apple = isApplePlatform();
  const paletteCombo = SHORTCUTS_BY_ID.get('command.palette')?.combo;
  const { Icon: ThemeIcon, label: themeLabel } = THEME_LABELS[theme];

  return (
    <header
      className="app-drag relative z-30 flex h-[38px] shrink-0 items-center gap-3 border-b border-subtle bg-chrome pr-3"
      style={{
        paddingLeft: apple ? 92 : 12,
        paddingRight: apple ? 12 : 138,
      }}
    >
      <span className="select-none text-sm font-semibold tracking-[-0.01em] text-secondary">
        ApplicantOS
      </span>

      {safety.warning !== null && (
        <button
          type="button"
          onClick={onOpenSettings}
          className="no-drag inline-flex h-5 items-center gap-1.5 rounded-full border px-2 text-mini text-st-review"
          style={{
            backgroundColor: 'rgb(240 169 59 / 0.12)',
            borderColor: 'rgb(240 169 59 / 0.35)',
          }}
          aria-label={`${safety.warning}. Nothing will be submitted. Open safety settings.`}
        >
          <StatusDot status="needs_review" size="sm" aria-hidden="true" />
          {safety.warning}
        </button>
      )}

      <span className="flex-1" />

      {/* The binding is rendered from the keymap rather than written out, so the hint says
          `Ctrl K` on Windows and `⌘K` on macOS without a fork in this file. */}
      <Tooltip content="Command palette">
        <Button
          variant="ghost"
          size="sm"
          className="no-drag gap-1.5 text-muted"
          onClick={togglePalette}
          aria-label="Open the command palette"
        >
          {paletteCombo === undefined ? 'Search' : <KbdCombo combo={paletteCombo} />}
        </Button>
      </Tooltip>

      <Tooltip content="Settings">
        <Button
          variant="ghost"
          size="sm"
          icon
          className="no-drag"
          onClick={onOpenSettings}
          aria-label="Settings"
        >
          <SettingsIcon aria-hidden="true" />
        </Button>
      </Tooltip>

      <Tooltip content={themeLabel}>
        <Button
          variant="ghost"
          size="sm"
          icon
          className="no-drag"
          onClick={cycleTheme}
          aria-label={themeLabel}
        >
          <ThemeIcon aria-hidden="true" />
        </Button>
      </Tooltip>
    </header>
  );
}
