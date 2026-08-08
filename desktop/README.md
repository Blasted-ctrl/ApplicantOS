# ApplicantOS — desktop

The Tauri v2 shell and the React renderer. The shell is a Rust process that owns a window and
the Python backend; the renderer is a single-chunk React app that talks to that backend over
HTTP and a WebSocket. Nothing about the product lives in Rust.

Binding specs: [`docs/CONTRACTS.md`](../docs/CONTRACTS.md) §18 (this shell, exactly),
[`docs/UI.md`](../docs/UI.md) (every visual and motion decision), and the ten golden rules in
[`CLAUDE.md`](../CLAUDE.md).

---

## Run it

```bash
cd desktop
npm install
npm run dev          # backend + Vite, in a browser at http://localhost:5173
npm run app          # the same, inside the Tauri window
```

`npm run dev` finds the backend before it starts one. If something is already serving
`/health` on port 8000 it adopts it; otherwise it starts uvicorn from the project's virtualenv
on a free port and writes that port to `desktop/.dev-backend.json`, which the debug build of the
shell reads and attaches to. Two backends never end up sharing one SQLite file, which is the
failure `docs/CONTRACTS.md` §18 singles out.

It looks for the interpreter at `.venv/Scripts/python.exe` (Windows) or `.venv/bin/python`, then
`venv/`. Override it:

```bash
APPLICANTOS_PYTHON=/path/to/python npm run dev
```

There is no fallback to a bare `python` on `PATH`: the system interpreter almost certainly lacks
this project's dependencies, and `ModuleNotFoundError: fastapi` is a worse error than being told
no virtualenv was found.

### Before any `cargo` command

`npm run app`, `npm run app:build` and `cargo check` all run `build.rs`, which copies the
sidecar binary and treats a missing one as an error. Build it once:

```bash
python src-tauri/sidecar/build_sidecar.py
```

See [`src-tauri/binaries/README.md`](src-tauri/binaries/README.md). `npm run dev` does not need
it.

The build script sets `SQLITE_MODE=true` for the analysis pass, and that line is load-bearing.
The backend discovers its own routers and plugins by walking its packages at runtime, so
PyInstaller has to import every module to find them — and `app/database/session.py` builds the
engine at import time from a `DATABASE_URL` that defaults to Postgres. Without the switch, every
route module raises `ModuleNotFoundError: asyncpg` during analysis, PyInstaller skips them all
silently, and the resulting binary starts, logs a healthy startup, and serves **zero routes**.

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Backend (adopt or spawn) + Vite. The everyday loop. |
| `npm run dev:vite` | Vite alone, against a backend you are managing yourself. |
| `npm run app` | `tauri dev` — the shell, which runs `npm run dev` for the frontend. |
| `npm run build` | Typecheck, then the production bundle into `dist/`. |
| `npm run app:build` | `tauri build` — installers into `src-tauri/target/release/bundle/`. |
| `npm run typecheck` | `tsc --noEmit` over the renderer, then over the launcher (`checkJs`). |
| `npm run lint` | ESLint, zero warnings tolerated. |

---

## What the shell exposes to the renderer

### Commands — `@tauri-apps/api/core`'s `invoke`

| Command | Signature | Notes |
|---|---|---|
| `backend_port` | `() => Promise<number>` | Rejects while starting or failed, with the reason. |
| `backend_status` | `() => Promise<BackendStatus>` | Never rejects. |
| `restart_backend` | `() => Promise<BackendStatus>` | Resolves once serving or failed. |
| `app_data_dir` | `() => Promise<string>` | Creates the directory if needed. |
| `open_path` | `(args: { path: string }) => Promise<void>` | Scope- and extension-guarded. |
| `reveal_in_folder` | `(args: { path: string }) => Promise<void>` | Scope-guarded. |

**Never hardcode the port.** It is chosen at runtime so two installs cannot collide. Read it
from `backend_port()` or from the `baseUrl` on `backend_status()`.

`open_path` and `reveal_in_folder` refuse any path that does not resolve inside a directory this
app *writes to* — the app data, local data, cache and log directories, plus the repository root
in a debug build. Downloads and Documents are deliberately not on the list. `open_path`
additionally refuses executable extensions. Both checks run after canonicalisation, so `..` and
symlinks cannot walk out.

### Events — `@tauri-apps/api/event`'s `listen`

| Event | Payload | Meaning |
|---|---|---|
| `backend://status` | `BackendStatus` | Every backend transition. |
| `menu://action` | `string` (the menu item id) | A native menu item was chosen (macOS). |

The renderer must emit one event back:

| Event | When |
|---|---|
| `app://ready` | Once the shell is mounted, the theme is applied and the first paint is committed. |

The window is created invisible and is shown on `app://ready` — that is layer one of the
three-layer white-flash defence in `docs/UI.md` §5.2 (the other two are the window
`backgroundColor` and the inline `<style>` in `index.html`). If the event never arrives the
window is shown anyway after 8 seconds, with a warning on stderr: an application with no window
cannot be reported as broken.

### Types to mirror in `src/lib/api/types.ts`

```ts
export type BackendPhase = 'starting' | 'ready' | 'failed' | 'stopped';

export interface BackendStatus {
  phase: BackendPhase;
  port: number | null;
  baseUrl: string | null;   // 'http://127.0.0.1:<port>'
  version: string | null;   // backend package version, from GET /health
  managed: boolean;         // false when attached to an externally started backend
  message: string | null;   // failure reason; null unless phase === 'failed'
  detail: string[];         // tail of the backend's own output
  changedAt: number;        // Unix milliseconds
}
```

`menu://action` ids, all of which the renderer already has a keyboard binding for
(`docs/UI.md` §9.2), except the last two:

`settings` · `postings.discover` · `session.start` · `session.stop` · `command.palette` ·
`sidebar.toggle` · `detail.toggle` · `theme.toggle` · `nav.back` · `nav.forward` ·
`app.reload` · `cache.reset` · `help.shortcuts` · `help.safety`

`backend.restart` and `data.open` are handled inside the shell and never reach the renderer.

### Store

`window-state.json`, through `@tauri-apps/plugin-store`:

| Key | Type | Owner |
|---|---|---|
| `theme` | `'light' \| 'dark' \| 'system'` | The renderer writes it; the shell reads it before the window is shown. |
| `window` | `{ x, y, width, height, maximized }` | The shell only. Logical units. |

Writing `theme` is what makes the *next* launch open with the right window background — the
shell reads it before `show()` and paints the matching `--bg-chrome` behind the webview.

---

## Layout

```
desktop/
  package.json  vite.config.ts  tsconfig.json  tsconfig.node.json
  index.html  eslint.config.js
  scripts/dev-with-backend.mjs         # the dev loop's process supervisor
  src/                                 # the renderer (see docs/UI.md)
  src-tauri/
    Cargo.toml  tauri.conf.json  build.rs
    capabilities/default.json          # the renderer's entire permission surface
    icons/  generate_icons.py          # every icon, from one vector description
    binaries/                          # frozen sidecars (build artefacts, git-ignored)
    sidecar/  server.py  build_sidecar.py
    src/
      main.rs      # entry point + the Windows subsystem attribute
      lib.rs       # assembly order, and why the capability list is that short
      sidecar.rs   # the backend's process lifecycle
      commands.rs  # the six commands, and the path scope guard
      store.rs     # window geometry + theme, restored before the window is shown
      menu.rs      # the macOS application menu
      tray.rs      # the system tray
```

## Notes that will save you an afternoon

**The window has no frame, and the two platforms get there differently.** `tauri.conf.json` is
written for macOS (`decorations: true` + `titleBarStyle: "Overlay"`, which floats the native
traffic lights over the renderer's 38px bar); `lib.rs` turns decorations off on Windows and
Linux, where the frame is all-or-nothing. On those platforms the renderer draws the
minimise/maximise/close controls in the 138px right inset, which is why
`core:window:allow-minimize` and friends are in the capability list — `core:window:default` is
read-only and grants none of them.

**Every element of a frameless titlebar needs `-webkit-app-region`.** The bar is `drag`; every
interactive child must set `no-drag`. Forgetting it on one button is the classic frameless-window
bug and presents as "this button does nothing".

**`build.target` is `['edge120', 'safari17']`, not a Chromium version.** WKWebView is the
strictest of the three system webviews, so it sets the floor. A Chromium target would ship
syntax that parses on the developer's Windows machine and throws on a Mac.

**The CSP is static but the port is not.** `connect-src` allows `http://127.0.0.1:*` and
`ws://127.0.0.1:*` because the backend's port is chosen at runtime. The dev CSP additionally
allows inline and eval'd script, which React Fast Refresh injects into `index.html`; the
production CSP does not.

**`cargo check` validates `tauri.conf.json`.** A typo in a window key is a compile error, not a
silent default — `build.rs` deserialises the config against the linked Tauri version's schema.
