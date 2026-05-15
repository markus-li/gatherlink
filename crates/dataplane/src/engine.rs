//! engine
//!
//! High-speed Rust packet engine. No business policy belongs here.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Own lifecycle of Rust dataplane workers and socket tasks.
// - Accept only validated runtime config from Python.
// - Expose narrow control API for paths, services, policy, and stats.
