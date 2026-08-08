/**
 * Kbd — `custom` (`docs/UI.md` §7.22).
 *
 * 18px tall, `--bg-inset`, `--border-default`, mono at 11px. Chords render as separate
 * adjacent caps with a 2px gap (`g` `r`); modifier combinations render with no separator
 * (`⌘` `K`), because one is two presses and the other is one.
 *
 * Platform glyphs come from `lib/shortcuts.ts`, which spells modifiers out on Windows and
 * Linux rather than showing `⌘` on a machine that has no such key.
 */

import { comboCaps, comboLabel, type Combo } from '@/lib/shortcuts';
import { cn } from '@/lib/utils';

/** One keycap. */
function Cap({ children }: { children: React.ReactNode }) {
  return (
    <kbd
      className={cn(
        'inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-xs px-1',
        'border border-default bg-inset font-mono text-micro font-normal tracking-normal text-secondary',
      )}
    >
      {children}
    </kbd>
  );
}

/** Props for {@link Kbd}. */
export interface KbdProps extends React.ComponentPropsWithoutRef<'span'> {
  /**
   * Literal caps to render, in order. Use this for one-off hints (`Esc`, `↵`).
   *
   * Prefer {@link KbdCombo} wherever a real binding exists — it renders the platform's own
   * glyphs and stays correct if the binding changes.
   */
  keys: readonly string[];
  /** Render as a chord: caps separated by a 2px gap rather than pressed together. */
  chord?: boolean;
}

/** A run of keycaps. */
export function Kbd({ keys, chord = false, className, ...props }: KbdProps) {
  return (
    <span
      className={cn('inline-flex items-center', chord ? 'gap-0.5' : 'gap-px', className)}
      {...props}
    >
      {keys.map((key, index) => (
        <Cap key={`${key}-${String(index)}`}>{key}</Cap>
      ))}
    </span>
  );
}

/** Props for {@link KbdCombo}. */
export interface KbdComboProps extends React.ComponentPropsWithoutRef<'span'> {
  combo: Combo;
}

/**
 * A binding from the keymap, rendered with the platform's glyphs.
 *
 * Carries its own `aria-label` from {@link comboLabel}, so a screen reader hears "Command K"
 * rather than the two glyph characters.
 */
export function KbdCombo({ combo, className, ...props }: KbdComboProps) {
  const steps = comboCaps(combo);
  return (
    <span
      className={cn('inline-flex items-center gap-0.5', className)}
      aria-label={comboLabel(combo)}
      {...props}
    >
      {steps.map((caps, stepIndex) => (
        <span key={`step-${String(stepIndex)}`} className="inline-flex items-center gap-px">
          {caps.map((cap, capIndex) => (
            <Cap key={`${cap}-${String(capIndex)}`}>{cap}</Cap>
          ))}
        </span>
      ))}
    </span>
  );
}
