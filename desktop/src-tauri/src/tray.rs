//! The system tray icon.
//!
//! ApplicantOS runs a scheduler: discovery polls every 30 minutes and the status sync reads the
//! user's mailbox on an interval (`docs/CONTRACTS.md` §15, §17.6). A minimized window therefore
//! does not mean "stopped" — and the tray is how the app stays reachable and, more importantly,
//! *visible* while it keeps working. An automation that submits job applications with no
//! persistent indication that it is running would be exactly the kind of invisible agent the
//! safety posture in `CLAUDE.md` refuses to be. Closing the window is a different act: it quits
//! (see `lib.rs`), and the tray icon goes with it.
//!
//! The menu is deliberately three items long. It is not a second navigation surface; it is the
//! shortest path back to the window, plus the two recovery actions that are useless from inside
//! a window that will not load — restarting the backend, and quitting for real.

use tauri::menu::{MenuBuilder, MenuEvent, MenuItemBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Runtime};

use crate::sidecar;

/// Tray icon id, so the icon can be looked up later to update its tooltip.
pub const TRAY_ID: &str = "applicantos";

/// Show and focus the window.
const ID_SHOW: &str = "tray.show";

/// Restart the backend.
const ID_RESTART: &str = "tray.restart";

/// Quit the application, which also stops the backend.
const ID_QUIT: &str = "tray.quit";

/// Create the tray icon and its menu.
///
/// # Errors
///
/// Returns an error if the platform tray cannot be created — on Linux this happens when no
/// StatusNotifier host is running, which is a normal configuration and must not stop startup.
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show = MenuItemBuilder::with_id(ID_SHOW, "Open ApplicantOS").build(app)?;
    let restart = MenuItemBuilder::with_id(ID_RESTART, "Restart Backend").build(app)?;
    let quit = MenuItemBuilder::with_id(ID_QUIT, "Quit ApplicantOS").build(app)?;

    let menu = MenuBuilder::new(app)
        .item(&show)
        .separator()
        .item(&restart)
        .separator()
        .item(&quit)
        .build()?;

    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .menu(&menu)
        // The left click restores the window; the menu belongs on the right button, which is
        // the platform convention everywhere except macOS — and on macOS a left click opens
        // the menu regardless, which is that platform's convention.
        .show_menu_on_left_click(false)
        .tooltip("ApplicantOS")
        .on_menu_event(handle_menu)
        .on_tray_icon_event(handle_icon);

    if let Some(icon) = app.default_window_icon().cloned() {
        builder = builder.icon(icon);
    }

    builder.build(app)?;
    Ok(())
}

/// Handle a tray menu selection.
fn handle_menu<R: Runtime>(app: &AppHandle<R>, event: MenuEvent) {
    match event.id().0.as_str() {
        ID_SHOW => reveal_window(app),
        ID_RESTART => {
            let handle = app.clone();
            tauri::async_runtime::spawn(async move {
                sidecar::restart(&handle).await;
            });
        }
        ID_QUIT => {
            // `exit` unwinds through `RunEvent::Exit`, which is where the backend is stopped —
            // quitting from the tray must not be a path that skips that.
            app.exit(0);
        }
        _ => {}
    }
}

/// Handle a click on the icon itself.
fn handle_icon<R: Runtime>(tray: &tauri::tray::TrayIcon<R>, event: TrayIconEvent) {
    // Only a completed left click. Reacting to `Down` would fire before the user has had the
    // chance to drag the icon, and reacting to `Enter`/`Move` would raise the window on hover.
    if let TrayIconEvent::Click {
        button: MouseButton::Left,
        button_state: MouseButtonState::Up,
        ..
    } = event
    {
        reveal_window(tray.app_handle());
    }
}

/// Bring the main window back: unminimize if needed, show it, and take focus.
///
/// All three steps are required and in this order. A window that was minimized stays minimized
/// through `show()`, and a window that is shown without `set_focus()` can appear behind the
/// window the user was already looking at — which reads as "the tray icon did nothing".
fn reveal_window<R: Runtime>(app: &AppHandle<R>) {
    let Some(window) = app.get_webview_window(crate::MAIN_WINDOW) else {
        return;
    };
    if window.is_minimized().unwrap_or(false) {
        let _ = window.unminimize();
    }
    let _ = window.show();
    let _ = window.set_focus();
}
