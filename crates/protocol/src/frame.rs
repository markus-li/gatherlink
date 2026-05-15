//! frame
//!
//! Wire/control protocol definitions shared by dataplane components.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Define stable data-frame format with version, IDs, flags, sequence, and payload length.
// - Reserve flags/fields for future fragmentation without enabling it in v1.
// - Keep public UDP frame fingerprinting concerns in mind.
