//! scheduler::weighted_rr
//!
//! High-speed Rust packet engine. No business policy belongs here.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Implement deterministic weighted round-robin for MVP.
// - Keep algorithm allocation-free in hot path.
// - Add tests for distribution and disabled-path behavior.
