//! replay
//!
//! Transport envelope, AEAD, nonce, key, and replay primitives.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Implement sliding replay window per session/path.
// - Reject stale packets without leaking distinguishable failure behavior.
// - Add unit tests for wraparound and boundary behavior.
