# Packaging the desktop app

How a working tree becomes an installer someone else can double-click.

The server side of ApplicantOS is containerised (`docker/`, `docker-compose.yml`). **The
desktop app is not, and there is no `docker/desktop.Dockerfile`.** A Tauri application is a
native binary that opens a window on the user's own machine, linked against that machine's
webview — WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux. There is no window to
open inside a container, no webview to link against, and the artifact you would get out is a
Linux ELF that no user can install. Containers solve dependency isolation for servers; code
signing and per-OS bundling solve distribution for desktop apps, and that is what this
document is about.

---

## What is in the box

A shipped ApplicantOS install is three things fused into one bundle:

| Layer | What it is | Where it comes from |
|---|---|---|
| **Shell** | ~10MB Rust binary: one window, a tray icon, and the supervisor that owns the backend process | `cargo` via `tauri build` |
| **Renderer** | React 19 + Vite build, embedded in the binary as static assets | `vite build` |
| **Sidecar** | The entire FastAPI backend, frozen by PyInstaller into one executable | `desktop/src-tauri/sidecar/build_sidecar.py` |

The shell launches the sidecar with `--host 127.0.0.1 --port <free port>`, polls `/health`
until it answers, and only then shows the window. The renderer talks to the sidecar over
loopback HTTP and a WebSocket. Nothing listens on a routable address: the backend has no
authentication because it is not reachable, and `sidecar/server.py` refuses a non-loopback
`--host` outright.

---

## Prerequisites

| | Windows | macOS | Linux |
|---|---|---|---|
| Rust | [rustup](https://rustup.rs) | rustup | rustup |
| Node | ≥ 20.19 (22 LTS recommended) | same | same |
| Python | ≥ 3.12, with the project installed | same | same |
| System | [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (preinstalled on Windows 11), MSVC build tools | Xcode command line tools | `libwebkit2gtk-4.1-dev`, `build-essential`, `curl`, `wget`, `file`, `libxdo-dev`, `libssl-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev` |

`rustc` specifically must be on `PATH` — the sidecar build asks it for the target triple, and
guessing it from `sys.platform` would be wrong (`x86_64-pc-windows-msvc` and
`x86_64-pc-windows-gnu` are the same machine and different triples).

---

## Step 1 — freeze the backend into the sidecar

```bash
# with the interpreter that has the project installed, plus pyinstaller
pip install -e ".[sqlite]" pyinstaller
python desktop/src-tauri/sidecar/build_sidecar.py
```

This produces:

```
desktop/src-tauri/binaries/applicantos-server-<target-triple>[.exe]
```

for example `applicantos-server-x86_64-pc-windows-msvc.exe`,
`applicantos-server-aarch64-apple-darwin`, `applicantos-server-x86_64-unknown-linux-gnu`.

**The target-triple suffix is not decoration.** `tauri.conf.json` declares
`bundle.externalBin: ["binaries/applicantos-server"]` — *without* the suffix — and Tauri
appends the triple of whatever target it is building for. A file without the suffix is
invisible to the bundler, and the build fails with a missing-binary error rather than
silently shipping without a backend.

Three things about this step are worth knowing before you debug it:

**It must run before any `cargo` command.** `build.rs` copies external binaries on every
build and treats a missing one as an error, so `cargo check`, `npm run app` (`tauri dev`)
and `npm run app:build` all require the sidecar to exist first.

**It is not needed for ordinary development.** `npm run dev` starts the backend from the
project virtualenv with a reloader attached, and the Rust shell attaches to that process
instead of spawning a sidecar (`desktop/scripts/dev-with-backend.mjs`, `src/sidecar.rs`).
That is also why two backends never end up sharing one SQLite file.

**The analysis runs with `SQLITE_MODE=true`, and that is load-bearing.** The backend finds
its routers and its plugins by walking its own packages at runtime (`docs/CONTRACTS.md` §14,
§6), so PyInstaller has to import every module to see them — and importing one builds the
database engine from a `DATABASE_URL` that defaults to PostgreSQL. Without the switch every
route module raises `ModuleNotFoundError: asyncpg`, PyInstaller skips them all *silently*,
and you get a binary that starts cleanly and serves zero routes. The build script sets it;
do not remove it.

**Cross-compiling the sidecar is not possible.** PyInstaller freezes for the platform it runs
on. One CI job per target OS, each producing its own suffixed binary, is the only way to get
a multi-platform release — see the matrix sketch at the end.

---

## Step 2 — build the bundle

```bash
cd desktop
npm ci
npm run app:build     # tauri build: vite build, then cargo build --release, then bundle
```

`npm run app:build` is `tauri build`, and `tauri.conf.json` wires `beforeBuildCommand` to
`npm run build`, which runs `tsc --noEmit` before `vite build`. A type error therefore fails
the packaging run rather than shipping.

Artifacts land in `desktop/src-tauri/target/release/bundle/`:

| OS | Output |
|---|---|
| Windows | `nsis/ApplicantOS_0.1.0_x64-setup.exe`, `msi/ApplicantOS_0.1.0_x64_en-US.msi` |
| macOS | `macos/ApplicantOS.app`, `dmg/ApplicantOS_0.1.0_aarch64.dmg` |
| Linux | `deb/applicantos_0.1.0_amd64.deb`, `appimage/applicantos_0.1.0_amd64.AppImage`, `rpm/…` |

Restrict the set with `npm run app:build -- --bundles nsis` (or `dmg`, `deb`, `appimage`) —
`"targets": "all"` in `tauri.conf.json` otherwise builds every bundler available on the host.

The NSIS installer is configured `installMode: "currentUser"`: no UAC prompt, per-user
install. That is the right default for an app whose entire data set is the user's own, and
it keeps the installer usable on a managed machine.

---

## Step 3 — signing

An unsigned desktop app is not merely "untrusted". On Windows SmartScreen shows a full-screen
blue warning, and on macOS Gatekeeper refuses to launch it at all with a message that says
the app is damaged — which is what a user reports as "your app is broken".

### Windows — Authenticode

Since June 2023 a code-signing certificate's private key must live in FIPS-140-2 Level 2
hardware, so a plain `.pfx` file is no longer obtainable from a public CA. In practice that
means an OV/EV certificate on a hardware token, or a cloud signing service (Azure Trusted
Signing, DigiCert KeyLocker, SSL.com eSigner).

Tauri signs by invoking `signtool` on each artifact. Add to `tauri.conf.json` under
`bundle.windows`:

```json
"certificateThumbprint": "A1B2C3…",
"digestAlgorithm": "sha256",
"timestampUrl": "http://timestamp.digicert.com"
```

`timestampUrl` is not optional in any meaningful sense: without a countersigned timestamp
every installer you have ever shipped stops validating the day the certificate expires,
including the ones already on users' machines.

For a cloud signing service, set `bundle.windows.signCommand` to the provider's CLI instead
of a thumbprint, and keep credentials in the CI secret store, never in the file.

EV certificates carry SmartScreen reputation immediately; OV certificates accumulate it over
downloads, so expect warnings on early releases either way.

### macOS — Developer ID plus notarization

Two separate steps, and shipping only the first still produces a Gatekeeper block.

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_ID="you@example.com"
export APPLE_PASSWORD="app-specific-password"   # NOT your Apple ID password
export APPLE_TEAM_ID="TEAMID"

npm run app:build -- --target universal-apple-darwin
```

Tauri signs with the identity, submits to Apple's notary service, waits for the ticket, and
staples it to the `.app` and the `.dmg`. Budget 2-15 minutes for notarization; it is a
network round trip to Apple, and it is where a release pipeline usually times out.

`--target universal-apple-darwin` produces one binary for Intel and Apple Silicon. It
requires **both** architecture slices of the sidecar to exist:
`applicantos-server-x86_64-apple-darwin` and `applicantos-server-aarch64-apple-darwin`. Since
PyInstaller cannot cross-compile, build each on its own machine (or under Rosetta) and place
both files in `binaries/` before the universal build.

The hardened runtime is required for notarization and is enabled by Tauri's macOS bundler.
ApplicantOS needs no additional entitlements: it opens a loopback socket, which needs none,
and it never loads unsigned plugin code into its own process.

### Linux — no signing, detached signatures instead

There is no equivalent of Authenticode. Conventions per format:

* **`.deb`** — publish an APT repository whose `Release` file is signed with GPG. `dpkg -i`
  on a bare file checks nothing.
* **`.AppImage`** — `appimagetool --sign`, plus publish the public key. Users verify with
  `--appimage-signature`.
* **Everything** — publish `SHA256SUMS` and `SHA256SUMS.asc` beside the release. It is the
  minimum that lets a careful user verify a download, and it costs one CI step.

---

## Updater

`tauri-plugin-updater` is **not** in `Cargo.toml`, so builds carry no update mechanism today.
Adding one requires a signing keypair (`npm run tauri signer generate`), the public key in
`tauri.conf.json`, and a hosted `latest.json`. The private key belongs in the CI secret
store; leaking it means an attacker can ship a signed update to every install.

Until that exists, releases are manual downloads. Say so in the release notes rather than
letting users assume the app updates itself.

---

## Release CI sketch

Not in `.github/workflows/ci.yml` on purpose: `ci.yml` runs on every pull request and builds
only the renderer, because a full three-OS Rust build plus three PyInstaller freezes plus
notarization is 30-45 minutes and needs signing secrets that must not be exposed to
pull-request builds from forks.

A `release.yml` triggered on a tag would be:

```yaml
strategy:
  matrix:
    include:
      - { os: windows-latest, target: x86_64-pc-windows-msvc }
      - { os: macos-latest,   target: aarch64-apple-darwin   }
      - { os: macos-13,       target: x86_64-apple-darwin    }
      - { os: ubuntu-22.04,   target: x86_64-unknown-linux-gnu }
```

with, per job: install the Linux system dependencies where applicable → `pip install -e
".[sqlite]" pyinstaller` → `python desktop/src-tauri/sidecar/build_sidecar.py` → `npm ci` →
`tauri-action` with the signing secrets in `env`.

`ubuntu-22.04` rather than `ubuntu-latest` is deliberate: glibc is forward-compatible, not
backward-compatible, so a binary linked on 24.04 refuses to start on 22.04. Build against the
oldest distribution you intend to support.

---

## Checklist before tagging a release

- [ ] The sidecar exists for every target triple in the matrix, freshly frozen from this
      commit — a stale binary in `binaries/` bundles silently and ships old code
- [ ] `npm run typecheck` and `npm run lint` pass in `desktop/`
- [ ] Enum changes are mirrored in `desktop/src/lib/api/types.ts` (CLAUDE.md)
- [ ] `python -m scripts.smoke_test --start` passes against the frozen sidecar, not only
      against `uvicorn` — a route that PyInstaller failed to bundle exists in one and not the
      other
- [ ] The version matches in `package.json`, `tauri.conf.json`, `Cargo.toml`, `pyproject.toml`
      and `app/__init__.py`; the shell compares its version against `/health` and reports a
      mismatch as a sidecar mismatch
- [ ] The installer is signed and, on macOS, notarized **and stapled** — verify with
      `spctl -a -vv ApplicantOS.app` and `codesign --verify --deep --strict`
- [ ] `AUTO_APPLY_ENABLED=false` and `DRY_RUN=true` in the shipped defaults. A packaged build
      that submits applications out of the box is the one bug in this project that cannot be
      fixed after the fact
