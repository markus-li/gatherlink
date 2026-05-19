//! Error types for transport crypto primitives.

use core::fmt;

/// Public receive failures intentionally collapse into one silent-drop result.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CryptoError {
    /// The packet is too short, malformed, unauthenticated, or replayed.
    SilentDrop,
    /// A local caller provided invalid key/session material.
    InvalidInput,
}

impl fmt::Display for CryptoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SilentDrop => f.write_str("packet must be silently dropped"),
            Self::InvalidInput => f.write_str("invalid crypto input"),
        }
    }
}

impl std::error::Error for CryptoError {}
