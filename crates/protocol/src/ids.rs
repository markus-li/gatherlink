//! Compact protocol identifiers.
//!
//! Human-readable names stay in Python/config/helper layers. Rust dataplane
//! frames use compact IDs so future security, replay, metrics, and overlay
//! fields are stable and cheap to process.

/// Authenticated peer/session context identifier.
pub type SessionId = u128;

/// Virtual UDP service identifier.
pub type ServiceId = u64;

/// Logical path/carrier identifier.
pub type PathId = u16;

/// Reserved route/transit identifier for future explicit overlay plans.
pub type RouteId = u16;

/// Global per-session/service packet sequence number.
///
/// Data frames keep this sequence global so receivers can detect cross-path
/// missing, duplicate, and out-of-order arrivals. Per-path attribution is
/// reported over the control metaband instead of adding fixed data-header bytes.
pub type SequenceNumber = u64;
