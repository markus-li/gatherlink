//! Protocol parse/encode errors.

use std::fmt;

/// Errors returned when a Gatherlink frame cannot be parsed or encoded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    BufferTooSmall,
    UnsupportedVersion(u8),
    UnknownFrameKind(u8),
    UnknownFlags(u16),
    PayloadTooLarge(usize),
    BatchItemTooLarge(usize),
    BatchTooLarge(usize),
    EmptyBatch,
    InvalidFragment,
    MalformedFragment,
    MalformedBatch,
    MalformedControl,
    ControlTooLarge(usize),
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
            Self::PayloadTooLarge(length) => {
                write!(formatter, "payload too large for compact frame: {length} bytes")
            }
            Self::BatchItemTooLarge(length) => {
                write!(formatter, "batch item too large for compact frame: {length} bytes")
            }
            Self::BatchTooLarge(length) => {
                write!(formatter, "batch payload too large for compact frame: {length} bytes")
            }
            Self::EmptyBatch => write!(formatter, "batch frame must contain at least one payload"),
            Self::InvalidFragment => write!(formatter, "fragment metadata is invalid"),
            Self::MalformedFragment => write!(formatter, "fragment metadata is malformed"),
            Self::MalformedBatch => write!(formatter, "batch payload is malformed"),
            Self::MalformedControl => write!(formatter, "control metaband payload is malformed"),
            Self::ControlTooLarge(length) => {
                write!(
                    formatter,
                    "control metaband payload too large for compact frame: {length} bytes"
                )
            }
        }
    }
}

impl std::error::Error for ProtocolError {}
