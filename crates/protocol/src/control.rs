//! Extensible control metaband payloads.
//!
//! Data frames keep the fixed v1 header compact. This module carries sparse
//! peer telemetry and future safe control data, such as path assignment reports,
//! missing sequence ranges, path metadata, and later config-change proposals.

use crate::errors::ProtocolError;
use crate::ids::{PathId, SequenceNumber};

/// Current control metaband payload version.
pub const CONTROL_PAYLOAD_VERSION: u8 = 1;

const TYPE_PATH_ASSIGNMENT: u8 = 1;
const TYPE_MISSING_RANGE: u8 = 2;
const TYPE_PATH_METADATA: u8 = 3;
const TYPE_PATH_CAPACITY: u8 = 4;
const TYPE_PATH_LATENCY: u8 = 5;
const TYPE_INTERNAL_CLOCK_SYNC: u8 = 6;
const TYPE_SINK_TIME: u8 = 7;

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
    /// NTP-style peer-relative internal clock exchange.
    InternalClockSync(InternalClockSync),
    /// Sink-authoritative wall-clock and NTP status for diagnostics and policy.
    SinkTime(SinkTime),
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

fn optional_u64(value: u64) -> Option<u64> {
    if value == 0 {
        None
    } else {
        Some(value)
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
