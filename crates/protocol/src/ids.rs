//! Compact protocol identifiers.
//!
//! Human-readable names stay in Python/config/helper layers. Rust dataplane
//! frames use compact IDs so future security, replay, metrics, and overlay
//! fields are stable and cheap to process.

/// Authenticated peer/session context identifier.
pub type SessionId = u128;

/// Virtual UDP or internal Gatherlink service identifier.
///
/// The low range is reserved for Gatherlink-owned control-plane services.
/// User/application services start at [`USER_SERVICE_ID_START`]. Keeping this
/// as `u16` leaves 65,280 application service ids while charging only two bytes
/// in every data and control frame.
pub type ServiceId = u16;

/// Invalid service id used when a config or frame has not selected a service.
pub const SERVICE_ID_INVALID: ServiceId = 0;

/// Reserved id for the generic control metaband.
pub const SERVICE_ID_CONTROL_METADATA: ServiceId = 1;

/// Reserved id for sink-time/internal clock sync control.
pub const SERVICE_ID_TIME_SYNC: ServiceId = 2;

/// Reserved id for future internal DNS helper traffic.
pub const SERVICE_ID_INTERNAL_DNS: ServiceId = 3;

/// Reserved id for path discovery and keepalive control.
pub const SERVICE_ID_PATH_DISCOVERY: ServiceId = 4;

/// Reserved id for diagnostics and high-detail service monitor requests.
pub const SERVICE_ID_DIAGNOSTICS: ServiceId = 5;

/// Reserved id for safe config apply and reload coordination.
pub const SERVICE_ID_CONFIG_APPLY: ServiceId = 6;

/// Reserved id for future authentication and crypto handshake traffic.
pub const SERVICE_ID_AUTH_CRYPTO: ServiceId = 7;

/// Reserved id for on-demand remote IPC/status export.
pub const SERVICE_ID_REMOTE_STATUS: ServiceId = 8;

/// Final Gatherlink-owned service id reserved for future internal services.
pub const RESERVED_SERVICE_ID_END: ServiceId = 255;

/// First service id available to user/application UDP services.
pub const USER_SERVICE_ID_START: ServiceId = RESERVED_SERVICE_ID_END + 1;

/// Return whether a service id is reserved for Gatherlink internals.
pub const fn is_reserved_service_id(service_id: ServiceId) -> bool {
    service_id <= RESERVED_SERVICE_ID_END
}

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
