/**
 * The keymap (`docs/UI.md` §9), as data.
 *
 * Three principles, taken from Linear and Raycast because they are cross-product convention
 * now and deviating costs the user's existing muscle memory for nothing:
 *
 * 1. **Unmodified single letters act on the focused row**, mnemonically matched to the
 *    property they change. A useful side effect: macOS and Windows bindings are byte-identical
 *    — no `⌘`/`Ctrl` fork in the code and none in the documentation.
 * 2. **`g` is a navigation namespace.** One letter reserved for all navigation frees the
 *    other twenty-five for actions.
 * 3. **Modifiers encode priority, and `Ctrl` means irreversible.** `↵` primary · `⌘↵`
 *    secondary · `⌘⇧↵` tertiary · `Ctrl+X` destructive. A whole modifier meaning "this cannot
 *    be undone" is cheap insurance against a mis-hit.
 *
 * The list is exported as data rather than wired up inline because §7.18 makes it a product
 * requirement: the command palette shows each result's own shortcut inline, and `?` opens a
 * searchable cheatsheet listing every binding grouped exactly as this file groups them. Both
 * read this array, so a binding cannot exist without being discoverable — which is the rule
 * "no action may exist only as a shortcut", enforced structurally.
 */

import { isApplePlatform } from '@/lib/utils';

/** How long a pending `g` chord waits for its second key. */
export const CHORD_TIMEOUT_MS = 1200;

/** Groups, in the order the cheatsheet renders them. */
export const SHORTCUT_GROUPS = [
  'Global',
  'Navigation',
  'List',
  'Row actions',
  'Review queue',
  'Forms',
] as const;
export type ShortcutGroup = (typeof SHORTCUT_GROUPS)[number];

/** A single keystroke, possibly modified. */
export interface KeyCombo {
  /** `event.key`, matched case-insensitively. Named keys use their DOM spelling. */
  key: string;
  /** The platform command modifier: `⌘` on Apple, `Ctrl` elsewhere. */
  mod?: boolean;
  /** Literal `Ctrl` on every platform. Reserved for destructive actions. */
  ctrl?: boolean;
  shift?: boolean;
  alt?: boolean;
}

/** A two-key chord in the `g` navigation namespace. */
export interface ChordCombo {
  chord: [string, string];
}

/** Either form of binding. */
export type Combo = KeyCombo | ChordCombo;

/** Whether a combo is a chord. */
export function isChord(combo: Combo): combo is ChordCombo {
  return 'chord' in combo;
}

/** One binding. */
export interface Shortcut {
  /** Stable id. What `useShortcuts` dispatches and what the palette looks actions up by. */
  id: string;
  group: ShortcutGroup;
  /** Sentence-case description, as it appears in the cheatsheet and the palette. */
  description: string;
  combo: Combo;
  /** Route this binding navigates to, for the `g` namespace. */
  path?: string;
  /** True for irreversible actions, which must confirm before running. */
  destructive?: boolean;
  /**
   * Whether the binding still fires while a text field has focus.
   *
   * §12.3: shortcuts are suspended inside inputs except `Esc`, `⌘K`, and `⌘↵`.
   */
  allowInInput?: boolean;
}

/** Every binding in the product. */
export const SHORTCUTS: readonly Shortcut[] = [
  // ── Global (§9.2) ─────────────────────────────────────────────────────────────────
  {
    id: 'command.palette',
    group: 'Global',
    description: 'Open the command palette',
    combo: { key: 'k', mod: true },
    allowInInput: true,
  },
  {
    id: 'search.focus',
    group: 'Global',
    description: "Focus the page's search field",
    combo: { key: '/' },
  },
  {
    id: 'sidebar.toggle',
    group: 'Global',
    description: 'Toggle the sidebar',
    combo: { key: '.', mod: true },
  },
  {
    id: 'nav.back',
    group: 'Global',
    description: 'Back',
    combo: { key: '[', mod: true },
  },
  {
    id: 'nav.forward',
    group: 'Global',
    description: 'Forward',
    combo: { key: ']', mod: true },
  },
  {
    id: 'escape.pop',
    group: 'Global',
    description: 'Close the topmost layer, then clear the search, then deselect',
    combo: { key: 'Escape' },
    allowInInput: true,
  },
  {
    id: 'escape.home',
    group: 'Global',
    description: 'Return to the Dashboard',
    combo: { key: 'Escape', shift: true },
    allowInInput: true,
  },
  {
    id: 'help.shortcuts',
    group: 'Global',
    description: 'Keyboard cheatsheet',
    combo: { key: '?' },
  },
  {
    id: 'settings.open',
    group: 'Global',
    description: 'Settings',
    combo: { key: ',', mod: true },
  },
  {
    id: 'postings.discover',
    group: 'Global',
    description: 'Discover postings',
    combo: { key: 'd', mod: true, shift: true },
  },
  {
    id: 'session.start',
    group: 'Global',
    description: 'Start a run',
    combo: { key: 'r', mod: true, shift: true },
  },
  {
    id: 'session.stop',
    group: 'Global',
    description: 'Stop the running session',
    combo: { key: 's', ctrl: true },
    destructive: true,
  },
  {
    id: 'detail.toggle',
    group: 'Global',
    description: 'Toggle the detail pane',
    combo: { key: '\\', mod: true },
  },
  {
    id: 'theme.toggle',
    group: 'Global',
    description: 'Toggle the theme',
    combo: { key: 'l', mod: true, shift: true },
  },

  // ── Navigation — `g` then key (§9.3) ──────────────────────────────────────────────
  {
    id: 'go.dashboard',
    group: 'Navigation',
    description: 'Dashboard',
    combo: { chord: ['g', 'd'] },
    path: '/',
  },
  {
    id: 'go.applications',
    group: 'Navigation',
    description: 'Applications',
    combo: { chord: ['g', 'a'] },
    path: '/applications',
  },
  {
    id: 'go.postings',
    group: 'Navigation',
    description: 'Postings',
    combo: { chord: ['g', 'p'] },
    path: '/postings',
  },
  {
    id: 'go.reviews',
    group: 'Navigation',
    description: 'Review queue',
    combo: { chord: ['g', 'r'] },
    path: '/reviews',
  },
  {
    id: 'go.knowledge',
    group: 'Navigation',
    description: 'Knowledge',
    combo: { chord: ['g', 'k'] },
    path: '/knowledge',
  },
  {
    id: 'go.resumes',
    group: 'Navigation',
    description: 'Résumés',
    combo: { chord: ['g', 's'] },
    path: '/resumes',
  },
  {
    id: 'go.sessions',
    group: 'Navigation',
    description: 'Runs',
    combo: { chord: ['g', 'n'] },
    path: '/sessions',
  },
  {
    id: 'go.analytics',
    group: 'Navigation',
    description: 'Analytics',
    combo: { chord: ['g', 'i'] },
    path: '/analytics',
  },
  {
    id: 'go.logs',
    group: 'Navigation',
    description: 'Logs',
    combo: { chord: ['g', 'l'] },
    path: '/logs',
  },
  {
    id: 'go.settings',
    group: 'Navigation',
    description: 'Settings',
    combo: { chord: ['g', ','] },
    path: '/settings',
  },

  // ── List / table (§9.4) ───────────────────────────────────────────────────────────
  { id: 'list.next', group: 'List', description: 'Next row', combo: { key: 'j' } },
  { id: 'list.prev', group: 'List', description: 'Previous row', combo: { key: 'k' } },
  {
    id: 'list.nextSection',
    group: 'List',
    description: 'Next status group',
    combo: { key: 'ArrowDown', mod: true },
  },
  {
    id: 'list.prevSection',
    group: 'List',
    description: 'Previous status group',
    combo: { key: 'ArrowUp', mod: true },
  },
  {
    id: 'list.pageDown',
    group: 'List',
    description: 'Page down',
    combo: { key: 'ArrowDown', alt: true },
  },
  {
    id: 'list.pageUp',
    group: 'List',
    description: 'Page up',
    combo: { key: 'ArrowUp', alt: true },
  },
  { id: 'list.first', group: 'List', description: 'First row', combo: { key: 'Home' } },
  { id: 'list.last', group: 'List', description: 'Last row', combo: { key: 'End' } },
  {
    id: 'list.open',
    group: 'List',
    description: 'Open the focused row',
    combo: { key: 'Enter' },
  },
  {
    id: 'list.openRoute',
    group: 'List',
    description: 'Open in the full detail route',
    combo: { key: 'Enter', mod: true },
    allowInInput: true,
  },
  {
    id: 'list.peek',
    group: 'List',
    description: 'Quick-look preview',
    combo: { key: ' ' },
  },
  {
    id: 'list.toggleSelect',
    group: 'List',
    description: 'Toggle row selection',
    combo: { key: 'x' },
  },
  {
    id: 'list.extendDown',
    group: 'List',
    description: 'Extend the selection down',
    combo: { key: 'j', shift: true },
  },
  {
    id: 'list.extendUp',
    group: 'List',
    description: 'Extend the selection up',
    combo: { key: 'k', shift: true },
  },
  {
    id: 'list.selectAll',
    group: 'List',
    description: 'Select every visible row',
    combo: { key: 'a', mod: true },
  },

  // ── Row actions (§9.5) ────────────────────────────────────────────────────────────
  {
    id: 'row.status',
    group: 'Row actions',
    description: 'Change status',
    combo: { key: 's' },
  },
  { id: 'row.note', group: 'Row actions', description: 'Add a note', combo: { key: 'n' } },
  { id: 'row.retry', group: 'Row actions', description: 'Retry', combo: { key: 'r' } },
  { id: 'row.apply', group: 'Row actions', description: 'Apply now', combo: { key: 'a' } },
  {
    id: 'row.artifacts',
    group: 'Row actions',
    description: 'View artifacts',
    combo: { key: 'v' },
  },
  {
    id: 'row.copyUrl',
    group: 'Row actions',
    description: 'Copy the posting URL',
    combo: { key: 'c' },
  },
  {
    id: 'row.openExternal',
    group: 'Row actions',
    description: 'Open the posting in the system browser',
    combo: { key: 'o' },
  },
  { id: 'row.pin', group: 'Row actions', description: 'Pin', combo: { key: 'f' } },
  { id: 'row.edit', group: 'Row actions', description: 'Edit', combo: { key: 'e' } },
  {
    id: 'row.archive',
    group: 'Row actions',
    description: 'Archive — always confirms',
    combo: { key: 'x', ctrl: true },
    destructive: true,
  },
  {
    id: 'row.archiveSelected',
    group: 'Row actions',
    description: 'Archive every selected row — always confirms',
    combo: { key: 'x', ctrl: true, shift: true },
    destructive: true,
  },

  // ── Review queue (§9.5) ───────────────────────────────────────────────────────────
  {
    id: 'review.expand',
    group: 'Review queue',
    description: 'Expand the review',
    combo: { key: 'Enter' },
  },
  {
    id: 'review.approve',
    group: 'Review queue',
    description: 'Approve and submit',
    combo: { key: 'Enter', mod: true },
    allowInInput: true,
  },
  {
    id: 'review.dismiss',
    group: 'Review queue',
    description: 'Dismiss — always confirms',
    combo: { key: 'x', ctrl: true },
    destructive: true,
  },
  {
    id: 'review.draft',
    group: 'Review queue',
    description: 'Save a draft answer',
    combo: { key: 'd' },
  },

  // ── Forms and dialogs (§9.6) ──────────────────────────────────────────────────────
  {
    id: 'form.submit',
    group: 'Forms',
    description: 'Submit, from anywhere in the form',
    combo: { key: 'Enter', mod: true },
    allowInInput: true,
  },
  {
    id: 'form.cancel',
    group: 'Forms',
    description: 'Cancel — confirms first when the form is dirty',
    combo: { key: 'Escape' },
    allowInInput: true,
  },
];

/** Bindings indexed by id, for the palette and the cheatsheet. */
export const SHORTCUTS_BY_ID: ReadonlyMap<string, Shortcut> = new Map(
  SHORTCUTS.map((shortcut) => [shortcut.id, shortcut]),
);

/** Every `g`-chord binding, keyed by its second key. */
export const NAVIGATION_CHORDS: ReadonlyMap<string, Shortcut> = new Map(
  SHORTCUTS.filter((shortcut) => isChord(shortcut.combo)).map((shortcut) => [
    (shortcut.combo as ChordCombo).chord[1],
    shortcut,
  ]),
);

/** Whether the platform modifier is held. `⌘` on Apple, `Ctrl` elsewhere. */
export function hasMod(event: KeyboardEvent): boolean {
  return isApplePlatform() ? event.metaKey : event.ctrlKey;
}

/**
 * Whether a keyboard event matches a single-keystroke combo.
 *
 * Modifier matching is exact in both directions: a combo without `shift` does not match a
 * shifted press. Without that, `⌘⇧R` (start a run) would also fire on `⌘R`, and the two mean
 * very different things.
 */
export function matchesCombo(event: KeyboardEvent, combo: KeyCombo): boolean {
  if (event.key.toLowerCase() !== combo.key.toLowerCase()) return false;

  const apple = isApplePlatform();
  const modHeld = apple ? event.metaKey : event.ctrlKey;
  const wantMod = combo.mod === true;
  if (modHeld !== wantMod) return false;

  // On Apple, `mod` is ⌘ and `ctrl` is a separate physical key; elsewhere they are the same
  // key, so a combo asking for both is unreachable and must not be declared.
  const wantCtrl = combo.ctrl === true;
  if (apple) {
    if (event.ctrlKey !== wantCtrl) return false;
  } else if (!wantMod && event.ctrlKey !== wantCtrl) {
    return false;
  }

  // Shift is compared for named keys and letters, but ignored for a punctuation key whose
  // character *is* the shifted form. `?` is `Shift+/` on almost every layout, so requiring
  // `shift: false` there would make the cheatsheet unreachable — while `⇧J` genuinely does
  // need the comparison, because `j` and `J` are two different bindings.
  const shiftIsImplicit =
    combo.shift === undefined && combo.key.length === 1 && !/[a-z0-9]/i.test(combo.key);
  if (!shiftIsImplicit && event.shiftKey !== (combo.shift === true)) return false;

  if (event.altKey !== (combo.alt === true)) return false;
  return true;
}

/** Find the binding an event matches, if any. Chords are resolved by `useShortcuts`. */
export function findShortcut(
  event: KeyboardEvent,
  scope?: (shortcut: Shortcut) => boolean,
): Shortcut | undefined {
  return SHORTCUTS.find(
    (shortcut) =>
      !isChord(shortcut.combo) &&
      matchesCombo(event, shortcut.combo) &&
      (scope === undefined || scope(shortcut)),
  );
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Display — docs/UI.md §7.22
// ══════════════════════════════════════════════════════════════════════════════════════

/** Named keys rendered as a glyph rather than their DOM spelling. */
const KEY_GLYPHS: Readonly<Record<string, { apple: string; other: string }>> = {
  Enter: { apple: '↵', other: 'Enter' },
  Escape: { apple: '⎋', other: 'Esc' },
  Backspace: { apple: '⌫', other: 'Backspace' },
  ArrowUp: { apple: '↑', other: '↑' },
  ArrowDown: { apple: '↓', other: '↓' },
  ArrowLeft: { apple: '←', other: '←' },
  ArrowRight: { apple: '→', other: '→' },
  ' ': { apple: 'Space', other: 'Space' },
  Home: { apple: 'Home', other: 'Home' },
  End: { apple: 'End', other: 'End' },
};

/**
 * Render a combo as groups of keycaps.
 *
 * The outer array is chord *steps*: `[['g'], ['r']]` renders as two caps with a 2px gap,
 * because `g` then `r` is two presses. A modified keystroke is one step with several caps and
 * no separator, because `⌘` and `K` are pressed together.
 *
 * Platform glyphs: `⌘⇧⌥⌃↵⌫` on macOS; the words spelled out on Windows and Linux, which is
 * what those platforms' own menus do.
 */
export function comboCaps(combo: Combo, apple = isApplePlatform()): string[][] {
  if (isChord(combo)) {
    return combo.chord.map((key) => [renderKey(key, apple)]);
  }
  const caps: string[] = [];
  if (combo.ctrl === true) caps.push(apple ? '⌃' : 'Ctrl');
  if (combo.mod === true) caps.push(apple ? '⌘' : 'Ctrl');
  if (combo.alt === true) caps.push(apple ? '⌥' : 'Alt');
  if (combo.shift === true) caps.push(apple ? '⇧' : 'Shift');
  caps.push(renderKey(combo.key, apple));
  return [caps];
}

function renderKey(key: string, apple: boolean): string {
  const glyph = KEY_GLYPHS[key];
  if (glyph !== undefined) return apple ? glyph.apple : glyph.other;
  return key.length === 1 ? key.toUpperCase() : key;
}

/** A combo as a single flat string, for an `aria-keyshortcuts` attribute or a tooltip. */
export function comboLabel(combo: Combo, apple = isApplePlatform()): string {
  return comboCaps(combo, apple)
    .map((step) => step.join(apple ? '' : '+'))
    .join(' then ');
}
