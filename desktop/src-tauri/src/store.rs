//! Persisted window geometry and theme (`docs/UI.md` §5.2).
//!
//! Two values survive a restart, and both are read **before the window is shown**. That
//! ordering is the whole reason this module exists: the window is created invisible, and the
//! three-layer white-flash defence in `docs/UI.md` §5.2 only holds if the window's size,
//! position and background colour are already correct at the moment it becomes visible.
//! Restoring geometry after `show()` produces a visible jump; restoring the theme after
//! `show()` produces a flash of the wrong palette.
//!
//! Geometry is stored in **logical** units. A user who moves the app between a Retina display
//! and a 1080p monitor, or changes their display scaling, would otherwise find the window
//! doubling or halving — physical pixels are not a stable description of "where the window was".
//!
//! Restored geometry is validated against the monitors that exist *now*. Reconnecting to a
//! different desk is the ordinary case, not the edge case, and a window restored onto a monitor
//! that is no longer attached is invisible and unrecoverable without editing the store by hand.

use serde::{Deserialize, Serialize};
use tauri::{
    AppHandle, LogicalPosition, LogicalSize, Manager, Runtime, Theme, WebviewWindow, WindowEvent,
};
use tauri_plugin_store::StoreExt;

/// Store file, inside the app config directory the plugin manages.
pub const STORE_FILE: &str = "window-state.json";

/// Key holding the window rectangle.
const GEOMETRY_KEY: &str = "window";

/// Key holding the theme choice. Shared with the renderer, which writes it through
/// `@tauri-apps/plugin-store` when the user toggles the theme (`docs/UI.md` §2.3).
const THEME_KEY: &str = "theme";

/// Smallest window we will restore to; matches `minWidth` in `tauri.conf.json`.
const MIN_WIDTH: f64 = 1120.0;

/// Smallest window height we will restore to; matches `minHeight` in `tauri.conf.json`.
const MIN_HEIGHT: f64 = 720.0;

/// How much of the restored window must land on a live monitor for the position to be kept.
/// A titlebar's worth of window is the difference between "slightly off-screen" and "gone".
const MIN_VISIBLE_MARGIN: f64 = 80.0;

/// `--bg-chrome` in the dark theme (`docs/UI.md` §2.2). The window paints this before the
/// webview has anything to show.
const CHROME_DARK: (u8, u8, u8, u8) = (0x08, 0x09, 0x0C, 0xFF);

/// `--bg-chrome` in the light theme (`docs/UI.md` §2.3).
const CHROME_LIGHT: (u8, u8, u8, u8) = (0xEC, 0xEE, 0xF1, 0xFF);

/// The persisted window rectangle, in logical units.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
struct Geometry {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    maximized: bool,
}

/// The user's theme choice. `System` is a real third state, not the absence of a choice —
/// `docs/UI.md` §2.3 requires following `prefers-color-scheme` when no explicit choice is set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThemeChoice {
    Light,
    Dark,
    System,
}

impl ThemeChoice {
    /// Parse the value the renderer writes.
    fn parse(raw: &str) -> Self {
        match raw {
            "light" => Self::Light,
            "dark" => Self::Dark,
            _ => Self::System,
        }
    }

    /// The window theme to force, or `None` to follow the OS.
    fn window_theme(self) -> Option<Theme> {
        match self {
            Self::Light => Some(Theme::Light),
            Self::Dark => Some(Theme::Dark),
            Self::System => None,
        }
    }
}

/// Read the persisted theme choice.
///
/// A missing or unreadable store is [`ThemeChoice::System`], which is also the first-run
/// default: the app should look like the rest of the machine until told otherwise.
pub fn theme_choice<R: Runtime>(app: &AppHandle<R>) -> ThemeChoice {
    let Ok(store) = app.store(STORE_FILE) else {
        return ThemeChoice::System;
    };
    store
        .get(THEME_KEY)
        .and_then(|value| value.as_str().map(ThemeChoice::parse))
        .unwrap_or(ThemeChoice::System)
}

/// Apply the persisted geometry and theme to a window that has not been shown yet.
///
/// Failures are non-fatal and silent by design: a corrupt or missing store must produce the
/// default window, never a startup error. There is nothing the user could do with the message,
/// and the recovery — "use the configured defaults" — is already correct.
pub fn restore<R: Runtime>(app: &AppHandle<R>, window: &WebviewWindow<R>) {
    let choice = theme_choice(app);
    let _ = window.set_theme(choice.window_theme());

    let resolved_dark = match choice {
        ThemeChoice::Light => false,
        ThemeChoice::Dark => true,
        // `Window::theme()` reports what the OS resolved, which is what the renderer's
        // `prefers-color-scheme` will also see.
        ThemeChoice::System => window.theme().map(|theme| theme != Theme::Light).unwrap_or(true),
    };
    let chrome = if resolved_dark { CHROME_DARK } else { CHROME_LIGHT };
    let _ = window.set_background_color(Some(chrome.into()));

    let Some(geometry) = load_geometry(app) else {
        return;
    };

    let width = geometry.width.max(MIN_WIDTH);
    let height = geometry.height.max(MIN_HEIGHT);
    let _ = window.set_size(LogicalSize::new(width, height));

    if is_position_visible(window, geometry.x, geometry.y, width) {
        let _ = window.set_position(LogicalPosition::new(geometry.x, geometry.y));
    } else {
        let _ = window.center();
    }

    if geometry.maximized {
        let _ = window.maximize();
    }
}

/// Read the stored rectangle, if there is a well-formed one.
fn load_geometry<R: Runtime>(app: &AppHandle<R>) -> Option<Geometry> {
    let store = app.store(STORE_FILE).ok()?;
    let raw = store.get(GEOMETRY_KEY)?;
    serde_json::from_value::<Geometry>(raw).ok()
}

/// Whether enough of the window would land on a currently attached monitor.
///
/// Only the top edge is tested, and generously: a window whose titlebar is reachable can be
/// dragged anywhere, and being stricter would re-centre windows that users deliberately park
/// half off-screen.
fn is_position_visible<R: Runtime>(window: &WebviewWindow<R>, x: f64, y: f64, width: f64) -> bool {
    let Ok(monitors) = window.available_monitors() else {
        return false;
    };

    monitors.iter().any(|monitor| {
        let scale = monitor.scale_factor();
        let position = monitor.position().to_logical::<f64>(scale);
        let size = monitor.size().to_logical::<f64>(scale);

        let intersection_right = (x + width).min(position.x + size.width);
        let intersection_left = x.max(position.x);
        let visible_width = intersection_right - intersection_left;

        visible_width >= MIN_VISIBLE_MARGIN
            && y + MIN_VISIBLE_MARGIN > position.y
            && y < position.y + size.height
    })
}

/// Write the window's current rectangle to the store.
///
/// A maximized or minimized window reports the rectangle it currently occupies, not the one it
/// would return to, so the size is only recorded when the window is in its normal state — that
/// is the geometry the user actually chose. The `maximized` flag carries the rest.
pub fn persist<R: Runtime>(window: &WebviewWindow<R>) {
    let app = window.app_handle();
    let Ok(store) = app.store(STORE_FILE) else {
        return;
    };

    let (Ok(maximized), Ok(minimized)) = (window.is_maximized(), window.is_minimized()) else {
        return;
    };
    if minimized {
        return;
    }

    let (Ok(scale), Ok(position), Ok(size)) = (
        window.scale_factor(),
        window.outer_position(),
        window.inner_size(),
    ) else {
        return;
    };

    let position = position.to_logical::<f64>(scale);
    let size = size.to_logical::<f64>(scale);
    let current = Geometry {
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
        maximized,
    };

    // A maximized window reports the whole screen, which is not the rectangle to restore to
    // when it is unmaximized. Keep the last normal rectangle and change only the flag; fall
    // back to the current one only when there is no history — a first launch straight into a
    // maximized window still deserves to reopen maximized.
    let geometry = if maximized {
        match load_geometry(app) {
            Some(previous) => Geometry {
                maximized: true,
                ..previous
            },
            None => current,
        }
    } else {
        current
    };

    if let Ok(value) = serde_json::to_value(geometry) {
        store.set(GEOMETRY_KEY, value);
        let _ = store.save();
    }
}

/// Whether a window event changes geometry worth persisting.
///
/// `Moved` and `Resized` fire continuously during a drag. Writing on every one of them would
/// hammer the disk for the whole gesture, so only the terminal events are honoured — the
/// geometry at the end of a drag is the only geometry anyone wants restored.
#[must_use]
pub fn should_persist(event: &WindowEvent) -> bool {
    matches!(
        event,
        WindowEvent::CloseRequested { .. } | WindowEvent::Destroyed | WindowEvent::Focused(false)
    )
}
