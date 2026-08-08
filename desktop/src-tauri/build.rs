//! Tauri's build step.
//!
//! `tauri_build::build()` does three things this crate depends on: it validates
//! `tauri.conf.json` against the schema for the linked Tauri version (a typo in a window key is
//! a compile error, not a silent default), it compiles `capabilities/*.json` into the ACL that
//! gates the renderer's command access, and on Windows it embeds `icons/icon.ico` and the
//! version information into the executable's resource table.

fn main() {
    tauri_build::build();
}
