//! mtu
//!
//! High-speed Rust packet engine. No business policy belongs here.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Track effective per-path payload MTU.
// - Skip paths that cannot carry a packet.
// - Emit counters/events when no eligible path exists.
