//! The Python backend's process lifecycle (`docs/CONTRACTS.md` §18).
//!
//! This module owns exactly one thing: there is at most one FastAPI process belonging to this
//! app, we know its port, and it dies when we do. Everything else here exists to make one of
//! those three statements true in a case where it otherwise would not be.
//!
//! **Why the port is chosen at runtime.** Two copies of ApplicantOS — a released build and a
//! development build, or two user accounts on one machine — must not fight over a port, and a
//! fixed port turns that collision into "the app loads someone else's data". The shell binds
//! `127.0.0.1:0`, reads back what the OS assigned, releases it, and hands that number to the
//! backend. There is a race between releasing and re-binding; it is bounded (a failed bind
//! shows up as the backend exiting during startup, which retries on a fresh port) and it is
//! the only portable way to ask the OS for a free port.
//!
//! **Why shutdown is the important half.** An orphaned uvicorn keeps a handle on the SQLite
//! file. On Windows that handle is mandatory-locking, so the *next* launch cannot open its own
//! database and the app appears permanently broken — with no visible process to blame, because
//! the window is gone. `docs/CONTRACTS.md` §18 names this the most common failure mode for this
//! architecture, which is why [`shutdown`] is wired to both the window-close handler *and*
//! `RunEvent::ExitRequested` in `lib.rs`: those are different events, and on Windows closing the
//! last window fires the first without the second.
//!
//! **Why the kill is not a graceful signal.** There is no SIGTERM on Windows, and Windows is
//! where the orphan actually hurts. Rather than pretend to a graceful path that only exists on
//! one platform, the shell terminates the child and then *waits until the process is really
//! gone* — first for the runtime's `Terminated` event, then, as a backstop, until the port can
//! be bound again. Waiting is what makes this safe: SQLite recovers a hard-killed writer from
//! its journal on the next open, but nothing recovers from a second process still holding the
//! file. Returning from shutdown before the OS has reaped the child is the bug; the signal used
//! to get there is not.
//!
//! **Attach before spawn.** In development the backend is usually already running with a
//! debugger and a reloader attached (`scripts/dev-with-backend.mjs`). Spawning a second one
//! would produce exactly the double-SQLite-handle situation described above, so the supervisor
//! probes for an existing instance first and attaches to it, marking the status `managed:
//! false` so shutdown knows it does not own that process and must not kill it.

use std::collections::{HashMap, VecDeque};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, Runtime};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Sidecar binary name, without the target triple suffix Tauri appends. Must match the
/// `bundle.externalBin` entry in `tauri.conf.json`.
pub const SIDECAR_NAME: &str = "applicantos-server";

/// Event the renderer listens on for every backend transition. The payload is
/// [`BackendStatus`], identical to what `backend_status()` returns, so a listener and a poll
/// can never disagree about the shape.
pub const STATUS_EVENT: &str = "backend://status";

/// Liveness path. Deliberately not `/ready`: `/ready` probes the database and would report
/// "not up" during first-run schema creation, when the process is in fact up and working.
const HEALTH_PATH: &str = "/health";

/// Gap between health polls during startup.
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// Health polls before startup is declared failed — 60 seconds. Generous because the very
/// first launch creates the SQLite schema and loads plugin entry points before serving.
const HEALTH_ATTEMPTS: u32 = 120;

/// Per-request timeout while polling. Short: a health check that hangs is a health check that
/// has already failed.
const HEALTH_REQUEST_TIMEOUT: Duration = Duration::from_secs(2);

/// Timeout for the single probe that decides whether an existing backend is already listening.
/// This one runs before the window is useful, so it is kept well under one frame budget's worth
/// of user-visible delay.
const ATTACH_PROBE_TIMEOUT: Duration = Duration::from_millis(400);

/// How long to wait for a killed child to actually be reaped before moving on.
const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

/// How long to wait for the port to become bindable again after the child is gone.
const PORT_RELEASE_TIMEOUT: Duration = Duration::from_secs(3);

/// Interval between port-release checks.
const PORT_RELEASE_INTERVAL: Duration = Duration::from_millis(50);

/// Backend output lines retained for diagnostics.
const LOG_TAIL_LINES: usize = 40;

/// Lines from that tail attached to a failure status — enough to carry a Python traceback's
/// last frame and its exception line into the renderer's error state.
const FAILURE_DETAIL_LINES: usize = 10;

/// Overrides the discovery order with an already-running backend's port.
const PORT_ENV: &str = "APPLICANTOS_BACKEND_PORT";

/// Overrides the interpreter used by the development fallback.
const PYTHON_ENV: &str = "APPLICANTOS_PYTHON";

/// Written by `scripts/dev-with-backend.mjs` so a debug build can find the backend that the
/// dev launcher already started.
const DEV_HANDOFF_FILE: &str = ".dev-backend.json";

/// Loopback interface. The backend is never bound to anything routable: it holds the user's
/// entire knowledge graph and has no authentication, because it is not reachable.
const BIND_HOST: &str = "127.0.0.1";

// ==========================================================================================
// Status
// ==========================================================================================

/// Where the backend is in its lifecycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendPhase {
    /// Spawning, or waiting for the first successful health response.
    Starting,
    /// Serving. `port` is set and safe to use.
    Ready,
    /// Startup or supervision failed. `message` says what, `detail` carries the output tail.
    Failed,
    /// Deliberately stopped — app shutting down, or between the two halves of a restart.
    Stopped,
}

/// The full backend status, returned by `backend_status()` and emitted on [`STATUS_EVENT`].
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendStatus {
    /// Lifecycle phase.
    pub phase: BackendPhase,
    /// Port the backend is listening on, once known.
    pub port: Option<u16>,
    /// Convenience origin (`http://127.0.0.1:<port>`) so the renderer never builds it itself.
    pub base_url: Option<String>,
    /// Backend package version, read from `/health`. A mismatch against the renderer's
    /// expected schema version is what invalidates the persisted query cache
    /// (`docs/UI.md` §10.5).
    pub version: Option<String>,
    /// Whether this process spawned the backend. `false` means we attached to one that was
    /// already running and must not kill it.
    pub managed: bool,
    /// Human-readable failure reason. `None` unless `phase` is [`BackendPhase::Failed`].
    pub message: Option<String>,
    /// Tail of the backend's own output, for the renderer's error state.
    pub detail: Vec<String>,
    /// Unix milliseconds at which the current phase was entered.
    pub changed_at: u64,
}

impl BackendStatus {
    /// Build a status carrying no port, in the given phase.
    fn blank(phase: BackendPhase) -> Self {
        Self {
            phase,
            port: None,
            base_url: None,
            version: None,
            managed: false,
            message: None,
            detail: Vec::new(),
            changed_at: now_millis(),
        }
    }
}

/// Current wall-clock time in Unix milliseconds, saturating at zero on a pre-epoch clock.
fn now_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

/// Compose the loopback origin for a port.
fn base_url(port: u16) -> String {
    format!("http://{BIND_HOST}:{port}")
}

// ==========================================================================================
// Exit signalling
// ==========================================================================================

/// A one-shot, blocking-waitable "the child has exited" flag.
///
/// It is a `Condvar` rather than a channel because the two sides live in different worlds: the
/// child's exit arrives on an async task draining the runtime's event stream, while the wait
/// happens on the main thread inside `RunEvent::ExitRequested`, where blocking on a future
/// would deadlock the very event loop the future needs in order to complete.
#[derive(Debug, Default)]
struct ExitSignal {
    /// `Some(code)` once the child has been reaped; the inner option is the exit code, which
    /// is absent when the process was terminated by a signal.
    state: Mutex<Option<Option<i32>>>,
    changed: Condvar,
}

impl ExitSignal {
    /// Record the child's exit and wake every waiter.
    fn signal(&self, code: Option<i32>) {
        if let Ok(mut state) = self.state.lock() {
            if state.is_none() {
                *state = Some(code);
            }
        }
        self.changed.notify_all();
    }

    /// Whether the child has already exited.
    fn has_exited(&self) -> bool {
        self.state.lock().map(|state| state.is_some()).unwrap_or(true)
    }

    /// The exit code, if the child has exited and carried one.
    fn exit_code(&self) -> Option<i32> {
        self.state.lock().ok().and_then(|state| state.flatten())
    }

    /// Block until the child exits or the timeout elapses.
    ///
    /// Returns whether the child had exited by the time this returned. A poisoned mutex counts
    /// as "exited": the supervisor task panicked, so nothing will ever signal, and waiting the
    /// full grace period would only delay app shutdown.
    fn wait_for_exit(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let Ok(mut state) = self.state.lock() else {
            return true;
        };
        while state.is_none() {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return false;
            }
            let Ok((next, _)) = self.changed.wait_timeout(state, remaining) else {
                return true;
            };
            state = next;
        }
        true
    }
}

// ==========================================================================================
// Shared state
// ==========================================================================================

/// What the supervisor knows, guarded as one unit so a restart cannot interleave with a
/// shutdown and leave a live child behind an already-`Stopped` status.
struct Inner {
    status: BackendStatus,
    child: Option<CommandChild>,
    exit: Arc<ExitSignal>,
    log: Arc<Mutex<VecDeque<String>>>,
    /// Incremented on every start. A health poll from a superseded generation discards its
    /// result instead of publishing a status for a backend that has already been replaced.
    generation: u64,
}

/// Managed state holding the backend supervisor. Registered with `app.manage()` in `lib.rs`.
pub struct BackendState {
    inner: Mutex<Inner>,
}

impl Default for BackendState {
    fn default() -> Self {
        Self::new()
    }
}

impl BackendState {
    /// Create the supervisor in its pre-start state.
    #[must_use]
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(Inner {
                status: BackendStatus::blank(BackendPhase::Stopped),
                child: None,
                exit: Arc::new(ExitSignal::default()),
                log: Arc::new(Mutex::new(VecDeque::new())),
                generation: 0,
            }),
        }
    }

    /// A snapshot of the current status.
    #[must_use]
    pub fn status(&self) -> BackendStatus {
        match self.inner.lock() {
            Ok(inner) => inner.status.clone(),
            Err(poisoned) => poisoned.into_inner().status.clone(),
        }
    }

    /// The port, if the backend is ready.
    #[must_use]
    pub fn ready_port(&self) -> Option<u16> {
        let status = self.status();
        match status.phase {
            BackendPhase::Ready => status.port,
            _ => None,
        }
    }
}

/// Read the supervisor's managed state out of any handle.
fn state<R: Runtime>(app: &AppHandle<R>) -> tauri::State<'_, BackendState> {
    app.state::<BackendState>()
}

/// Replace the status, publish it to the renderer, and return it.
fn publish<R: Runtime>(app: &AppHandle<R>, status: BackendStatus) -> BackendStatus {
    {
        let handle = state(app);
        let mut inner = match handle.inner.lock() {
            Ok(inner) => inner,
            Err(poisoned) => poisoned.into_inner(),
        };
        inner.status = status.clone();
    }
    // A failed emit means every window is already gone, which is not worth surfacing during
    // shutdown — the status is still recorded for `backend_status()`.
    let _ = app.emit(STATUS_EVENT, status.clone());
    status
}

// ==========================================================================================
// Port allocation
// ==========================================================================================

/// Ask the OS for an unused loopback port.
///
/// The listener is bound and immediately dropped; the number it reveals is what the backend is
/// then told to bind. See the module docs for why this small race is accepted.
fn free_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind((BIND_HOST, 0))?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

/// Whether a port can be bound right now — used to confirm a dead backend really released it.
fn port_is_free(port: u16) -> bool {
    TcpListener::bind((BIND_HOST, port)).is_ok()
}

// ==========================================================================================
// Health probing
// ==========================================================================================

/// The subset of `GET /health` this shell reads. The backend returns more; extra fields are
/// ignored rather than deserialised so a backend addition cannot break the shell.
#[derive(Debug, Deserialize)]
struct HealthReport {
    version: Option<String>,
}

/// Issue one health request.
///
/// The client is passed in rather than built here: startup polls this up to 120 times, and a
/// fresh `Client` per poll would discard the connection pool and rebuild the whole HTTP stack
/// each time. The timeout is per-request instead of on the client, because the single probe
/// that decides whether to attach to an existing backend wants a much shorter one than the
/// startup poll.
///
/// Returns the backend version on success. Any failure — connection refused, timeout, non-2xx,
/// unparsable body — is `None`, because at this layer they all mean the same thing: not yet.
async fn probe(client: &reqwest::Client, port: u16, timeout: Duration) -> Option<String> {
    let response = client
        .get(format!("{}{HEALTH_PATH}", base_url(port)))
        .timeout(timeout)
        .send()
        .await
        .ok()?;
    if !response.status().is_success() {
        return None;
    }
    let report = response.json::<HealthReport>().await.ok()?;
    Some(report.version.unwrap_or_default())
}

// ==========================================================================================
// Discovery of an already-running backend
// ==========================================================================================

/// The development launcher's handoff file.
#[derive(Debug, Deserialize)]
struct DevHandoff {
    port: u16,
}

/// The repository root, known only to a development build.
///
/// `CARGO_MANIFEST_DIR` is baked in at compile time and points at `desktop/src-tauri`, so its
/// grandparent is the repository. In a released build the path refers to the machine that did
/// the build and is meaningless, which is why every caller is behind `debug_assertions`.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .to_path_buf()
}

/// A port that might already have a backend on it, from the environment or the dev handoff.
fn attach_candidate() -> Option<u16> {
    if let Ok(raw) = std::env::var(PORT_ENV) {
        if let Ok(port) = raw.trim().parse::<u16>() {
            return Some(port);
        }
    }

    if cfg!(debug_assertions) {
        let handoff = repo_root().join("desktop").join(DEV_HANDOFF_FILE);
        if let Ok(contents) = std::fs::read_to_string(&handoff) {
            if let Ok(parsed) = serde_json::from_str::<DevHandoff>(&contents) {
                return Some(parsed.port);
            }
        }
    }

    None
}

// ==========================================================================================
// Spawning
// ==========================================================================================

/// Environment handed to the backend process.
///
/// Everything the desktop install needs to differ from a server install is set here rather
/// than in a `.env` the user would have to maintain: the zero-infrastructure SQLite mode, a
/// data directory inside the OS's per-app location, the bound port, and the CORS origins the
/// webview will actually present. Names are the field names from `app/config/settings.py`
/// upper-cased — that module has no `env_prefix`.
fn backend_environment(port: u16, data_dir: &Path) -> HashMap<String, String> {
    let mut env = HashMap::new();
    env.insert("APPLICANTOS_MANAGED_BY".into(), "tauri".into());
    env.insert("SQLITE_MODE".into(), "true".into());
    env.insert("DATA_DIR".into(), data_dir.to_string_lossy().into_owned());
    env.insert("API_HOST".into(), BIND_HOST.into());
    env.insert("API_PORT".into(), port.to_string());
    // The webview's origin differs per platform: a custom `tauri://` scheme on macOS and
    // Linux, an `http://tauri.localhost` shim on Windows because WebView2 cannot register a
    // custom scheme as a secure context. Both are listed unconditionally — an origin that
    // never appears simply never matches — along with the Vite dev server.
    env.insert(
        "CORS_ORIGINS".into(),
        r#"["tauri://localhost","http://tauri.localhost","https://tauri.localhost","http://localhost:5173","http://127.0.0.1:5173"]"#
            .into(),
    );
    env
}

/// Arguments every backend entry point accepts, sidecar or development interpreter.
fn backend_args(port: u16) -> Vec<String> {
    vec![
        "--host".into(),
        BIND_HOST.into(),
        "--port".into(),
        port.to_string(),
    ]
}

/// Locate a Python interpreter for the development fallback.
///
/// Checked in order: an explicit override, then the two conventional virtualenv locations, then
/// nothing — falling through to a bare `python` on `PATH` is deliberately *not* done, because
/// the system interpreter almost certainly lacks this project's dependencies and the resulting
/// `ModuleNotFoundError` is a far more confusing failure than "no interpreter configured".
fn find_dev_python() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var(PYTHON_ENV) {
        let path = PathBuf::from(explicit);
        if path.is_file() {
            return Some(path);
        }
    }

    let relative = if cfg!(windows) {
        ["Scripts", "python.exe"]
    } else {
        ["bin", "python"]
    };
    for venv in [".venv", "venv"] {
        let candidate = repo_root().join(venv).join(relative[0]).join(relative[1]);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// The spawned child plus the stream of its output events.
type SpawnedBackend = (tauri::async_runtime::Receiver<CommandEvent>, CommandChild);

/// Start the bundled sidecar, or — in a development build with no sidecar compiled — uvicorn
/// from the project's virtualenv.
fn spawn_process<R: Runtime>(
    app: &AppHandle<R>,
    port: u16,
    data_dir: &Path,
) -> Result<SpawnedBackend, String> {
    let env = backend_environment(port, data_dir);

    let sidecar = app
        .shell()
        .sidecar(SIDECAR_NAME)
        .map(|command| command.args(backend_args(port)).envs(env.clone()).spawn());

    match sidecar {
        Ok(Ok(spawned)) => return Ok(spawned),
        Ok(Err(error)) if !cfg!(debug_assertions) => {
            return Err(format!("The backend sidecar failed to start: {error}"))
        }
        Err(error) if !cfg!(debug_assertions) => {
            return Err(format!(
                "The backend sidecar `{SIDECAR_NAME}` is missing from this build: {error}"
            ))
        }
        _ => {}
    }

    // Development fallback. A release build never reaches here.
    let python = find_dev_python().ok_or_else(|| {
        format!(
            "No backend to start. A development build needs either a running backend, a \
             virtualenv at `.venv/`, or {PYTHON_ENV} pointing at an interpreter with the \
             project installed. Run `npm run dev` in `desktop/` to start one."
        )
    })?;

    let mut args = vec![
        "-m".to_string(),
        "uvicorn".to_string(),
        "app.main:app".to_string(),
    ];
    args.extend(backend_args(port));

    app.shell()
        .command(python.to_string_lossy().into_owned())
        .args(args)
        .envs(env)
        .current_dir(repo_root())
        .spawn()
        .map_err(|error| format!("Failed to launch the development backend: {error}"))
}

/// Drain the child's output stream: keep a bounded tail for diagnostics and signal exit.
///
/// This task is the only owner of the receiver and it must keep running for the child's whole
/// life — dropping the receiver early would discard the `Terminated` event that [`shutdown`]
/// waits on, and shutdown would then always burn its full grace period.
fn drain_output(
    mut events: tauri::async_runtime::Receiver<CommandEvent>,
    log: Arc<Mutex<VecDeque<String>>>,
    exit: Arc<ExitSignal>,
) {
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                    record(&log, String::from_utf8_lossy(&bytes).trim_end().to_string());
                }
                CommandEvent::Error(message) => record(&log, format!("[shell] {message}")),
                CommandEvent::Terminated(payload) => {
                    record(
                        &log,
                        format!(
                            "[shell] backend exited (code {:?}, signal {:?})",
                            payload.code, payload.signal
                        ),
                    );
                    exit.signal(payload.code);
                    break;
                }
                _ => {}
            }
        }
        // The stream can end without a `Terminated` event if the runtime drops it; treat that
        // as an exit so nothing waits forever on a child that is already gone.
        exit.signal(None);
    });
}

/// Append one line to the bounded output tail.
fn record(log: &Arc<Mutex<VecDeque<String>>>, line: String) {
    if line.is_empty() {
        return;
    }
    let Ok(mut lines) = log.lock() else {
        return;
    };
    if lines.len() == LOG_TAIL_LINES {
        lines.pop_front();
    }
    lines.push_back(line);
}

/// The last few recorded lines, oldest first.
fn tail(log: &Arc<Mutex<VecDeque<String>>>) -> Vec<String> {
    let Ok(lines) = log.lock() else {
        return Vec::new();
    };
    lines
        .iter()
        .skip(lines.len().saturating_sub(FAILURE_DETAIL_LINES))
        .cloned()
        .collect()
}

// ==========================================================================================
// Lifecycle
// ==========================================================================================

/// Bring the backend up, and leave a terminal status behind either way.
///
/// Safe to call repeatedly: any process this app owns is shut down first, so a double-start
/// cannot produce two backends on two ports with one database between them.
pub async fn start<R: Runtime>(app: &AppHandle<R>) -> BackendStatus {
    stop_and_wait(app).await;
    publish(app, BackendStatus::blank(BackendPhase::Starting));

    let client = match reqwest::Client::builder().build() {
        Ok(client) => client,
        Err(error) => {
            return publish(
                app,
                BackendStatus {
                    message: Some(format!("Could not build an HTTP client: {error}")),
                    ..BackendStatus::blank(BackendPhase::Failed)
                },
            )
        }
    };

    if let Some(port) = attach_candidate() {
        if let Some(version) = probe(&client, port, ATTACH_PROBE_TIMEOUT).await {
            return publish(
                app,
                BackendStatus {
                    phase: BackendPhase::Ready,
                    port: Some(port),
                    base_url: Some(base_url(port)),
                    version: Some(version),
                    managed: false,
                    ..BackendStatus::blank(BackendPhase::Ready)
                },
            );
        }
    }

    let data_dir = match app.path().app_data_dir() {
        Ok(dir) => dir,
        Err(error) => {
            return publish(
                app,
                BackendStatus {
                    message: Some(format!("No writable application data directory: {error}")),
                    ..BackendStatus::blank(BackendPhase::Failed)
                },
            )
        }
    };
    if let Err(error) = std::fs::create_dir_all(&data_dir) {
        return publish(
            app,
            BackendStatus {
                message: Some(format!(
                    "Could not create the application data directory at {}: {error}",
                    data_dir.display()
                )),
                ..BackendStatus::blank(BackendPhase::Failed)
            },
        );
    }

    let port = match free_port() {
        Ok(port) => port,
        Err(error) => {
            return publish(
                app,
                BackendStatus {
                    message: Some(format!("Could not reserve a local port: {error}")),
                    ..BackendStatus::blank(BackendPhase::Failed)
                },
            )
        }
    };

    let (events, child) = match spawn_process(app, port, &data_dir) {
        Ok(spawned) => spawned,
        Err(message) => {
            return publish(
                app,
                BackendStatus {
                    message: Some(message),
                    ..BackendStatus::blank(BackendPhase::Failed)
                },
            )
        }
    };

    let (exit, log, generation) = {
        let handle = state(app);
        let mut inner = match handle.inner.lock() {
            Ok(inner) => inner,
            Err(poisoned) => poisoned.into_inner(),
        };
        inner.generation = inner.generation.wrapping_add(1);
        inner.exit = Arc::new(ExitSignal::default());
        inner.log = Arc::new(Mutex::new(VecDeque::new()));
        inner.child = Some(child);
        (
            Arc::clone(&inner.exit),
            Arc::clone(&inner.log),
            inner.generation,
        )
    };

    drain_output(events, Arc::clone(&log), Arc::clone(&exit));

    publish(
        app,
        BackendStatus {
            phase: BackendPhase::Starting,
            port: Some(port),
            base_url: Some(base_url(port)),
            managed: true,
            ..BackendStatus::blank(BackendPhase::Starting)
        },
    );

    for _ in 0..HEALTH_ATTEMPTS {
        if !is_current_generation(app, generation) {
            // A restart superseded this attempt while it was polling. Its own poll owns the
            // status from here; publishing anything now would overwrite a newer truth.
            return state(app).status();
        }

        if exit.has_exited() {
            let code = exit.exit_code();
            return publish(
                app,
                BackendStatus {
                    message: Some(format!(
                        "The backend exited during startup{}. Its last output is below.",
                        code.map(|code| format!(" with code {code}")).unwrap_or_default()
                    )),
                    detail: tail(&log),
                    ..BackendStatus::blank(BackendPhase::Failed)
                },
            );
        }

        if let Some(version) = probe(&client, port, HEALTH_REQUEST_TIMEOUT).await {
            return publish(
                app,
                BackendStatus {
                    phase: BackendPhase::Ready,
                    port: Some(port),
                    base_url: Some(base_url(port)),
                    version: Some(version),
                    managed: true,
                    ..BackendStatus::blank(BackendPhase::Ready)
                },
            );
        }

        tokio::time::sleep(HEALTH_POLL_INTERVAL).await;
    }

    publish(
        app,
        BackendStatus {
            port: Some(port),
            base_url: Some(base_url(port)),
            managed: true,
            message: Some(format!(
                "The backend did not answer {HEALTH_PATH} within {} seconds.",
                HEALTH_ATTEMPTS * u32::try_from(HEALTH_POLL_INTERVAL.as_millis()).unwrap_or(500)
                    / 1000
            )),
            detail: tail(&log),
            ..BackendStatus::blank(BackendPhase::Failed)
        },
    )
}

/// Whether the given start attempt is still the newest one.
fn is_current_generation<R: Runtime>(app: &AppHandle<R>, generation: u64) -> bool {
    let handle = state(app);
    let inner = match handle.inner.lock() {
        Ok(inner) => inner,
        Err(poisoned) => poisoned.into_inner(),
    };
    inner.generation == generation
}

/// Stop the backend this app owns, and do not return until it is gone.
///
/// Synchronous by design: both callers — the window-close handler and
/// `RunEvent::ExitRequested` — run on the main thread, and the whole point of this function is
/// that the process outlives it by zero milliseconds.
///
/// A child is recorded only when this process spawned one, so attaching to an externally
/// managed backend makes this a no-op beyond clearing the status. That is the intended
/// behaviour: killing a backend we did not start would take down the developer's reloader, and
/// on a shared machine someone else's session.
pub fn shutdown<R: Runtime>(app: &AppHandle<R>) {
    let (child, exit, port) = {
        let handle = state(app);
        let mut inner = match handle.inner.lock() {
            Ok(inner) => inner,
            Err(poisoned) => poisoned.into_inner(),
        };
        let port = inner.status.port;
        (inner.child.take(), Arc::clone(&inner.exit), port)
    };

    if let Some(child) = child {
        let pid = child.pid();
        match child.kill() {
            Ok(()) => {
                if !exit.wait_for_exit(SHUTDOWN_GRACE) {
                    log_shutdown_warning(&format!(
                        "backend pid {pid} did not report exit within {}s",
                        SHUTDOWN_GRACE.as_secs()
                    ));
                }
            }
            Err(error) => log_shutdown_warning(&format!("could not kill backend pid {pid}: {error}")),
        }

        // Backstop: the child being reaped and the OS releasing its listening socket are two
        // different moments, and it is the second one that decides whether the next launch can
        // bind. On Windows a socket in TIME_WAIT can outlive the process, which is exactly the
        // window in which a fast relaunch would otherwise fail.
        if let Some(port) = port {
            let deadline = Instant::now() + PORT_RELEASE_TIMEOUT;
            while Instant::now() < deadline && !port_is_free(port) {
                std::thread::sleep(PORT_RELEASE_INTERVAL);
            }
        }
    }

    publish(app, BackendStatus::blank(BackendPhase::Stopped));
}

/// Report a shutdown problem on stderr.
///
/// Deliberately not `eprintln!`-free: this runs while the app is tearing down, after any
/// logging the renderer could show is gone, and a silent failure here is precisely the
/// orphaned-process bug the module exists to prevent.
fn log_shutdown_warning(message: &str) {
    eprintln!("[applicantos] shutdown: {message}");
}

/// Run [`shutdown`] without holding an async worker thread for the grace period.
///
/// [`shutdown`] blocks until the operating system has actually reaped the child — that is the
/// point of it — which is several seconds in the worst case. Calling it directly from an async
/// task would park a Tokio worker for that whole time, and the task that has to keep running
/// during it is the one draining the child's output, which is what reports the exit we are
/// waiting for.
async fn stop_and_wait<R: Runtime>(app: &AppHandle<R>) {
    let handle = app.clone();
    let _ = tauri::async_runtime::spawn_blocking(move || shutdown(&handle)).await;
}

/// Stop the backend and start it again, returning the resulting status.
///
/// [`start`] already stops whatever is running before it spawns, so this is `start` under the
/// name its call sites mean. Shutting down twice would be harmless but would double the worst
/// case wait for no reason.
pub async fn restart<R: Runtime>(app: &AppHandle<R>) -> BackendStatus {
    start(app).await
}
