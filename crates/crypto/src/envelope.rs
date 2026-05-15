//! envelope
//!
//! Transport envelope, AEAD, nonce, key, and replay primitives.
//!
//! Rust code should stay focused on deterministic, high-performance execution.
//! Product policy, path interpretation, config expansion, and environment logic
//! belong in the Python control plane.
// File-specific TODO:
// - Implement authenticated transport envelope using established AEAD primitives.
// - Return silent validation failure for invalid public UDP packets.
// - Keep age out of packet transport; age is only for at-rest sealed bundles.
