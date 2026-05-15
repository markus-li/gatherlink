//! control
//!
//! Wire/control protocol definitions shared by dataplane components.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Define control frames for metrics, probes, capabilities, time exchange, and diagnostics.
// - Keep control protocol versioned and backward-compatible.
// - Avoid unauthenticated public replies.
