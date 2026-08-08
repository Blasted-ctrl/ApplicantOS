/**
 * Vite configuration for the ApplicantOS renderer (docs/CONTRACTS.md §18, docs/UI.md §10.7).
 *
 * Three decisions here are binding rather than preference:
 *
 * 1. `build.target` is the **system-webview baseline**, not a Chromium version. Tauri hosts
 *    WebView2 on Windows, WKWebView on macOS and WebKitGTK on Linux, and WKWebView is the
 *    strictest of the three — so `safari17` is the floor, and `edge120` pins the other end.
 *    Targeting a Chromium release would silently ship syntax Safari cannot parse.
 * 2. **No manual chunks and no route-level code splitting.** Code splitting optimises network
 *    transfer, which does not exist in a packaged desktop app; all it can do here is add a
 *    per-route async fetch that becomes the one visible navigation delay.
 * 3. `strictPort` — the Rust shell's `devUrl` is a fixed origin. If Vite silently moved to
 *    5174 the shell would load a blank window with no error, so a taken port must fail loudly.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/** Dev-server port. Mirrored by `devUrl` in `src-tauri/tauri.conf.json` — change both. */
const DEV_PORT = 5173;

/** Separate HMR port used only when serving to a physical device over the network. */
const DEV_HMR_PORT = 5183;

interface PackageManifest {
  readonly version: string;
}

const manifest = JSON.parse(
  readFileSync(fileURLToPath(new URL('./package.json', import.meta.url)), 'utf8'),
) as PackageManifest;

/**
 * Set by `tauri dev` when the app is being served to a physical device; empty for the normal
 * desktop loop, where the webview and the dev server share a loopback interface.
 */
const devHost = process.env['TAURI_DEV_HOST'];

export default defineConfig({
  plugins: [react(), tailwindcss()],

  // Absolute base in both modes: production is served from a custom protocol at the origin
  // root, so dev and prod resolve assets identically and history routing behaves the same.
  base: '/',

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  // Vite's screen clear would erase the Rust compiler's output in the `tauri dev` terminal.
  clearScreen: false,

  // `TAURI_ENV_*` carries the target triple and platform into the bundle; `APPLICANTOS_*`
  // carries the dev launcher's backend port for the browser-only workflow.
  envPrefix: ['VITE_', 'TAURI_ENV_', 'APPLICANTOS_'],

  define: {
    __APP_VERSION__: JSON.stringify(manifest.version),
  },

  server: {
    port: DEV_PORT,
    strictPort: true,
    host: devHost ?? '127.0.0.1',
    // Spread rather than an explicit `undefined`: Vite's `hmr` option is typed
    // `boolean | HmrOptions`, and "use the default" means the key is absent, not present and
    // undefined.
    ...(devHost === undefined
      ? {}
      : { hmr: { protocol: 'ws' as const, host: devHost, port: DEV_HMR_PORT } }),
    watch: {
      // Rust rebuild artefacts and the backend's runtime directory churn constantly and would
      // otherwise trigger a full-page reload mid-edit.
      ignored: ['**/src-tauri/**', '**/var/**', '**/__pycache__/**'],
    },
  },

  build: {
    target: ['edge120', 'safari17'],
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
    // Recharts and the router push the single chunk past Vite's default warning; the single
    // chunk is the point (see decision 2 above), so the warning is raised rather than obeyed.
    chunkSizeWarningLimit: 2400,
    rollupOptions: {
      output: {
        manualChunks: undefined,
      },
    },
  },
});
