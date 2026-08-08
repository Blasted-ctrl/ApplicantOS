/**
 * The shell bridge: backend process status, native-menu actions, and the ready signal.
 *
 * Three separate concerns, kept in one module because all three are subscriptions to the same
 * Tauri event system and all three are no-ops in a plain browser.
 *
 * The backend's status matters to the UI in a way a web app's server never does: it is a
 * child process that can be *starting*, and "starting" is the one state where an empty screen
 * is honest. `docs/CONTRACTS.md` §18 requires a visible error state with a timeout, and this
 * is what feeds it.
 */

import { useEffect } from 'react';

import { resetBaseUrl, onBackendStatus, onMenuAction, signalReady } from '@/lib/tauri';
import { reconnectEvents } from '@/lib/query/ws';
import { useSessionStore } from '@/stores/session';

/**
 * Mirror the shell's backend status into the session store.
 *
 * A phase change to `ready` also clears the memoised origin and forces a socket reconnect:
 * a restarted sidecar picks a **new free port**, so every cached URL is pointed at a closed
 * socket and the event stream is talking to nothing. Waiting out the reconnect backoff would
 * leave the app blind for up to ten seconds after a restart the user asked for.
 */
export function useBackendStatus(): void {
  const setBackend = useSessionStore((s) => s.setBackend);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    let lastPort: number | null = null;

    void onBackendStatus((status) => {
      setBackend(status);
      if (status.phase === 'ready' && status.port !== lastPort) {
        lastPort = status.port;
        resetBaseUrl();
        reconnectEvents();
      }
    }).then((fn) => {
      if (disposed) fn();
      else unlisten = fn;
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [setBackend]);
}

/**
 * Dispatch native-menu activations to the same handlers the keyboard uses.
 *
 * The menu ids in `src-tauri/src/menu.rs` were chosen to match the shortcut ids in
 * `lib/shortcuts.ts`, so a menu item and its accelerator run one function. Where they differ
 * — `data.open`, `backend.restart`, `cache.reset`, `help.safety` have no keyboard binding —
 * the caller supplies the extra ids in the same map.
 *
 * @param handlers - Keyed by menu id.
 */
export function useMenuActions(handlers: Readonly<Record<string, () => void>>): void {
  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    void onMenuAction((action) => {
      handlers[action]?.();
    }).then((fn) => {
      if (disposed) fn();
      else unlisten = fn;
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [handlers]);
}

/**
 * Tell the shell the first frame is painted, so it may show the window.
 *
 * Layer 1 of the three-layer white-flash prevention (`docs/UI.md` §5.2). Fired from an
 * effect after the first commit — early enough that the shell's timeout never expires, late
 * enough that the window reveals a painted app rather than an empty root.
 */
export function useReadySignal(): void {
  useEffect(() => {
    void signalReady();
  }, []);
}
