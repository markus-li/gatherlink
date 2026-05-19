//! Extensible control metaband payloads.
//!
//! Data frames keep the fixed v1 header compact. This module carries sparse
//! peer telemetry and future safe control data, such as path assignment reports,
//! missing sequence ranges, path metadata, and later config-change proposals.

use crate::errors::ProtocolError;
use crate::ids::{PathId, SequenceNumber, ServiceId, USER_SERVICE_ID_START};

/// Current control metaband payload version.
pub const CONTROL_PAYLOAD_VERSION: u8 = 1;

const TYPE_PATH_ASSIGNMENT: u8 = 1;
const TYPE_MISSING_RANGE: u8 = 2;
const TYPE_PATH_METADATA: u8 = 3;
const TYPE_PATH_CAPACITY: u8 = 4;
const TYPE_PATH_LATENCY: u8 = 5;
const TYPE_INTERNAL_CLOCK_SYNC: u8 = 6;
const TYPE_SINK_TIME: u8 = 7;
const TYPE_SERVICE_METADATA: u8 = 8;
const TYPE_SERVICE_ENDPOINT_ASSERTION: u8 = 9;
const TYPE_SERVICE_DISABLE: u8 = 10;
const TYPE_PATH_MTU: u8 = 11;
const TYPE_SERVICE_SCHEDULER_POLICY: u8 = 12;

/// One control metaband message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControlMessage {
    /// Sender telemetry mapping a global sequence range to the path used.
    PathAssignment(PathAssignment),
    /// Receiver telemetry reporting a global sequence range that did not arrive.
    MissingRange(MissingRange),
    /// Friendly path metadata so peers and monitors can map ids to names.
    PathMetadata(PathMetadata),
    /// Directional path capacity estimate in bit/s for scheduler telemetry.
    PathCapacity(PathCapacity),
    /// Directional path latency estimate in microseconds for scheduler telemetry.
    PathLatency(PathLatency),
    /// Local path MTU observation and compiled frame MTU.
    PathMtu(PathMtu),
    /// NTP-style peer-relative internal clock exchange.
    InternalClockSync(InternalClockSync),
    /// Sink-authoritative wall-clock and NTP status for diagnostics and policy.
    SinkTime(SinkTime),
    /// Friendly service metadata so peers and monitors can map compact service ids to names.
    ServiceMetadata(ServiceMetadata),
    /// Peer assertion for endpoint verification only; receivers must never apply it as config.
    ServiceEndpointAssertion(ServiceEndpointAssertion),
    /// Peer request/assertion to stop traffic for one service until config or policy changes.
    ServiceDisable(ServiceDisable),
    /// Python-owned service scheduler policy FYI so the peer can install expected receive facts.
    ServiceSchedulerPolicy(ServiceSchedulerPolicy),
}

/// Sender-side path assignment report for a contiguous global sequence range.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PathAssignment {
    pub first_sequence: SequenceNumber,
    pub packet_count: u32,
    pub path_id: PathId,
}

impl PathAssignment {
    /// Build one sequence range to path-id report.
    pub fn new(first_sequence: SequenceNumber, packet_count: u32, path_id: PathId) -> Result<Self, ProtocolError> {
        if packet_count == 0 {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            first_sequence,
            packet_count,
            path_id,
        })
    }
}

/// Receiver-side missing global sequence range.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MissingRange {
    pub first_sequence: SequenceNumber,
    pub packet_count: u32,
}

impl MissingRange {
    /// Build one missing sequence report.
    pub fn new(first_sequence: SequenceNumber, packet_count: u32) -> Result<Self, ProtocolError> {
        if packet_count == 0 {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            first_sequence,
            packet_count,
        })
    }
}

/// Optional path id to friendly name metadata.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathMetadata {
    pub path_id: PathId,
    pub name: String,
}

impl PathMetadata {
    /// Build path metadata for diagnostics and peer correlation.
    pub fn new(path_id: PathId, name: impl Into<String>) -> Result<Self, ProtocolError> {
        let name = name.into();
        if name.is_empty() || name.len() > u8::MAX as usize {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self { path_id, name })
    }
}

/// Optional service id to friendly name metadata.
///
/// This deliberately carries only a compact service id and display/config name.
/// Service targets, listen addresses, and return endpoints are explicit config
/// and must not be sent through this telemetry message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceMetadata {
    pub service_id: ServiceId,
    pub name: String,
}

impl ServiceMetadata {
    /// Build service metadata for diagnostics and peer correlation.
    pub fn new(service_id: ServiceId, name: impl Into<String>) -> Result<Self, ProtocolError> {
        let name = name.into();
        if service_id < USER_SERVICE_ID_START || name.is_empty() || name.len() > u8::MAX as usize {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self { service_id, name })
    }
}

/// Peer assertion that a service endpoint matches explicit local config.
///
/// This is a safety check, not a config channel. A receiver may stop traffic for
/// the service if this value disagrees with local config, but must not rewrite
/// its target endpoint from this metadata.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceEndpointAssertion {
    pub service_id: ServiceId,
    pub target: String,
}

impl ServiceEndpointAssertion {
    /// Build one endpoint assertion for service-level verification.
    pub fn new(service_id: ServiceId, target: impl Into<String>) -> Result<Self, ProtocolError> {
        let target = target.into();
        if service_id < USER_SERVICE_ID_START || target.is_empty() || target.len() > u8::MAX as usize {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self { service_id, target })
    }
}

/// Peer assertion that a service must stop carrying traffic.
///
/// This is intentionally generic rather than sink-only. Endpoint verification,
/// remote policy, helper lifecycle, and later authenticated config decisions can
/// all use the same loud stop path without changing service config implicitly.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceDisable {
    pub service_id: ServiceId,
    pub reason: String,
}

impl ServiceDisable {
    /// Build one service disable assertion.
    pub fn new(service_id: ServiceId, reason: impl Into<String>) -> Result<Self, ProtocolError> {
        let reason = reason.into();
        if service_id < USER_SERVICE_ID_START || reason.is_empty() || reason.len() > u8::MAX as usize {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self { service_id, reason })
    }
}

/// Python-owned service scheduler policy advertisement.
///
/// This is a policy fact for the peer Python control plane. Rust should only use
/// this after Python chooses to compile it into local receive expectations.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ServiceSchedulerPolicy {
    pub service_id: ServiceId,
    pub fanout: u16,
    pub fanout_below_bytes: u32,
}

impl ServiceSchedulerPolicy {
    /// Build one service scheduler policy advertisement.
    pub fn new(service_id: ServiceId, fanout: u16, fanout_below_bytes: u32) -> Result<Self, ProtocolError> {
        if service_id < USER_SERVICE_ID_START {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            service_id,
            fanout,
            fanout_below_bytes,
        })
    }
}

/// Optional directional max-speed estimate for one path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PathCapacity {
    pub path_id: PathId,
    pub tx_bps: Option<u64>,
    pub rx_bps: Option<u64>,
}

impl PathCapacity {
    /// Build a directional path capacity report.
    pub fn new(path_id: PathId, tx_bps: Option<u64>, rx_bps: Option<u64>) -> Result<Self, ProtocolError> {
        if matches!(tx_bps, Some(0)) || matches!(rx_bps, Some(0)) || (tx_bps.is_none() && rx_bps.is_none()) {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            path_id,
            tx_bps,
            rx_bps,
        })
    }
}

/// Optional directional latency estimate for one path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PathLatency {
    pub path_id: PathId,
    pub tx_current_us: Option<u32>,
    pub tx_mean_us: Option<u32>,
    pub rx_current_us: Option<u32>,
    pub rx_mean_us: Option<u32>,
}

impl PathLatency {
    /// Build a directional path latency report.
    pub fn new(
        path_id: PathId,
        tx_current_us: Option<u32>,
        tx_mean_us: Option<u32>,
        rx_current_us: Option<u32>,
        rx_mean_us: Option<u32>,
    ) -> Result<Self, ProtocolError> {
        if [tx_current_us, tx_mean_us, rx_current_us, rx_mean_us]
            .into_iter()
            .all(|value| value.is_none())
        {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            path_id,
            tx_current_us,
            tx_mean_us,
            rx_current_us,
            rx_mean_us,
        })
    }
}

/// Optional directional path MTU observation.
///
/// The TX values describe what the sender can put onto this carrier path.
/// The RX values are optional peer-view facts the sender has already learned.
/// Receivers flip those directions into their own local view when recording
/// telemetry, matching capacity and latency control metadata.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PathMtu {
    pub path_id: PathId,
    pub tx_link_mtu: Option<u16>,
    pub tx_frame_mtu: Option<u16>,
    pub rx_link_mtu: Option<u16>,
    pub rx_frame_mtu: Option<u16>,
}

impl PathMtu {
    /// Build one directional MTU observation for a path.
    pub fn new(
        path_id: PathId,
        tx_link_mtu: Option<u16>,
        tx_frame_mtu: Option<u16>,
        rx_link_mtu: Option<u16>,
        rx_frame_mtu: Option<u16>,
    ) -> Result<Self, ProtocolError> {
        if !valid_mtu_pair(tx_link_mtu, tx_frame_mtu)
            || !valid_mtu_pair(rx_link_mtu, rx_frame_mtu)
            || (tx_link_mtu.is_none() && rx_link_mtu.is_none())
        {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            path_id,
            tx_link_mtu,
            tx_frame_mtu,
            rx_link_mtu,
            rx_frame_mtu,
        })
    }
}

/// NTP-style internal clock sync message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InternalClockSync {
    pub exchange_id: u64,
    pub path_id: PathId,
    pub mode: ClockSyncMode,
    pub origin_us: u64,
    pub receive_us: Option<u64>,
    pub transmit_us: Option<u64>,
}

impl InternalClockSync {
    /// Build an internal clock sync request or response.
    pub fn new(
        exchange_id: u64,
        path_id: PathId,
        mode: ClockSyncMode,
        origin_us: u64,
        receive_us: Option<u64>,
        transmit_us: Option<u64>,
    ) -> Result<Self, ProtocolError> {
        if exchange_id == 0 || origin_us == 0 {
            return Err(ProtocolError::MalformedControl);
        }
        if mode == ClockSyncMode::Response && (receive_us.is_none() || transmit_us.is_none()) {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            exchange_id,
            path_id,
            mode,
            origin_us,
            receive_us,
            transmit_us,
        })
    }
}

/// Internal clock sync message direction.
#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockSyncMode {
    Request = 1,
    Response = 2,
}

impl ClockSyncMode {
    fn from_u8(value: u8) -> Result<Self, ProtocolError> {
        match value {
            1 => Ok(Self::Request),
            2 => Ok(Self::Response),
            _ => Err(ProtocolError::MalformedControl),
        }
    }

    fn as_u8(self) -> u8 {
        match self {
            Self::Request => 1,
            Self::Response => 2,
        }
    }
}

/// Sink-side system clock status advertised over the control metaband.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SinkTime {
    pub path_id: PathId,
    pub sink_unix_us: u64,
    pub sink_internal_us: u64,
    pub ntp_state: NtpState,
}

impl SinkTime {
    /// Build one sink-authoritative wall-clock report.
    pub fn new(
        path_id: PathId,
        sink_unix_us: u64,
        sink_internal_us: u64,
        ntp_state: NtpState,
    ) -> Result<Self, ProtocolError> {
        if sink_unix_us == 0 || sink_internal_us == 0 {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self {
            path_id,
            sink_unix_us,
            sink_internal_us,
            ntp_state,
        })
    }
}

/// Sink-side NTP synchronization state. Gatherlink reports this but does not set system time.
#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NtpState {
    Unknown = 0,
    Synchronized = 1,
    Unsynchronized = 2,
}

impl NtpState {
    fn from_u8(value: u8) -> Result<Self, ProtocolError> {
        match value {
            0 => Ok(Self::Unknown),
            1 => Ok(Self::Synchronized),
            2 => Ok(Self::Unsynchronized),
            _ => Err(ProtocolError::MalformedControl),
        }
    }

    fn as_u8(self) -> u8 {
        match self {
            Self::Unknown => 0,
            Self::Synchronized => 1,
            Self::Unsynchronized => 2,
        }
    }
}

/// Versioned control metaband payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlPayload {
    pub messages: Vec<ControlMessage>,
}

impl ControlPayload {
    /// Build a control payload from one or more messages.
    pub fn new(messages: Vec<ControlMessage>) -> Result<Self, ProtocolError> {
        if messages.is_empty() || messages.len() > u16::MAX as usize {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self { messages })
    }

    /// Encode as `version, message_count, repeated(type, len, value)`.
    pub fn encode(&self) -> Result<Vec<u8>, ProtocolError> {
        let mut output = Vec::with_capacity(3 + self.messages.len() * 8);
        output.push(CONTROL_PAYLOAD_VERSION);
        output.extend_from_slice(&(self.messages.len() as u16).to_be_bytes());
        for message in &self.messages {
            let (message_type, value) = encode_message(message)?;
            output.push(message_type);
            output.extend_from_slice(&(value.len() as u16).to_be_bytes());
            output.extend_from_slice(&value);
            if output.len() > u16::MAX as usize {
                return Err(ProtocolError::ControlTooLarge(output.len()));
            }
        }
        Ok(output)
    }

    /// Decode a control metaband payload.
    pub fn decode(input: &[u8]) -> Result<Self, ProtocolError> {
        if input.len() < 3 || input[0] != CONTROL_PAYLOAD_VERSION {
            return Err(ProtocolError::MalformedControl);
        }
        let message_count = usize::from(read_u16(input, 1));
        if message_count == 0 {
            return Err(ProtocolError::MalformedControl);
        }
        let mut cursor = 3_usize;
        let mut messages = Vec::with_capacity(message_count);
        for _ in 0..message_count {
            if cursor + 3 > input.len() {
                return Err(ProtocolError::MalformedControl);
            }
            let message_type = input[cursor];
            let value_len = usize::from(read_u16(input, cursor + 1));
            cursor += 3;
            if cursor + value_len > input.len() {
                return Err(ProtocolError::MalformedControl);
            }
            messages.push(decode_message(message_type, &input[cursor..cursor + value_len])?);
            cursor += value_len;
        }
        if cursor != input.len() {
            return Err(ProtocolError::MalformedControl);
        }
        Ok(Self { messages })
    }
}

/// Observation result for one received data sequence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SequenceObservation {
    pub missing_packets: u64,
    pub out_of_order: bool,
    pub duplicate_or_late: bool,
}

/// Wrap-safe global sequence tracker for receiver-side telemetry.
#[derive(Debug, Clone, Default)]
pub struct GlobalSequenceTracker {
    next_expected: Option<SequenceNumber>,
}

impl GlobalSequenceTracker {
    /// Observe one global sequence number and report gaps or out-of-order arrival.
    pub fn observe(&mut self, sequence: SequenceNumber) -> SequenceObservation {
        let Some(expected) = self.next_expected else {
            self.next_expected = Some(sequence.wrapping_add(1));
            return SequenceObservation {
                missing_packets: 0,
                out_of_order: false,
                duplicate_or_late: false,
            };
        };

        if sequence == expected {
            self.next_expected = Some(expected.wrapping_add(1));
            return SequenceObservation {
                missing_packets: 0,
                out_of_order: false,
                duplicate_or_late: false,
            };
        }

        let forward_distance = sequence.wrapping_sub(expected);
        if forward_distance < (1_u64 << 63) {
            self.next_expected = Some(sequence.wrapping_add(1));
            return SequenceObservation {
                missing_packets: forward_distance,
                out_of_order: false,
                duplicate_or_late: false,
            };
        }

        SequenceObservation {
            missing_packets: 0,
            out_of_order: true,
            duplicate_or_late: true,
        }
    }
}

fn encode_message(message: &ControlMessage) -> Result<(u8, Vec<u8>), ProtocolError> {
    match message {
        ControlMessage::PathAssignment(assignment) => {
            let mut value = Vec::with_capacity(14);
            value.extend_from_slice(&assignment.first_sequence.to_be_bytes());
            value.extend_from_slice(&assignment.packet_count.to_be_bytes());
            value.extend_from_slice(&assignment.path_id.to_be_bytes());
            Ok((TYPE_PATH_ASSIGNMENT, value))
        }
        ControlMessage::MissingRange(range) => {
            let mut value = Vec::with_capacity(12);
            value.extend_from_slice(&range.first_sequence.to_be_bytes());
            value.extend_from_slice(&range.packet_count.to_be_bytes());
            Ok((TYPE_MISSING_RANGE, value))
        }
        ControlMessage::PathMetadata(metadata) => {
            let mut value = Vec::with_capacity(3 + metadata.name.len());
            value.extend_from_slice(&metadata.path_id.to_be_bytes());
            value.push(metadata.name.len() as u8);
            value.extend_from_slice(metadata.name.as_bytes());
            Ok((TYPE_PATH_METADATA, value))
        }
        ControlMessage::PathCapacity(capacity) => {
            let mut value = Vec::with_capacity(18);
            value.extend_from_slice(&capacity.path_id.to_be_bytes());
            value.extend_from_slice(&capacity.tx_bps.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&capacity.rx_bps.unwrap_or(0).to_be_bytes());
            Ok((TYPE_PATH_CAPACITY, value))
        }
        ControlMessage::PathLatency(latency) => {
            let mut value = Vec::with_capacity(18);
            value.extend_from_slice(&latency.path_id.to_be_bytes());
            value.extend_from_slice(&latency.tx_current_us.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&latency.tx_mean_us.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&latency.rx_current_us.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&latency.rx_mean_us.unwrap_or(0).to_be_bytes());
            Ok((TYPE_PATH_LATENCY, value))
        }
        ControlMessage::PathMtu(mtu) => {
            let mut value = Vec::with_capacity(10);
            value.extend_from_slice(&mtu.path_id.to_be_bytes());
            value.extend_from_slice(&mtu.tx_link_mtu.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&mtu.tx_frame_mtu.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&mtu.rx_link_mtu.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&mtu.rx_frame_mtu.unwrap_or(0).to_be_bytes());
            Ok((TYPE_PATH_MTU, value))
        }
        ControlMessage::InternalClockSync(sync) => {
            let mut value = Vec::with_capacity(35);
            value.extend_from_slice(&sync.exchange_id.to_be_bytes());
            value.extend_from_slice(&sync.path_id.to_be_bytes());
            value.push(sync.mode.as_u8());
            value.extend_from_slice(&sync.origin_us.to_be_bytes());
            value.extend_from_slice(&sync.receive_us.unwrap_or(0).to_be_bytes());
            value.extend_from_slice(&sync.transmit_us.unwrap_or(0).to_be_bytes());
            Ok((TYPE_INTERNAL_CLOCK_SYNC, value))
        }
        ControlMessage::SinkTime(sink_time) => {
            let mut value = Vec::with_capacity(19);
            value.extend_from_slice(&sink_time.path_id.to_be_bytes());
            value.extend_from_slice(&sink_time.sink_unix_us.to_be_bytes());
            value.extend_from_slice(&sink_time.sink_internal_us.to_be_bytes());
            value.push(sink_time.ntp_state.as_u8());
            Ok((TYPE_SINK_TIME, value))
        }
        ControlMessage::ServiceMetadata(metadata) => {
            let mut value = Vec::with_capacity(3 + metadata.name.len());
            value.extend_from_slice(&metadata.service_id.to_be_bytes());
            value.push(metadata.name.len() as u8);
            value.extend_from_slice(metadata.name.as_bytes());
            Ok((TYPE_SERVICE_METADATA, value))
        }
        ControlMessage::ServiceEndpointAssertion(assertion) => {
            let mut value = Vec::with_capacity(3 + assertion.target.len());
            value.extend_from_slice(&assertion.service_id.to_be_bytes());
            value.push(assertion.target.len() as u8);
            value.extend_from_slice(assertion.target.as_bytes());
            Ok((TYPE_SERVICE_ENDPOINT_ASSERTION, value))
        }
        ControlMessage::ServiceDisable(disable) => {
            let mut value = Vec::with_capacity(3 + disable.reason.len());
            value.extend_from_slice(&disable.service_id.to_be_bytes());
            value.push(disable.reason.len() as u8);
            value.extend_from_slice(disable.reason.as_bytes());
            Ok((TYPE_SERVICE_DISABLE, value))
        }
        ControlMessage::ServiceSchedulerPolicy(policy) => {
            let mut value = Vec::with_capacity(8);
            value.extend_from_slice(&policy.service_id.to_be_bytes());
            value.extend_from_slice(&policy.fanout.to_be_bytes());
            value.extend_from_slice(&policy.fanout_below_bytes.to_be_bytes());
            Ok((TYPE_SERVICE_SCHEDULER_POLICY, value))
        }
    }
}

fn decode_message(message_type: u8, value: &[u8]) -> Result<ControlMessage, ProtocolError> {
    match message_type {
        TYPE_PATH_ASSIGNMENT => {
            if value.len() != 14 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::PathAssignment(PathAssignment::new(
                read_u64(value, 0),
                read_u32(value, 8),
                read_u16(value, 12),
            )?))
        }
        TYPE_MISSING_RANGE => {
            if value.len() != 12 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::MissingRange(MissingRange::new(
                read_u64(value, 0),
                read_u32(value, 8),
            )?))
        }
        TYPE_PATH_METADATA => {
            if value.len() < 3 {
                return Err(ProtocolError::MalformedControl);
            }
            let name_len = usize::from(value[2]);
            if value.len() != 3 + name_len {
                return Err(ProtocolError::MalformedControl);
            }
            let name = std::str::from_utf8(&value[3..]).map_err(|_| ProtocolError::MalformedControl)?;
            Ok(ControlMessage::PathMetadata(PathMetadata::new(
                read_u16(value, 0),
                name,
            )?))
        }
        TYPE_PATH_CAPACITY => {
            if value.len() != 18 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::PathCapacity(PathCapacity::new(
                read_u16(value, 0),
                optional_u64(read_u64(value, 2)),
                optional_u64(read_u64(value, 10)),
            )?))
        }
        TYPE_PATH_LATENCY => {
            if value.len() != 18 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::PathLatency(PathLatency::new(
                read_u16(value, 0),
                optional_u32(read_u32(value, 2)),
                optional_u32(read_u32(value, 6)),
                optional_u32(read_u32(value, 10)),
                optional_u32(read_u32(value, 14)),
            )?))
        }
        TYPE_PATH_MTU => {
            if value.len() != 10 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::PathMtu(PathMtu::new(
                read_u16(value, 0),
                optional_u16(read_u16(value, 2)),
                optional_u16(read_u16(value, 4)),
                optional_u16(read_u16(value, 6)),
                optional_u16(read_u16(value, 8)),
            )?))
        }
        TYPE_INTERNAL_CLOCK_SYNC => {
            if value.len() != 35 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::InternalClockSync(InternalClockSync::new(
                read_u64(value, 0),
                read_u16(value, 8),
                ClockSyncMode::from_u8(value[10])?,
                read_u64(value, 11),
                optional_u64(read_u64(value, 19)),
                optional_u64(read_u64(value, 27)),
            )?))
        }
        TYPE_SINK_TIME => {
            if value.len() != 19 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::SinkTime(SinkTime::new(
                read_u16(value, 0),
                read_u64(value, 2),
                read_u64(value, 10),
                NtpState::from_u8(value[18])?,
            )?))
        }
        TYPE_SERVICE_METADATA => {
            if value.len() < 3 {
                return Err(ProtocolError::MalformedControl);
            }
            let name_len = usize::from(value[2]);
            if value.len() != 3 + name_len {
                return Err(ProtocolError::MalformedControl);
            }
            let name = std::str::from_utf8(&value[3..]).map_err(|_| ProtocolError::MalformedControl)?;
            Ok(ControlMessage::ServiceMetadata(ServiceMetadata::new(
                read_u16(value, 0),
                name,
            )?))
        }
        TYPE_SERVICE_ENDPOINT_ASSERTION => {
            if value.len() < 3 {
                return Err(ProtocolError::MalformedControl);
            }
            let target_len = usize::from(value[2]);
            if value.len() != 3 + target_len {
                return Err(ProtocolError::MalformedControl);
            }
            let target = std::str::from_utf8(&value[3..]).map_err(|_| ProtocolError::MalformedControl)?;
            Ok(ControlMessage::ServiceEndpointAssertion(ServiceEndpointAssertion::new(
                read_u16(value, 0),
                target,
            )?))
        }
        TYPE_SERVICE_DISABLE => {
            if value.len() < 3 {
                return Err(ProtocolError::MalformedControl);
            }
            let reason_len = usize::from(value[2]);
            if value.len() != 3 + reason_len {
                return Err(ProtocolError::MalformedControl);
            }
            let reason = std::str::from_utf8(&value[3..]).map_err(|_| ProtocolError::MalformedControl)?;
            Ok(ControlMessage::ServiceDisable(ServiceDisable::new(
                read_u16(value, 0),
                reason,
            )?))
        }
        TYPE_SERVICE_SCHEDULER_POLICY => {
            if value.len() != 8 {
                return Err(ProtocolError::MalformedControl);
            }
            Ok(ControlMessage::ServiceSchedulerPolicy(ServiceSchedulerPolicy::new(
                read_u16(value, 0),
                read_u16(value, 2),
                read_u32(value, 4),
            )?))
        }
        _ => Err(ProtocolError::MalformedControl),
    }
}

fn optional_u32(value: u32) -> Option<u32> {
    if value == 0 {
        None
    } else {
        Some(value)
    }
}

fn optional_u16(value: u16) -> Option<u16> {
    if value == 0 {
        None
    } else {
        Some(value)
    }
}

fn optional_u64(value: u64) -> Option<u64> {
    if value == 0 {
        None
    } else {
        Some(value)
    }
}

fn valid_mtu_pair(link_mtu: Option<u16>, frame_mtu: Option<u16>) -> bool {
    match (link_mtu, frame_mtu) {
        (None, None) => true,
        (Some(link_mtu), Some(frame_mtu)) => frame_mtu > 0 && frame_mtu <= link_mtu,
        _ => false,
    }
}

fn read_u16(input: &[u8], offset: usize) -> u16 {
    u16::from_be_bytes([input[offset], input[offset + 1]])
}

fn read_u32(input: &[u8], offset: usize) -> u32 {
    u32::from_be_bytes([input[offset], input[offset + 1], input[offset + 2], input[offset + 3]])
}

fn read_u64(input: &[u8], offset: usize) -> u64 {
    u64::from_be_bytes([
        input[offset],
        input[offset + 1],
        input[offset + 2],
        input[offset + 3],
        input[offset + 4],
        input[offset + 5],
        input[offset + 6],
        input[offset + 7],
    ])
}
