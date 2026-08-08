//! The ApplicantOS desktop shell (`docs/CONTRACTS.md` §18).
//!
//! Assembly order is the only behaviour in this file; everything it wires lives in its own
//! module. The order matters in three places.
//!
//! **The backend is started before the window is shown, and neither waits for the other.**
//! Startup is dominated by two independent latencies — Python importing FastAPI, and the
//! webview parsing the bundle — and running them in series would add one to the other. The
//! supervisor task is spawned first and the renderer paints from its persisted query cache
//! (`docs/UI.md` §10.5), so by the time the user sees the window there is usually already a
//! backend behind it. A cold start budget of 1.5s to interactive does not survive doing these
//! one after the other.
//!
//! **Geometry and theme are restored before `show()`, never after.** The window is created with
//! `visible: false`; see `store.rs` for why the restore has to complete first.
//!
//! **The backend is stopped on two different events, on purpose.** Closing the window and
//! exiting the process are distinct, and on Windows the first happens without the second. Both
//! call `sidecar::shutdown`, which is idempotent. `docs/CONTRACTS.md` §18 singles out the
//! orphaned uvicorn — still holding the SQLite file, invisible, and fatal to the next launch —
//! as the most common failure mode for this architecture, and one missed path is all it takes.
//!
//! # The capability manifest, and why it is that short
//!
//! `capabilities/default.json` is the list of things the renderer is allowed to ask the shell
//! to do. It is written as an allow-list of individual commands rather than plugin defaults,
//! because the renderer's job is to draw an interface over an HTTP API — it does not need the
//! operating system, and a capability granted "just in case" is one a compromised dependency
//! can use. The renderer parses job descriptions written by strangers (`docs/CONTRACTS.md`
//! §10b), so this is not a hypothetical threat model.
//!
//! * `core:default` — IPC, events, path helpers, image and resource plumbing. Without it no
//!   command can be invoked at all.
//! * `core:window:*` — the specific window verbs a frameless titlebar needs and nothing else:
//!   `start-dragging` for the drag region, `minimize` / `maximize` / `unmaximize` /
//!   `toggle-maximize` / `close` for the controls the OS is no longer drawing (`docs/UI.md`
//!   §5.2), `show` / `set-focus` for the ready handshake, `set-theme` and
//!   `set-background-color` for the theme toggle, and the `is-*` readbacks those need. Note
//!   that `core:window:default` grants none of these — it is read-only — so each is listed.
//! * `shell:allow-execute`, scoped to the sidecar — the only executable the renderer may ever
//!   launch is this app's own backend. Every other program on the machine is denied by the
//!   absence of a matching scope entry, and there is no `shell:allow-open`: opening files goes
//!   through `open_path`, which checks a path allow-list this crate owns (`commands.rs`).
//! * `dialog:allow-open` — the file picker for adding a knowledge source. Not
//!   `dialog:allow-save`: nothing in the app writes a user-chosen path; artefacts are written
//!   by the backend into its own data directory.
//! * `store:default` — the theme and window-state store, which is also the only persistence
//!   the renderer owns.
//! * `fs:*-appdata-recursive` — read, write and metadata inside the app data directory, and
//!   nowhere else. This is the directory the backend also writes its artefacts to, so the
//!   renderer can display a rendered resume without a wider grant. `$HOME`, the documents
//!   directory, and every other base directory are absent.

mod commands;
/// macOS only — see the module's own documentation for why a frameless window and a native
/// menu coexist there and nowhere else. Compiling it on Windows and Linux would leave the whole
/// module unreachable.
#[cfg(target_os = "macos")]
mod menu;
mod sidecar;
mod store;
mod tray;

use std::time::Duration;

use tauri::{Listener, Manager, RunEvent, WindowEvent};

use sidecar::BackendState;

/// Label of the window declared in `tauri.conf.json`. The renderer, the tray and the store all
/// address the same window through this constant.
pub const MAIN_WINDOW: &str = "main";

/// Emitted by the renderer once the shell is mounted, the theme is applied and the first paint
/// is committed. Showing the window before this is what produces the white flash that
/// `docs/UI.md` §5.2 spends three layers of defence preventing.
pub const READY_EVENT: &str = "app://ready";

/// How long to wait for [`READY_EVENT`] before showing the window anyway.
///
/// This is not a timeout on a slow machine — the renderer emits within a frame or two of
/// mounting. It is a guard against a renderer that threw before it could emit, because the
/// alternative failure mode is an application that runs with no window and no way to reach it
/// except the tray. A visible broken window can be reported; an invisible one cannot.
const READY_FALLBACK: Duration = Duration::from_secs(8);

/// Build, configure and run the desktop application.
///
/// # Panics
///
/// Panics if the Tauri context cannot be built — a corrupt `tauri.conf.json`, a missing icon,
/// or an unavailable webview runtime. There is no recovery from any of those and no window in
/// which to report them.
pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .manage(BackendState::new())
        .invoke_handler(tauri::generate_handler![
            commands::backend_port,
            commands::backend_status,
            commands::restart_backend,
            commands::open_path,
            commands::reveal_in_folder,
            commands::app_data_dir,
        ]);

    // Registered only where a menu exists to fire it. The tray has its own handler.
    #[cfg(target_os = "macos")]
    let builder = builder.on_menu_event(menu::handle);

    builder
        .setup(|app| {
            let handle = app.handle().clone();

            // Start the backend first: it is the longest pole in startup and nothing below
            // depends on it having finished.
            let backend_handle = handle.clone();
            tauri::async_runtime::spawn(async move {
                sidecar::start(&backend_handle).await;
            });

            // macOS keeps its menu outside the window, so a frameless window and a native menu
            // coexist there. On Windows and Linux the menu would have to be drawn inside the
            // client area, on top of the renderer's own titlebar.
            #[cfg(target_os = "macos")]
            {
                app.set_menu(menu::build(&handle)?)?;
            }

            // A tray is unavailable on a Linux session with no StatusNotifier host. That is a
            // supported configuration, not an error: the app is fully usable without it.
            if let Err(error) = tray::build(&handle) {
                eprintln!("[applicantos] tray unavailable: {error}");
            }

            let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
                return Err(format!("window `{MAIN_WINDOW}` is missing from tauri.conf.json").into());
            };

            // `docs/UI.md` §5.2 asks for a frameless window on both platforms, but the two
            // platforms reach it from opposite directions and `tauri.conf.json` has one window
            // block for both. macOS wants decorations *on* with an overlay title bar, which is
            // what keeps the native traffic lights floating over the renderer's 38px bar; the
            // config is written for that case. Windows and Linux have no such mode — the frame
            // is all-or-nothing — so decorations come off here, and the renderer draws the
            // minimise/maximise/close controls itself in the 138px right inset. The window is
            // still invisible at this point, so removing the frame costs no repaint.
            #[cfg(not(target_os = "macos"))]
            {
                let _ = window.set_decorations(false);
            }

            store::restore(&handle, &window);

            let ready_window = window.clone();
            handle.listen(READY_EVENT, move |_event| {
                let _ = ready_window.show();
                let _ = ready_window.set_focus();
            });

            let fallback_window = window.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(READY_FALLBACK).await;
                if !fallback_window.is_visible().unwrap_or(true) {
                    eprintln!(
                        "[applicantos] renderer did not signal {READY_EVENT} within {}s; \
                         showing the window anyway",
                        READY_FALLBACK.as_secs()
                    );
                    let _ = fallback_window.show();
                    let _ = fallback_window.set_focus();
                }
            });

            let event_handle = handle.clone();
            let persisted_window = window.clone();
            window.on_window_event(move |event| {
                if store::should_persist(event) {
                    store::persist(&persisted_window);
                }

                if matches!(event, WindowEvent::CloseRequested { .. }) {
                    // Closing the main window quits, on every platform. A single-window app
                    // whose window is gone but whose backend still runs is the orphan case in
                    // a different costume: the scheduler would keep submitting applications
                    // with nothing on screen to say so. The tray covers "keep working, get out
                    // of the way" — that is minimizing, not closing.
                    sidecar::shutdown(&event_handle);
                    event_handle.exit(0);
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the ApplicantOS shell")
        .run(|app, event| match event {
            // Fired when something asks the process to end: the last window closing, a
            // platform quit request, or `AppHandle::exit`. The exit is not prevented — this is
            // the last chance to take the backend with us.
            RunEvent::ExitRequested { .. } => sidecar::shutdown(app),
            // The final event, after the event loop has stopped. Idempotent by then in the
            // ordinary case, and the only path that runs when the process is ended in a way
            // that skips `ExitRequested`.
            RunEvent::Exit => sidecar::shutdown(app),
            _ => {}
        });
}
