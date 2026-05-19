//! Nonce helpers for the compact AEAD transport envelope.

/// ChaCha20-Poly1305 nonce size in bytes.
pub const NONCE_LEN: usize = 12;

/// Build the deterministic per-direction nonce from a transport counter.
///
/// The first four bytes are zero, matching the compact WireGuard-like shape.
/// The remaining eight bytes are the big-endian packet counter. Python owns
/// key/session lifecycle; Rust only executes the compiled counter/key state.
#[must_use]
pub fn nonce_from_counter(counter: u64) -> [u8; NONCE_LEN] {
    let mut nonce = [0u8; NONCE_LEN];
    nonce[4..].copy_from_slice(&counter.to_be_bytes());
    nonce
}
