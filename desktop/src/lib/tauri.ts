/**
 * The renderer's half of the Tauri shell contract (`docs/CONTRACTS.md` §18).
 *
 * Six commands and three events cross this boundary, and every one of them is wrapped here
 * rather than called at the point of use. The reason is the fallback: `npm run dev:vite`
 * serves the renderer in a plain browser with no shell at all, and `npm run dev` runs it
 * against a backend whose port a Node script chose. Both paths have to work, and neither may
 * be allowed to leak an `invoke` call into a module that would then throw on import.
 *
 * **The renderer never hardcodes a port.** `backend_port()` is the only source of the origin
 * inside the shell, because the port is picked at runtime from whatever was free. A
 * renderer that defaulted to 8000 "just for dev" would work on the developer's machine and
 * load a stranger's data on a machine where 8000 happened to be taken.
 *
 * Resolution order for the base URL, first hit wins:
 *
 *   1. `backend_port()` — inside the Tauri shell, always.
 *   2. `VITE_API_URL` — an explicit override for browser development.
 *   3. `VITE_APPLICANTOS_BACKEND_PORT` — written by `scripts/dev-with-backend.mjs`.
 *   4. `http://127.0.0.1:8000` — the documented default in `Settings.api_port`, reached only
 *      when someone opened the Vite dev server directly with no launcher.
 */

import { invoke, isTauri as isTauriRuntime } from '@tauri-apps/api/core';
import { emit, listen, type UnlistenFn } from '@tauri-apps/api/event';

/** Lifecycle phase of the Python sidecar, mirroring `BackendPhase` in `sidecar.rs`. */
export type BackendPhase = 'starting' | 'ready' | 'failed' | 'stopped';

/**
 * The full backend status, returned by `backend_status()` and pushed on `backend://status`.
 *
 * Serialised `camelCase` by serde, which is why `baseUrl` and `changedAt` break this file's
 * otherwise snake_case convention — they are Rust's field names, not the API's.
 */
export interface BackendStatus {
  phase: BackendPhase;
  port: number | null;
  /** `http://127.0.0.1:<port>`, so the renderer never assembles the origin itself. */
  baseUrl: string | null;
  /** Backend package version read from `/health`; half of the cache buster (`docs/UI.md` §10.5). */
  version: string | null;
  /** False when we attached to a backend someone else started, and must not kill it. */
  managed: boolean;
  message: string | null;
  /** Tail of the backend's own stdout/stderr, for the renderer's error state. */
  detail: string[];
  changedAt: number;
}

/** Tauri event carrying every backend transition. Payload is a {@link BackendStatus}. */
export const BACKEND_STATUS_EVENT = 'backend://status';

/** Tauri event carrying a native-menu item id. Payload is the id string. */
export const MENU_EVENT = 'menu://action';

/**
 * Event the renderer emits once it has applied the persisted theme and painted.
 *
 * The shell keeps the window hidden until this arrives (with a timeout), which is layer 1 of
 * the three-layer white-flash prevention in `docs/UI.md` §5.2.
 */
export const READY_EVENT = 'app://ready';

/** Native-menu item ids, from `src-tauri/src/menu.rs`. */
export const MENU_ACTIONS = [
  'settings',
  'postings.discover',
  'session.start',
  'session.stop',
  'data.open',
  'command.palette',
  'sidebar.toggle',
  'detail.toggle',
  'theme.toggle',
  'nav.back',
  'nav.forward',
  'app.reload',
  'backend.restart',
  'cache.reset',
  'help.shortcuts',
  'help.safety',
] as const;
export type MenuAction = (typeof MENU_ACTIONS)[number];

/** Backend origin used when nothing else resolves. Matches `Settings.api_port`. */
const FALLBACK_ORIGIN = 'http://127.0.0.1:8000';

/** Whether the renderer is running inside the Tauri shell rather than a plain browser. */
export function isTauri(): boolean {
  return isTauriRuntime();
}

/**
 * The port the backend is listening on.
 *
 * @throws When the backend is not serving yet — the shell returns the current phase as the
 *   error string so the caller can distinguish "still starting" from "failed" without a
 *   second round trip.
 */
export async function backendPort(): Promise<number> {
  return invoke<number>('backend_port');
}

/** The backend's full status. Never throws; a failed backend is a value, not an exception. */
export async function backendStatus(): Promise<BackendStatus> {
  return invoke<BackendStatus>('backend_status');
}

/** Stop and respawn the sidecar. Resolves with the status after the restart attempt. */
export async function restartBackend(): Promise<BackendStatus> {
  return invoke<BackendStatus>('restart_backend');
}

/** Absolute path of this app's data directory. */
export async function appDataDir(): Promise<string> {
  return invoke<string>('app_data_dir');
}

/**
 * Hand a file to the operating system's default handler.
 *
 * The shell refuses paths outside a directory this app owns and refuses executable
 * extensions, so a failure here is a rejected path rather than a missing file.
 */
export async function openPath(path: string): Promise<void> {
  await invoke('open_path', { path });
}

/** Reveal a file in Explorer / Finder / the desktop file manager. */
export async function revealInFolder(path: string): Promise<void> {
  await invoke('reveal_in_folder', { path });
}

/** Subscribe to backend transitions. Returns the unsubscribe function. */
export async function onBackendStatus(
  handler: (status: BackendStatus) => void,
): Promise<UnlistenFn> {
  if (!isTauri()) return () => {};
  return listen<BackendStatus>(BACKEND_STATUS_EVENT, (event) => {
    handler(event.payload);
  });
}

/** Subscribe to native-menu activations. Returns the unsubscribe function. */
export async function onMenuAction(handler: (action: string) => void): Promise<UnlistenFn> {
  if (!isTauri()) return () => {};
  return listen<string>(MENU_EVENT, (event) => {
    handler(event.payload);
  });
}

/**
 * Tell the shell the first frame is painted and the window may be shown.
 *
 * Safe to call more than once and safe to call outside the shell; the shell listens once.
 */
export async function signalReady(): Promise<void> {
  if (!isTauri()) return;
  await emit(READY_EVENT);
}

/** Resolved once per session — the port cannot change without a restart of the process. */
let cachedBaseUrl: string | null = null;

/** In-flight resolution, so N simultaneous first requests make one `invoke` call. */
let pendingBaseUrl: Promise<string> | null = null;

/** The origin to use when the shell is absent (browser development). */
function browserBaseUrl(): string {
  const explicit = import.meta.env.VITE_API_URL;
  if (explicit !== undefined && explicit !== '') return explicit.replace(/\/+$/, '');

  const port = Number.parseInt(import.meta.env.VITE_APPLICANTOS_BACKEND_PORT ?? '', 10);
  if (Number.isInteger(port) && port > 0 && port < 65536) return `http://127.0.0.1:${port}`;

  return FALLBACK_ORIGIN;
}

/**
 * The backend origin, without a trailing slash.
 *
 * Inside the shell this waits on `backend_port()`, which is the one call in the app that can
 * legitimately be pending at startup — the sidecar health-polls before it reports ready. The
 * result is memoised because a port that changed mid-session would mean the process was
 * restarted, and {@link resetBaseUrl} is the explicit way to say that happened.
 */
export async function resolveBaseUrl(): Promise<string> {
  if (cachedBaseUrl !== null) return cachedBaseUrl;
  if (pendingBaseUrl !== null) return pendingBaseUrl;

  pendingBaseUrl = (async (): Promise<string> => {
    if (!isTauri()) return browserBaseUrl();
    try {
      const port = await backendPort();
      return `http://127.0.0.1:${port}`;
    } finally {
      pendingBaseUrl = null;
    }
  })()
    .then((url) => {
      cachedBaseUrl = url;
      return url;
    })
    .catch((error: unknown) => {
      pendingBaseUrl = null;
      throw error;
    });

  return pendingBaseUrl;
}

/**
 * Forget the memoised origin.
 *
 * Called after `restart_backend()`, which picks a new free port: every subsequent request
 * must resolve against the new one, and a stale memo would send them all to a closed socket.
 */
export function resetBaseUrl(): void {
  cachedBaseUrl = null;
  pendingBaseUrl = null;
}

/**
 * The origin as a `ws://` scheme, for `GET /ws`.
 *
 * @param baseUrl - An origin previously returned by {@link resolveBaseUrl}.
 */
export function toWebSocketOrigin(baseUrl: string): string {
  return baseUrl.replace(/^http/, 'ws');
}
