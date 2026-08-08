/**
 * The keyboard layer (`docs/UI.md` §9), wired to the keymap in `lib/shortcuts.ts`.
 *
 * One listener per calling component, with a shared "already handled" mark so a list-scoped
 * binding and a global one cannot both fire for the same press. Deeper components mount
 * later, so their listener runs last — which is the wrong order for precedence, and is why
 * the mark exists: the first handler to claim an event wins, and callers that want to
 * override a global binding register it in their own map.
 *
 * Two behaviours are worth stating because they are easy to get subtly wrong:
 *
 * **Shortcuts are suspended inside text fields**, except the three §12.3 names: `Esc`, `⌘K`
 * and `⌘↵`. Without that, typing "same" into a search box would fire *status*, *apply*,
 * *note* and *edit* on the focused row.
 *
 * **`g` is a namespace, not a key.** Pressing it opens a 1200ms window and publishes the
 * pending state to the UI store, which the status bar renders as a `g …` indicator listing
 * the available second keys. `Esc` cancels; anything unrecognised cancels silently rather
 * than performing the second key's own binding, because a mistyped chord must never act.
 */

import { useEffect, useRef } from 'react';

import {
  CHORD_TIMEOUT_MS,
  findShortcut,
  NAVIGATION_CHORDS,
  type Shortcut,
} from '@/lib/shortcuts';
import { isTypingTarget } from '@/lib/utils';
import { useUiStore } from '@/stores/ui';

/** Handlers keyed by shortcut id. A missing id means the binding is inert in this scope. */
export type ShortcutHandlers = Readonly<
  Partial<Record<string, (event: KeyboardEvent, shortcut: Shortcut) => void>>
>;

/** Options for {@link useShortcuts}. */
export interface UseShortcutsOptions {
  /** Set false to suspend this scope — a modal that owns the keyboard, for instance. */
  enabled?: boolean;
  /**
   * Whether this scope participates in the `g` navigation namespace.
   *
   * Exactly one scope should: the app shell. A second would open two chord windows for one
   * press and consume the second key twice.
   */
  chords?: boolean;
}

/**
 * Events already claimed by a handler.
 *
 * A `WeakSet` rather than a flag on the event, because the event object is not ours to
 * decorate and a `stopPropagation` would break Radix's own key handling.
 */
const claimed = new WeakSet<KeyboardEvent>();

/**
 * Bind a scope's shortcuts.
 *
 * @param handlers - Keyed by the ids in `lib/shortcuts.ts`. Referential stability is not
 *   required; the map is read through a ref so a fresh object every render costs nothing.
 */
export function useShortcuts(
  handlers: ShortcutHandlers,
  options: UseShortcutsOptions = {},
): void {
  const { enabled = true, chords = false } = options;
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  const setPendingChord = useUiStore((s) => s.setPendingChord);

  useEffect(() => {
    if (!enabled) return;

    let chordKey: string | null = null;
    let chordTimer: number | null = null;

    const clearChord = (): void => {
      if (chordTimer !== null) {
        window.clearTimeout(chordTimer);
        chordTimer = null;
      }
      if (chordKey !== null) {
        chordKey = null;
        setPendingChord(null);
      }
    };

    const onKeyDown = (event: KeyboardEvent): void => {
      if (claimed.has(event)) return;
      // A keypress that is completing an IME composition is not a shortcut.
      if (event.isComposing) return;

      const map = handlersRef.current;

      // ── Second half of a pending chord ──────────────────────────────────────────
      if (chords && chordKey !== null) {
        const pressed = event.key;
        clearChord();
        if (pressed === 'Escape') {
          claimed.add(event);
          event.preventDefault();
          return;
        }
        const target = NAVIGATION_CHORDS.get(pressed.toLowerCase());
        if (target !== undefined) {
          const handler = map[target.id];
          if (handler !== undefined) {
            claimed.add(event);
            event.preventDefault();
            handler(event, target);
          }
        }
        // An unrecognised second key cancels and does nothing. Falling through to the
        // single-key table here would make `g` then `x` archive the focused row.
        return;
      }

      const typing = isTypingTarget(event.target);

      // ── Opening a chord ─────────────────────────────────────────────────────────
      if (
        chords &&
        !typing &&
        event.key.toLowerCase() === 'g' &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey &&
        !event.shiftKey
      ) {
        claimed.add(event);
        event.preventDefault();
        chordKey = 'g';
        setPendingChord('g');
        chordTimer = window.setTimeout(clearChord, CHORD_TIMEOUT_MS);
        return;
      }

      // ── Single keystroke ────────────────────────────────────────────────────────
      const shortcut = findShortcut(
        event,
        (candidate) =>
          map[candidate.id] !== undefined && (!typing || candidate.allowInInput === true),
      );
      if (shortcut === undefined) return;

      const handler = map[shortcut.id];
      if (handler === undefined) return;

      claimed.add(event);
      event.preventDefault();
      handler(event, shortcut);
    };

    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      clearChord();
    };
  }, [enabled, chords, setPendingChord]);
}

/**
 * Roving-tabindex list navigation (`docs/UI.md` §9.4).
 *
 * Returned as a handler map rather than a bound listener so a table can compose it with its
 * own row actions in one {@link useShortcuts} call. Arrow keys never scroll the page — they
 * move focus, and the caller scrolls the focused row into view with `block: 'nearest'`.
 *
 * @param count - Number of rows currently in the list.
 * @param index - The focused row index, or `-1` for none.
 * @param setIndex - Moves focus. The caller is responsible for scrolling it into view.
 * @param pageSize - Rows moved by `⌥↓` / `⌥↑`.
 */
export function listNavigationHandlers(
  count: number,
  index: number,
  setIndex: (next: number) => void,
  pageSize = 10,
): ShortcutHandlers {
  const move = (delta: number): void => {
    if (count === 0) return;
    // From "nothing focused", a downward move lands on the first row rather than the
    // second — `-1 + 1` clamps to 0, which is the behaviour the clamp already gives.
    setIndex(Math.min(Math.max(index + delta, 0), count - 1));
  };

  return {
    'list.next': () => {
      move(1);
    },
    'list.prev': () => {
      move(-1);
    },
    'list.pageDown': () => {
      move(pageSize);
    },
    'list.pageUp': () => {
      move(-pageSize);
    },
    'list.first': () => {
      if (count > 0) setIndex(0);
    },
    'list.last': () => {
      if (count > 0) setIndex(count - 1);
    },
  };
}
