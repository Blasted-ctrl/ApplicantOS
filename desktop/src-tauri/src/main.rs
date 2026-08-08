//! Executable entry point.
//!
//! Everything lives in the library crate (`lib.rs`) so that the same assembly can be driven by
//! a test harness or a mobile entry point without going through `main`. This file exists to
//! call it, and to carry the one attribute that cannot live anywhere else.

// Detach from the console on Windows in release builds. Without this, launching the app from
// Explorer flashes a console window behind it for the life of the process. Debug builds keep
// the console, because that is where the backend's stdout and every `eprintln!` in the
// supervisor go.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    applicantos_lib::run();
}
