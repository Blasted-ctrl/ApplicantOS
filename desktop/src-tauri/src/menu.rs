//! The native application menu.
//!
//! **Set on macOS only.** The window ships with `decorations: false` so that
//! `docs/UI.md` §5.2's 38px titlebar can be drawn by the renderer; on Windows and Linux there
//! is therefore no frame for a menu bar to live in, and Tauri would have to draw one inside the
//! client area — directly on top of the titlebar the design specifies. macOS keeps its menu in
//! the system bar, outside the window entirely, so there is no conflict there and removing it
//! would break platform conventions (and `⌘Q`, and the Services menu).
//!
//! **Every accelerator here is a shortcut `docs/UI.md` §9.2 already defines.** That is not a
//! coincidence, it is the constraint: a native menu accelerator is consumed by the OS before
//! the webview sees the keystroke, so any accelerator the renderer also binds would silently
//! stop working, and any accelerator the renderer *doesn't* bind would be a second,
//! undocumented way to do something. Each item forwards its id on [`MENU_EVENT`] and the
//! renderer runs the same handler its own key binding would have run — one implementation, two
//! entry points.
//!
//! Two ids are handled here instead of being forwarded: restarting the backend and opening the
//! data folder are shell responsibilities, and the renderer has no privileged way to do either.

use tauri::menu::{
    AboutMetadataBuilder, Menu, MenuBuilder, MenuEvent, MenuItemBuilder, SubmenuBuilder,
};
use tauri::{AppHandle, Manager, Runtime};

use crate::sidecar;

/// Event carrying a menu selection to the renderer. The payload is the item id.
pub const MENU_EVENT: &str = "menu://action";

/// Restart the backend — handled in the shell.
const ID_BACKEND_RESTART: &str = "backend.restart";

/// Open the application data folder — handled in the shell.
const ID_DATA_OPEN: &str = "data.open";

/// Build the application menu.
///
/// # Errors
///
/// Returns an error if the platform menu cannot be constructed.
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let settings = MenuItemBuilder::with_id("settings", "Settings…")
        .accelerator("CmdOrCtrl+,")
        .build(app)?;

    let discover = MenuItemBuilder::with_id("postings.discover", "Discover Postings")
        .accelerator("CmdOrCtrl+Shift+D")
        .build(app)?;
    let start_run = MenuItemBuilder::with_id("session.start", "Start a Run")
        .accelerator("CmdOrCtrl+Shift+R")
        .build(app)?;
    // Ctrl, not Cmd, on every platform: `docs/UI.md` §9.1 reserves Ctrl for irreversible
    // actions, and stopping a run mid-submission is one.
    let stop_run = MenuItemBuilder::with_id("session.stop", "Stop the Running Run")
        .accelerator("Ctrl+S")
        .build(app)?;
    let open_data = MenuItemBuilder::with_id(ID_DATA_OPEN, "Open Data Folder").build(app)?;

    let palette = MenuItemBuilder::with_id("command.palette", "Command Palette…")
        .accelerator("CmdOrCtrl+K")
        .build(app)?;
    let toggle_sidebar = MenuItemBuilder::with_id("sidebar.toggle", "Toggle Sidebar")
        .accelerator("CmdOrCtrl+.")
        .build(app)?;
    let toggle_detail = MenuItemBuilder::with_id("detail.toggle", "Toggle Detail Pane")
        .accelerator("CmdOrCtrl+\\")
        .build(app)?;
    let toggle_theme = MenuItemBuilder::with_id("theme.toggle", "Toggle Theme")
        .accelerator("CmdOrCtrl+Shift+L")
        .build(app)?;
    let back = MenuItemBuilder::with_id("nav.back", "Back")
        .accelerator("CmdOrCtrl+[")
        .build(app)?;
    let forward = MenuItemBuilder::with_id("nav.forward", "Forward")
        .accelerator("CmdOrCtrl+]")
        .build(app)?;
    let reload = MenuItemBuilder::with_id("app.reload", "Reload Interface")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;

    let restart_backend =
        MenuItemBuilder::with_id(ID_BACKEND_RESTART, "Restart Backend").build(app)?;
    let reset_cache = MenuItemBuilder::with_id("cache.reset", "Reset Local Cache").build(app)?;

    let shortcuts = MenuItemBuilder::with_id("help.shortcuts", "Keyboard Shortcuts").build(app)?;
    let safety = MenuItemBuilder::with_id("help.safety", "Safety & Permissions").build(app)?;

    let about = AboutMetadataBuilder::new()
        .name(Some("ApplicantOS"))
        .version(Some(env!("CARGO_PKG_VERSION")))
        .comments(Some(
            "Knowledge-graph-driven job application automation, with a hard kill switch.",
        ))
        .build();

    let app_menu = SubmenuBuilder::new(app, "ApplicantOS")
        .about(Some(about))
        .separator()
        .item(&settings)
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .quit()
        .build()?;

    let file_menu = SubmenuBuilder::new(app, "File")
        .item(&discover)
        .item(&start_run)
        .item(&stop_run)
        .separator()
        .item(&open_data)
        .separator()
        .close_window()
        .build()?;

    let edit_menu = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;

    let view_menu = SubmenuBuilder::new(app, "View")
        .item(&palette)
        .separator()
        .item(&toggle_sidebar)
        .item(&toggle_detail)
        .item(&toggle_theme)
        .separator()
        .item(&back)
        .item(&forward)
        .separator()
        .item(&reload)
        .fullscreen()
        .build()?;

    let backend_menu = SubmenuBuilder::new(app, "Backend")
        .item(&restart_backend)
        .separator()
        .item(&reset_cache)
        .build()?;

    let window_menu = SubmenuBuilder::new(app, "Window")
        .minimize()
        .maximize()
        .separator()
        .bring_all_to_front()
        .build()?;

    let help_menu = SubmenuBuilder::new(app, "Help")
        .item(&shortcuts)
        .item(&safety)
        .build()?;

    MenuBuilder::new(app)
        .items(&[
            &app_menu,
            &file_menu,
            &edit_menu,
            &view_menu,
            &backend_menu,
            &window_menu,
            &help_menu,
        ])
        .build()
}

/// Route a menu selection: handle the two shell-owned ids, forward everything else.
pub fn handle<R: Runtime>(app: &AppHandle<R>, event: MenuEvent) {
    let id = event.id().0.clone();

    match id.as_str() {
        ID_BACKEND_RESTART => {
            let handle = app.clone();
            tauri::async_runtime::spawn(async move {
                sidecar::restart(&handle).await;
            });
        }
        ID_DATA_OPEN => {
            if let Ok(dir) = app.path().app_data_dir() {
                let _ = std::fs::create_dir_all(&dir);
                let _ = crate::commands::open_path(app.clone(), dir.to_string_lossy().into_owned());
            }
        }
        _ => {
            // The renderer owns the rest. A failed emit means there is no window to receive
            // it, which is not an error state — the menu item simply had nowhere to act.
            let _ = tauri::Emitter::emit(app, MENU_EVENT, id);
        }
    }
}
