/**
 * The boot sequence, split into the part that must run **before** React and the part that
 * runs after (`docs/UI.md` §10.5, `docs/CONTRACTS.md` §18).
 *
 * `main.tsx` is expected to be exactly this, and the order is the whole point:
 *
 * ```ts
 * import { hydrateBeforeRender, startRuntime } from '@/boot';
 *
 * hydrateBeforeRender();               // MODULE SCOPE — before createRoot
 * startRuntime();
 * createRoot(document.getElementById('root')!).render(<App />);
 * ```
 *
 * {@link hydrateBeforeRender} is synchronous and must stay that way. `localStorage.getItem`
 * is sub-millisecond at the snapshot's 500 KB cap, so **the first React render already has
 * data: zero frames of empty state.** Moving it into an effect, a promise, or a provider
 * would give the frame back and with it the flash the whole strategy exists to prevent —
 * IndexedDB is asynchronous, which is why even a perfect IDB persister costs one frame of
 * nothing.
 */

import { getHealth } from '@/lib/api/endpoints';
import { queryClient } from '@/lib/query/client';
import { qk } from '@/lib/query/keys';
import {
  hydrateHotSnapshot,
  installPersistence,
  verifyBackendBuild,
} from '@/lib/query/persist';
import { connectEvents } from '@/lib/query/ws';

/**
 * Restore the synchronous hot snapshot. **Call at module scope, before `createRoot`.**
 *
 * Never throws: a corrupt or foreign snapshot is dropped and the app cold-starts normally,
 * which is slower but correct. Failing a launch over a cache entry would be the wrong trade.
 *
 * @returns Whether a usable snapshot was applied — useful only for instrumentation.
 */
export function hydrateBeforeRender(): boolean {
  return hydrateHotSnapshot();
}

/**
 * Start everything that lives outside React: persistence, the event stream, and the
 * build-identity check.
 *
 * All three are module-level singletons on purpose. A subscriber created inside a component
 * would be torn down and rebuilt by every Fast Refresh and by StrictMode's double mount, and
 * each cycle would cost a real WebSocket reconnect and a real round of IndexedDB writes.
 *
 * @returns A teardown function. Called by nothing in production — the process exiting is the
 *   teardown — but it makes the module testable and keeps a Fast Refresh from double-binding.
 */
export function startRuntime(): () => void {
  const stopPersistence = installPersistence();
  const events = connectEvents();

  // The build check runs against the *live* backend, so it cannot be synchronous. It is
  // deliberately not awaited: the app renders from the hot snapshot immediately, and if the
  // backend turns out to be a different build the cache is cleared and everything refetches
  // a few hundred milliseconds later. Clearing first and rendering second would trade a rare
  // wrong-looking screen for a guaranteed empty one.
  void (async () => {
    try {
      const health = await getHealth();
      queryClient.setQueryData(qk.health(), health);
      await verifyBackendBuild(health.version);
    } catch {
      // The backend is not up yet. `useBackendStatus` owns that state and the shell shows
      // its own error surface; there is nothing to report from here.
    }
  })();

  return () => {
    events.close();
    stopPersistence();
  };
}
