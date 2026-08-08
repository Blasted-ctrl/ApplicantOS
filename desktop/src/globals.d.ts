/**
 * Ambient declarations for values the bundler injects rather than the module graph.
 *
 * `vite/client` covers `import.meta.env`'s built-ins and the asset module shapes; what it
 * cannot know is what `vite.config.ts` puts in `define`, or which `VITE_`/`APPLICANTOS_`
 * variables this app actually reads. Both are declared here so a typo in an env var name is
 * a compile error rather than an `undefined` that silently falls back to a default origin.
 */

/// <reference types="vite/client" />

/** Renderer version, injected from `package.json` by `vite.config.ts`. */
declare const __APP_VERSION__: string;

interface ImportMetaEnv {
  /**
   * Backend origin override for browser-only development (`npm run dev:vite` against a
   * hand-started uvicorn). Ignored inside the Tauri shell, where `backend_port()` is the
   * only source of truth for the port — see `src/lib/tauri.ts`.
   */
  readonly VITE_API_URL?: string;

  /**
   * Port written by `scripts/dev-with-backend.mjs` after it picks a free one. Present only
   * in the dev launcher's environment.
   */
  readonly VITE_APPLICANTOS_BACKEND_PORT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
