//! Protocol parse/encode errors.

use std::fmt;

/// Errors returned when a Gatherlink frame cannot be parsed or encoded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    BufferTooSmall,
    UnsupportedVersion(u16),
    UnknownFrameKind(u8),
    UnknownFlags(u16),
    HeaderLengthMismatch { expected: usize, actual: usize },
    PayloadLengthMismatch { expected: usize, actual: usize },
    PayloadTooLarge(usize),
    FragmentationNotSupported,
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BufferTooSmall => write!(formatter, "buffer is too small for a Gatherlink frame"),
            Self::UnsupportedVersion(version) => {
                write!(formatter, "unsupported protocol version: {version}")
            }
            Self::UnknownFrameKind(kind) => write!(formatter, "unknown frame kind: {kind}"),
            Self::UnknownFlags(flags) => write!(formatter, "unknown frame flags: {flags:#06x}"),
            Self::HeaderLengthMismatch { expected, actual } => {
                write!(
                    formatter,
                    "header length mismatch: expected {expected}, got {actual}"
                )
            }
            Self::PayloadLengthMismatch { expected, actual } => {
                write!(
                    formatter,
                    "payload length mismatch: expected {expected}, got {actual}"
                )
            }
            Self::PayloadTooLarge(length) => {
                write!(formatter, "payload too large for v1 frame: {length} bytes")
            }
            Self::FragmentationNotSupported => write!(
                formatter,
                "fragmented frames are reserved but not supported in v1"
            ),
        }
    }
}

impl std::error::Error for ProtocolError {}
