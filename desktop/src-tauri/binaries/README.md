# Sidecar binaries

`tauri.conf.json` declares `bundle.externalBin: ["binaries/applicantos-server"]`. Tauri appends
the target triple, so the file this directory must contain is:

```
applicantos-server-x86_64-pc-windows-msvc.exe
applicantos-server-aarch64-apple-darwin
applicantos-server-x86_64-unknown-linux-gnu
```

…one per platform you build for. Produce the one for this machine with:

```bash
python desktop/src-tauri/sidecar/build_sidecar.py
```

using the interpreter that has the project installed (`pip install -e ".[sqlite]"`) plus
`pyinstaller`. The script asks `rustc` for the triple, so the name always matches what the
bundler looks for.

It also sets `SQLITE_MODE=true` for the analysis pass. That is not a convenience: the backend
finds its routers and plugins by walking its own packages at runtime, so PyInstaller must import
every module to see them, and importing one builds the database engine from a `DATABASE_URL`
that defaults to Postgres. Without the switch every route module raises
`ModuleNotFoundError: asyncpg`, PyInstaller skips them all silently, and the binary starts
cleanly while serving zero routes.

**The binaries are build artefacts and are not committed** — see `.gitignore` here. They are
30–60MB each and are reproducible from the Python source in one command.

**When you need one:** any `cargo` command in `src-tauri/` runs `build.rs`, which copies
external binaries and treats a missing one as an error. So `cargo check`, `npm run app`
(`tauri dev`) and `npm run app:build` all require it.

**When you do not:** `npm run dev` — the ordinary development loop — starts the backend from the
project's virtualenv with a reloader attached and the Rust shell attaches to that process
instead of spawning a sidecar, which is also why two backends never end up sharing one SQLite
file (see `src/sidecar.rs`).
