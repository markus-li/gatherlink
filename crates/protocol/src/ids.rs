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
pub type PathId = u64;

/// Reserved route/transit identifier for future explicit overlay plans.
pub type RouteId = u64;

/// Per-session or per-service sequence number, depending on future policy.
pub type SequenceNumber = u64;
