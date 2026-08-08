/**
 * The announcer's imperative half (`docs/UI.md` §12.5).
 *
 * Split from the component for the reason `components/ui/variants.ts` is split from its
 * button: React Fast Refresh can only preserve state for a module whose exports are all
 * components, and `announce()` is a function. Keeping it here means editing the live region's
 * markup hot-reloads it instead of remounting the shell around it.
 *
 * There is exactly **one** live region in the app, and this is how everything reaches it. The
 * alternative — a component per `aria-live` node — is what turns a continuously-updating app
 * into a screen reader that never stops talking: forty rows changing status during a run would
 * each announce themselves, and the one announcement that mattered would be buried.
 */

/** How long a message stays in the region before it is cleared. */
const CLEAR_AFTER_MS = 1_000;

/** The live element, once mounted. */
let node: HTMLElement | null = null;

/** Pending clear, so two announcements in quick succession do not clear each other. */
let clearTimer: number | null = null;

/**
 * Bind the live region. Called by `<Announcer/>` on mount, and with `null` on unmount.
 *
 * @param element - The element whose `textContent` announcements are written to.
 */
export function registerAnnouncer(element: HTMLElement | null): void {
  node = element;
  if (element === null && clearTimer !== null) {
    window.clearTimeout(clearTimer);
    clearTimer = null;
  }
}

/**
 * Announce a message politely.
 *
 * Safe to call before the announcer has mounted and safe to call from outside React — a
 * dropped announcement is strictly better than a component that has to know whether the DOM
 * is ready before it can report a status change.
 *
 * @param message - A complete sentence in the user's terms: `Stripe application submitted`.
 */
export function announce(message: string): void {
  if (node === null || message === '') return;
  // Re-writing the same string does not re-announce, so the region is cleared first.
  node.textContent = '';
  node.textContent = message;
  if (clearTimer !== null) window.clearTimeout(clearTimer);
  clearTimer = window.setTimeout(() => {
    if (node !== null) node.textContent = '';
    clearTimer = null;
  }, CLEAR_AFTER_MS);
}
