/**
 * The renderer's entry point, and the exact order the cold-start strategy depends on
 * (`docs/UI.md` §10.5).
 *
 * ```
 * hydrateBeforeRender();   ← synchronous localStorage snapshot, MODULE SCOPE
 * startRuntime();          ← persistence, the event stream, the build-identity check
 * createRoot().render();   ← the first React render already has data
 * ```
 *
 * `hydrateBeforeRender()` must stay synchronous and must stay above `createRoot`.
 * `localStorage.getItem` is sub-millisecond at the snapshot's 500 KB cap, so the very first
 * render paints real numbers rather than an empty shell: **zero frames of empty state.**
 * IndexedDB is asynchronous, which is why even a perfect IDB persister costs one frame of
 * nothing — that frame is the flash this whole arrangement exists to prevent.
 *
 * The window itself is revealed by the Tauri shell only once the renderer says it has painted
 * (`useReadySignal` in the shell). That is layer 1 of the three-layer white-flash prevention;
 * layer 2 is the window's `backgroundColor` and `visible: false`, and layer 3 is the inline
 * `<style>` in `index.html`. All three are required, and none of them is sufficient alone.
 */

import { RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { hydrateBeforeRender, startRuntime } from '@/boot';
import { AppProviders } from '@/components/providers';
import { router } from '@/router';

import '@/styles/globals.css';

// ── Before React ─────────────────────────────────────────────────────────────────────
hydrateBeforeRender();
startRuntime();

// ── React ────────────────────────────────────────────────────────────────────────────
const container = document.getElementById('root');
if (container === null) {
  throw new Error('The #root element is missing from index.html.');
}

createRoot(container).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
);
