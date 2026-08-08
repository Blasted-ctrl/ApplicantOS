//! The command surface exposed to the renderer (`docs/CONTRACTS.md` §18).
//!
//! Six commands, and the list is meant to stay that length. Everything the product does is an
//! HTTP call to the backend; these exist only for the three things a webview cannot do for
//! itself — find out where the backend is, restart it, and hand a file to the operating system.
//!
//! **The renderer never hardcodes a port.** `backend_port()` is the only source of the origin,
//! which is what lets the port be chosen at runtime (see `sidecar.rs`). A renderer that
//! defaulted to 8000 "just for dev" would work on the developer's machine and load a stranger's
//! data on a machine where 8000 happened to be taken.
//!
//! **Path commands are scoped, not trusted.** `open_path` and `reveal_in_folder` hand a path to
//! the OS's default handler, which on every platform means "possibly execute something". The
//! renderer is not hostile, but it renders job descriptions written by strangers, and
//! `docs/CONTRACTS.md` §10b treats that text as untrusted throughout the backend; the shell
//! holds the same line. Two independent guards apply: the resolved path must sit inside a
//! directory this app owns, and its extension must not be executable. Both are checked after
//! canonicalisation, so `..` cannot walk out and a symlink cannot point out.

use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager, Runtime, State};

use crate::sidecar::{self, BackendState, BackendStatus};

/// Extensions refused by [`open_path`].
///
/// This is a deny-list rather than an allow-list on purpose: the app legitimately opens PDFs,
/// screenshots, HTML artefacts, JSON exports and log files, and new artefact types are added by
/// the backend without the shell knowing. What must never happen is handing the OS something it
/// will *execute*, so the closed set — the one that can be enumerated — is the dangerous one.
const EXECUTABLE_EXTENSIONS: &[&str] = &[
    "action", "apk", "app", "bat", "bin", "cmd", "com", "command", "cpl", "csh", "dll", "dmg",
    "exe", "gadget", "inf", "ins", "inx", "ipa", "isu", "job", "js", "jse", "jar", "ksh", "lnk",
    "msc", "msi", "msp", "mst", "osx", "out", "paf", "pif", "ps1", "psm1", "reg", "rgs", "run",
    "scf", "scr", "sct", "sh", "shb", "shs", "u3p", "vb", "vbe", "vbs", "vbscript", "workflow",
    "ws", "wsf", "wsh",
];

/// Result type for every command here. The error is a plain string because it crosses into
/// JavaScript, where a structured Rust error would arrive as `{}` anyway.
type CommandResult<T> = Result<T, String>;

// ==========================================================================================
// Backend
// ==========================================================================================

/// The port the backend is listening on.
///
/// # Errors
///
/// Returns the current phase as an error when the backend is not serving, so the renderer can
/// distinguish "still starting" from "failed" without a second round trip.
#[tauri::command]
pub fn backend_port(state: State<'_, BackendState>) -> CommandResult<u16> {
    state.ready_port().ok_or_else(|| {
        let status = state.status();
        status.message.unwrap_or_else(|| {
            format!(
                "The backend is not serving yet (phase: {}).",
                serde_json::to_string(&status.phase).unwrap_or_else(|_| "unknown".into())
            )
        })
    })
}

/// The full backend status, including the phase, the port, the backend version and — on
/// failure — the tail of its output.
#[tauri::command]
pub fn backend_status(state: State<'_, BackendState>) -> BackendStatus {
    state.status()
}

/// Stop the backend and start it again, resolving once it is serving or has failed.
#[tauri::command]
pub async fn restart_backend<R: Runtime>(app: AppHandle<R>) -> BackendStatus {
    sidecar::restart(&app).await
}

// ==========================================================================================
// Filesystem
// ==========================================================================================

/// The application data directory — where the backend's SQLite database, artefacts and
/// rendered documents live.
///
/// # Errors
///
/// Returns an error if the platform has no per-app data location or it cannot be created.
#[tauri::command]
pub fn app_data_dir<R: Runtime>(app: AppHandle<R>) -> CommandResult<String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("No application data directory: {error}"))?;
    std::fs::create_dir_all(&dir)
        .map_err(|error| format!("Could not create {}: {error}", dir.display()))?;
    Ok(dir.to_string_lossy().into_owned())
}

/// Directories a renderer-supplied path is allowed to resolve inside.
///
/// Every entry is a directory *this application writes to*, which is a much tighter rule than
/// "somewhere the user could plausibly want a file from". The downloads and documents
/// directories are deliberately absent: nothing in ApplicantOS writes there, so a path that
/// resolves there did not come from this app and has no business being opened by it.
///
/// In a development build the repository root is added, because that is where the backend's
/// `var/` directory — its artefacts, screenshots and rendered resumes — actually lives.
fn allowed_roots<R: Runtime>(app: &AppHandle<R>) -> Vec<PathBuf> {
    let path = app.path();
    let mut roots: Vec<PathBuf> = [
        path.app_data_dir().ok(),
        path.app_local_data_dir().ok(),
        path.app_cache_dir().ok(),
        path.app_log_dir().ok(),
    ]
    .into_iter()
    .flatten()
    .collect();

    if cfg!(debug_assertions) {
        roots.push(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join(".."),
        );
    }

    // Canonicalising the roots is what makes `starts_with` a real containment test rather than
    // a string comparison: both sides end up in the same form, so a root reached through a
    // symlink or a short 8.3 path still matches. A root that does not exist yet — the log
    // directory before anything has been logged — drops out and grants nothing, which is the
    // right way to fail.
    roots
        .into_iter()
        .filter_map(|root| std::fs::canonicalize(root).ok())
        .collect()
}

/// Resolve a renderer-supplied path and check it against both guards.
///
/// # Errors
///
/// Returns an error when the path does not exist, escapes every allowed root, or — when
/// `require_openable` is set — carries an executable extension.
fn resolve_scoped<R: Runtime>(
    app: &AppHandle<R>,
    raw: &str,
    require_openable: bool,
) -> CommandResult<PathBuf> {
    if raw.trim().is_empty() {
        return Err("No path was given.".into());
    }

    let resolved = std::fs::canonicalize(raw)
        .map_err(|error| format!("Cannot open {raw}: {error}"))?;

    let roots = allowed_roots(app);
    if !roots.iter().any(|root| resolved.starts_with(root)) {
        return Err(format!(
            "Refusing to open {} — it is outside every directory this app owns.",
            resolved.display()
        ));
    }

    if require_openable {
        let extension = resolved
            .extension()
            .map(|value| value.to_string_lossy().to_ascii_lowercase())
            .unwrap_or_default();
        if EXECUTABLE_EXTENSIONS.contains(&extension.as_str()) {
            return Err(format!(
                "Refusing to open {} — `.{extension}` files are executable.",
                resolved.display()
            ));
        }
    }

    Ok(resolved)
}

/// Open a file or directory with the operating system's default handler.
///
/// # Errors
///
/// Returns an error when the path fails either scope guard, or when the platform's opener
/// cannot be launched.
#[tauri::command]
pub fn open_path<R: Runtime>(app: AppHandle<R>, path: String) -> CommandResult<()> {
    let target = resolve_scoped(&app, &path, true)?;
    launch_opener(&target, false)
}

/// Show a file in the system file manager with the file itself selected.
///
/// # Errors
///
/// Returns an error when the path is outside every allowed root, or when the file manager
/// cannot be launched. The executable-extension guard does not apply: revealing a file never
/// runs it.
#[tauri::command]
pub fn reveal_in_folder<R: Runtime>(app: AppHandle<R>, path: String) -> CommandResult<()> {
    let target = resolve_scoped(&app, &path, false)?;
    launch_opener(&target, true)
}

/// Hand a path to the platform's opener or file manager.
///
/// The child is spawned and deliberately not waited on: Explorer returns a non-zero exit code
/// in ordinary success cases, and none of these programs report anything worth surfacing.
/// What *is* reported is a failure to launch at all, which is the only actionable outcome.
///
/// `std::process::Command` is used rather than the shell plugin's `open` because the guard that
/// matters is the one in [`resolve_scoped`] — an explicit allow-list this file owns and can be
/// audited against, rather than a scope expression in a manifest.
fn launch_opener(target: &Path, reveal: bool) -> CommandResult<()> {
    let mut command = platform_opener(target, reveal);

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        /// Do not flash a console window behind the file manager.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    command
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open {}: {error}", target.display()))
}

/// Build the platform-specific opener invocation.
#[cfg(windows)]
fn platform_opener(target: &Path, reveal: bool) -> std::process::Command {
    // `explorer.exe <path>` opens a file with its registered handler and a directory in a new
    // window; `/select,` switches it to "reveal". The comma is part of the switch, so the
    // argument has to be passed as one token.
    let mut command = std::process::Command::new("explorer.exe");
    if reveal {
        command.arg(format!("/select,{}", target.display()));
    } else {
        command.arg(target);
    }
    command
}

/// Build the platform-specific opener invocation.
#[cfg(target_os = "macos")]
fn platform_opener(target: &Path, reveal: bool) -> std::process::Command {
    let mut command = std::process::Command::new("open");
    if reveal {
        command.arg("-R");
    }
    command.arg(target);
    command
}

/// Build the platform-specific opener invocation.
///
/// Linux has no single file manager, so revealing falls back to opening the containing
/// directory — every desktop environment handles that, and landing in the right folder is most
/// of the value of "reveal".
#[cfg(all(unix, not(target_os = "macos")))]
fn platform_opener(target: &Path, reveal: bool) -> std::process::Command {
    let mut command = std::process::Command::new("xdg-open");
    if reveal {
        command.arg(target.parent().unwrap_or(target));
    } else {
        command.arg(target);
    }
    command
}
