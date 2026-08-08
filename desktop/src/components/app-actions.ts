/**
 * The shell's actions, made reachable from any screen.
 *
 * `Start a run`, `Discover postings`, `Stop the run` and `Reset the local cache` exist in three
 * places at once: a keyboard binding, a command-palette row, and a button on the screen that
 * cares about them. All three must open the *same* dialog with the same confirmation, or the
 * safety envelope has three implementations and only one of them was reviewed.
 *
 * The dialogs therefore live in the shell — which never unmounts, so a dialog cannot be
 * dismissed by a navigation — and screens reach them through this context. Route components
 * take no props (the router constructs them), which is the other reason this is a context
 * rather than a prop drill.
 */

import { createContext, useContext } from 'react';

/** Everything the shell can be asked to do from inside a screen. */
export interface AppActions {
  /** Open the start-a-run dialog. */
  startRun: () => void;
  /** Open the stop confirmation. No-op when nothing is running. */
  stopRun: () => void;
  /** Open the discovery dialog. */
  discover: () => void;
  /** Open the destructive cache-reset confirmation. */
  resetCache: () => void;
  /** Open the keyboard cheatsheet. */
  showShortcuts: () => void;
}

/**
 * Default actions.
 *
 * Deliberately no-ops rather than a thrown error: a screen rendered outside the shell — in a
 * test, or in an error boundary's fallback — should render, not crash on a button nobody
 * pressed.
 */
const FALLBACK: AppActions = {
  startRun: () => {},
  stopRun: () => {},
  discover: () => {},
  resetCache: () => {},
  showShortcuts: () => {},
};

/** The context itself. The shell supplies the value; screens read it through the hook. */
export const appActionsContext = createContext<AppActions>(FALLBACK);

/** The shell's actions. */
export function useAppActions(): AppActions {
  return useContext(appActionsContext);
}
