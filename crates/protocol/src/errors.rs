//! Protocol parse/encode errors.

use std::fmt;

/// Errors returned when a Gatherlink frame cannot be parsed or encoded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    BufferTooSmall,
    UnsupportedVersion(u8),
    UnknownFrameKind(u8),
    UnknownFlags(u16),
    HeaderLengthMismatch { expected: usize, actual: usize },
    HeaderTooLarge(usize),
    PayloadLengthMismatch { expected: usize, actual: usize },
    PayloadTooLarge(usize),
    BatchItemTooLarge(usize),
    BatchTooLarge(usize),
    EmptyBatch,
    FragmentTooLarge(usize),
    InvalidFragment,
    MalformedExtension,
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
            Self::HeaderLengthMismatch { expected, actual } => {
                write!(formatter, "header length mismatch: expected {expected}, got {actual}")
            }
            Self::HeaderTooLarge(length) => {
                write!(formatter, "header too large for v1 frame: {length} bytes")
            }
            Self::PayloadLengthMismatch { expected, actual } => {
                write!(formatter, "payload length mismatch: expected {expected}, got {actual}")
            }
            Self::PayloadTooLarge(length) => {
                write!(formatter, "payload too large for v1 frame: {length} bytes")
            }
            Self::BatchItemTooLarge(length) => {
                write!(formatter, "batch item too large for v1 frame: {length} bytes")
            }
            Self::BatchTooLarge(length) => {
                write!(formatter, "batch payload too large for v1 frame: {length} bytes")
            }
            Self::EmptyBatch => write!(formatter, "batch frame must contain at least one payload"),
            Self::FragmentTooLarge(length) => {
                write!(formatter, "fragment too large for v1 frame: {length} bytes")
            }
            Self::InvalidFragment => write!(formatter, "fragment metadata is invalid"),
            Self::MalformedExtension => write!(formatter, "frame extension area is malformed"),
            Self::MalformedBatch => write!(formatter, "batch payload is malformed"),
            Self::MalformedControl => write!(formatter, "control metaband payload is malformed"),
            Self::ControlTooLarge(length) => {
                write!(
                    formatter,
                    "control metaband payload too large for v1 frame: {length} bytes"
                )
            }
        }
    }
}

impl std::error::Error for ProtocolError {}
