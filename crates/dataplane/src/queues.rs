//! queues
//!
//! High-speed Rust packet engine. No business policy belongs here.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Implement bounded per-path queues.
// - Never allow a blocked WSS/TCP/QUIC path to stall the whole engine.
// - Expose queue depth and queue age counters.
