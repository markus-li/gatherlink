//! Versioned Gatherlink UDP frame format.
//!
//! The v1 frame deliberately models the durable protocol concepts before their
//! full implementations exist: frame class, flags, session/service/path/route
//! IDs, sequence number, and MTU-relevant payload length. Optional extension
//! headers cost zero bytes unless a frame actually carries them. Authentication/encryption wraps this context later;
//! unauthenticated public listeners must still silently drop invalid frames.

use crate::errors::ProtocolError;
use crate::ids::{PathId, RouteId, SequenceNumber, ServiceId, SessionId};
use crate::version::PROTOCOL_VERSION;

/// Current fixed header length in bytes.
pub const V1_HEADER_LEN: usize = 38;

/// Max v1 payload length before future fragmentation is needed.
pub const MAX_V1_PAYLOAD_LEN: usize = u16::MAX as usize;

/// Fragment extension length including the compact TLV header.
pub const FRAGMENT_EXTENSION_LEN: usize = 12;

const KIND_DATA: u8 = 1;
const KIND_CONTROL: u8 = 2;
const KIND_BATCH: u8 = 3;
const EXT_FRAGMENT: u8 = 1;
const EXT_FRAGMENT_VALUE_LEN: u8 = 10;
const KNOWN_FLAGS: u16 = 0;

/// Broad frame class carried over UDP.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum FrameKind {
    Data,
    Control,
    Batch,
}

impl FrameKind {
    fn to_u8(self) -> u8 {
        match self {
            Self::Data => KIND_DATA,
            Self::Control => KIND_CONTROL,
            Self::Batch => KIND_BATCH,
        }
    }

    fn from_u8(value: u8) -> Result<Self, ProtocolError> {
        match value {
            KIND_DATA => Ok(Self::Data),
            KIND_CONTROL => Ok(Self::Control),
            KIND_BATCH => Ok(Self::Batch),
            other => Err(ProtocolError::UnknownFrameKind(other)),
        }
    }
}

/// Flags reserved for protocol behavior.
#[derive(Debug, Copy, Clone, PartialEq, Eq, Default)]
pub struct FrameFlags {
    bits: u16,
}

impl FrameFlags {
    /// Build flags from raw bits, rejecting values v1 does not understand.
    pub fn from_bits(bits: u16) -> Result<Self, ProtocolError> {
        if bits & !KNOWN_FLAGS != 0 {
            return Err(ProtocolError::UnknownFlags(bits));
        }
        Ok(Self { bits })
    }

    /// Return raw flag bits for encoding.
    pub fn bits(self) -> u16 {
        self.bits
    }
}

/// Fixed compact v1 header.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameHeader {
    pub version: u8,
    pub kind: FrameKind,
    pub header_len: u16,
    pub flags: FrameFlags,
    pub session_id: SessionId,
    pub service_id: ServiceId,
    pub path_id: PathId,
    pub route_id: RouteId,
    pub sequence: SequenceNumber,
    pub payload_len: u16,
}

/// Fragment metadata carried only when one virtual UDP payload must be split.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub struct FragmentInfo {
    pub datagram_id: u32,
    pub fragment_index: u16,
    pub fragment_count: u16,
    pub original_len: u16,
}

impl FragmentInfo {
    /// Build validated fragment metadata.
    pub fn new(
        datagram_id: u32,
        fragment_index: u16,
        fragment_count: u16,
        original_len: usize,
    ) -> Result<Self, ProtocolError> {
        if fragment_count == 0 || fragment_index >= fragment_count || original_len > u16::MAX as usize {
            return Err(ProtocolError::InvalidFragment);
        }

        Ok(Self {
            datagram_id,
            fragment_index,
            fragment_count,
            original_len: original_len as u16,
        })
    }
}

impl FrameHeader {
    /// Build a frame header for one protocol payload.
    pub fn new(
        kind: FrameKind,
        session_id: SessionId,
        service_id: ServiceId,
        path_id: PathId,
        route_id: RouteId,
        sequence: SequenceNumber,
        payload_len: usize,
    ) -> Result<Self, ProtocolError> {
        if payload_len > MAX_V1_PAYLOAD_LEN {
            return Err(ProtocolError::PayloadTooLarge(payload_len));
        }

        Ok(Self {
            version: PROTOCOL_VERSION,
            kind,
            header_len: V1_HEADER_LEN as u16,
            flags: FrameFlags::default(),
            session_id,
            service_id,
            path_id,
            route_id,
            sequence,
            payload_len: payload_len as u16,
        })
    }

    /// Build a data-frame header for one virtual UDP payload.
    pub fn data(
        session_id: SessionId,
        service_id: ServiceId,
        path_id: PathId,
        route_id: RouteId,
        sequence: SequenceNumber,
        payload_len: usize,
    ) -> Result<Self, ProtocolError> {
        Self::new(
            FrameKind::Data,
            session_id,
            service_id,
            path_id,
            route_id,
            sequence,
            payload_len,
        )
    }

    /// Encode the fixed header into a caller-provided buffer.
    pub fn encode_into(&self, output: &mut [u8]) -> Result<(), ProtocolError> {
        if output.len() < V1_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }

        output[..V1_HEADER_LEN].fill(0);
        output[0] = self.version;
        output[1] = self.kind.to_u8();
        write_u16(output, 2, self.header_len);
        write_u16(output, 4, self.flags.bits());
        write_u128(output, 6, self.session_id);
        write_u16(output, 22, self.service_id);
        write_u16(output, 24, self.path_id);
        write_u16(output, 26, self.route_id);
        write_u64(output, 28, self.sequence);
        write_u16(output, 36, self.payload_len);
        Ok(())
    }

    /// Decode and validate a fixed v1 header.
    pub fn decode(input: &[u8]) -> Result<Self, ProtocolError> {
        if input.len() < V1_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }

        let version = input[0];
        if version != PROTOCOL_VERSION {
            return Err(ProtocolError::UnsupportedVersion(version));
        }

        let header_len = read_u16(input, 2);
        if usize::from(header_len) < V1_HEADER_LEN {
            return Err(ProtocolError::HeaderLengthMismatch {
                expected: V1_HEADER_LEN,
                actual: usize::from(header_len),
            });
        }

        let flags = FrameFlags::from_bits(read_u16(input, 4))?;

        Ok(Self {
            version,
            kind: FrameKind::from_u8(input[1])?,
            header_len,
            flags,
            session_id: read_u128(input, 6),
            service_id: read_u16(input, 22),
            path_id: read_u16(input, 24),
            route_id: read_u16(input, 26),
            sequence: read_u64(input, 28),
            payload_len: read_u16(input, 36),
        })
    }
}

/// Owned frame with header and payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub header: FrameHeader,
    pub extensions: Vec<u8>,
    pub payload: Vec<u8>,
}

impl Frame {
    /// Build a data frame around a virtual UDP payload.
    pub fn data(
        session_id: SessionId,
        service_id: ServiceId,
        path_id: PathId,
        route_id: RouteId,
        sequence: SequenceNumber,
        payload: Vec<u8>,
    ) -> Result<Self, ProtocolError> {
        let header = FrameHeader::data(session_id, service_id, path_id, route_id, sequence, payload.len())?;
        Ok(Self {
            header,
            extensions: Vec::new(),
            payload,
        })
    }

    /// Build a batch frame containing multiple virtual UDP payloads for one path.
    ///
    /// Batch frames keep byte usage low for many small UDP datagrams. The frame
    /// header sequence is the first payload's sequence number; each following
    /// item is implicitly `sequence + index` in payload order.
    pub fn batch(
        session_id: SessionId,
        service_id: ServiceId,
        path_id: PathId,
        route_id: RouteId,
        sequence: SequenceNumber,
        payloads: &[Vec<u8>],
    ) -> Result<Self, ProtocolError> {
        let payload = encode_batch_payload(payloads)?;
        let header = FrameHeader::new(
            FrameKind::Batch,
            session_id,
            service_id,
            path_id,
            route_id,
            sequence,
            payload.len(),
        )?;
        Ok(Self {
            header,
            extensions: Vec::new(),
            payload,
        })
    }

    /// Build a control metaband frame.
    ///
    /// Control payloads carry telemetry and future safe state changes without
    /// increasing the fixed data header. They may be batched or sent sparsely by
    /// higher layers.
    pub fn control(
        session_id: SessionId,
        service_id: ServiceId,
        path_id: PathId,
        route_id: RouteId,
        sequence: SequenceNumber,
        payload: Vec<u8>,
    ) -> Result<Self, ProtocolError> {
        let header = FrameHeader::new(
            FrameKind::Control,
            session_id,
            service_id,
            path_id,
            route_id,
            sequence,
            payload.len(),
        )?;
        Ok(Self {
            header,
            extensions: Vec::new(),
            payload,
        })
    }

    /// Build one data-frame fragment for a virtual UDP payload that does not fit one path MTU.
    pub fn fragment(
        session_id: SessionId,
        service_id: ServiceId,
        path_id: PathId,
        route_id: RouteId,
        sequence: SequenceNumber,
        fragment: FragmentInfo,
        payload: Vec<u8>,
    ) -> Result<Self, ProtocolError> {
        let mut frame = Self::data(session_id, service_id, path_id, route_id, sequence, payload)?;
        frame.extensions = encode_fragment_extension(fragment);
        Ok(frame)
    }

    /// Decode this frame's batch payload into individual virtual UDP payloads.
    pub fn batch_payloads(&self) -> Result<Vec<Vec<u8>>, ProtocolError> {
        if self.header.kind != FrameKind::Batch {
            return Err(ProtocolError::UnknownFrameKind(self.header.kind.to_u8()));
        }

        decode_batch_payload(&self.payload)
    }

    /// Return fragment metadata when this frame carries the v1 fragment extension.
    pub fn fragment_info(&self) -> Result<Option<FragmentInfo>, ProtocolError> {
        decode_fragment_extension(&self.extensions)
    }

    /// Encode this frame into wire bytes.
    pub fn encode(&self) -> Result<Vec<u8>, ProtocolError> {
        if self.payload.len() != self.header.payload_len as usize {
            return Err(ProtocolError::PayloadLengthMismatch {
                expected: self.header.payload_len as usize,
                actual: self.payload.len(),
            });
        }

        let header_len = V1_HEADER_LEN + self.extensions.len();
        if header_len > u16::MAX as usize {
            return Err(ProtocolError::HeaderTooLarge(header_len));
        }

        let mut header = self.header.clone();
        header.header_len = header_len as u16;

        let mut output = vec![0_u8; header_len + self.payload.len()];
        header.encode_into(&mut output[..V1_HEADER_LEN])?;
        output[V1_HEADER_LEN..header_len].copy_from_slice(&self.extensions);
        output[header_len..].copy_from_slice(&self.payload);
        Ok(output)
    }

    /// Decode one complete frame from wire bytes.
    pub fn decode(input: &[u8]) -> Result<Self, ProtocolError> {
        let header = FrameHeader::decode(input)?;
        let header_len = usize::from(header.header_len);
        if input.len() < header_len {
            return Err(ProtocolError::BufferTooSmall);
        }

        let expected = header_len + header.payload_len as usize;
        if input.len() != expected {
            return Err(ProtocolError::PayloadLengthMismatch {
                expected,
                actual: input.len(),
            });
        }
        Ok(Self {
            header,
            extensions: input[V1_HEADER_LEN..header_len].to_vec(),
            payload: input[header_len..].to_vec(),
        })
    }
}

fn encode_fragment_extension(fragment: FragmentInfo) -> Vec<u8> {
    let mut output = Vec::with_capacity(FRAGMENT_EXTENSION_LEN);
    output.push(EXT_FRAGMENT);
    output.push(EXT_FRAGMENT_VALUE_LEN);
    output.extend_from_slice(&fragment.datagram_id.to_be_bytes());
    output.extend_from_slice(&fragment.fragment_index.to_be_bytes());
    output.extend_from_slice(&fragment.fragment_count.to_be_bytes());
    output.extend_from_slice(&fragment.original_len.to_be_bytes());
    output
}

fn decode_fragment_extension(input: &[u8]) -> Result<Option<FragmentInfo>, ProtocolError> {
    let mut cursor = 0_usize;
    let mut fragment = None;
    while cursor < input.len() {
        if cursor + 2 > input.len() {
            return Err(ProtocolError::MalformedExtension);
        }

        let extension_type = input[cursor];
        let extension_len = usize::from(input[cursor + 1]);
        cursor += 2;
        if cursor + extension_len > input.len() {
            return Err(ProtocolError::MalformedExtension);
        }

        if extension_type == EXT_FRAGMENT {
            if extension_len != usize::from(EXT_FRAGMENT_VALUE_LEN) || fragment.is_some() {
                return Err(ProtocolError::MalformedExtension);
            }
            let datagram_id =
                u32::from_be_bytes([input[cursor], input[cursor + 1], input[cursor + 2], input[cursor + 3]]);
            let fragment_index = read_u16(input, cursor + 4);
            let fragment_count = read_u16(input, cursor + 6);
            let original_len = read_u16(input, cursor + 8);
            fragment = Some(FragmentInfo::new(
                datagram_id,
                fragment_index,
                fragment_count,
                usize::from(original_len),
            )?);
        }

        cursor += extension_len;
    }

    Ok(fragment)
}

fn encode_batch_payload(payloads: &[Vec<u8>]) -> Result<Vec<u8>, ProtocolError> {
    if payloads.is_empty() {
        return Err(ProtocolError::EmptyBatch);
    }
    if payloads.len() > u16::MAX as usize {
        return Err(ProtocolError::BatchTooLarge(payloads.len()));
    }

    let mut total_len = 2_usize;
    for payload in payloads {
        if payload.len() > u16::MAX as usize {
            return Err(ProtocolError::BatchItemTooLarge(payload.len()));
        }
        total_len += 2 + payload.len();
        if total_len > MAX_V1_PAYLOAD_LEN {
            return Err(ProtocolError::BatchTooLarge(total_len));
        }
    }

    let mut output = Vec::with_capacity(total_len);
    output.extend_from_slice(&(payloads.len() as u16).to_be_bytes());
    for payload in payloads {
        output.extend_from_slice(&(payload.len() as u16).to_be_bytes());
        output.extend_from_slice(payload);
    }
    Ok(output)
}

fn decode_batch_payload(input: &[u8]) -> Result<Vec<Vec<u8>>, ProtocolError> {
    if input.len() < 2 {
        return Err(ProtocolError::MalformedBatch);
    }

    let item_count = usize::from(read_u16(input, 0));
    if item_count == 0 {
        return Err(ProtocolError::EmptyBatch);
    }

    let mut cursor = 2_usize;
    let mut payloads = Vec::with_capacity(item_count);
    for _ in 0..item_count {
        if cursor + 2 > input.len() {
            return Err(ProtocolError::MalformedBatch);
        }
        let item_len = usize::from(read_u16(input, cursor));
        cursor += 2;
        if cursor + item_len > input.len() {
            return Err(ProtocolError::MalformedBatch);
        }
        payloads.push(input[cursor..cursor + item_len].to_vec());
        cursor += item_len;
    }

    if cursor != input.len() {
        return Err(ProtocolError::MalformedBatch);
    }

    Ok(payloads)
}

fn write_u16(output: &mut [u8], offset: usize, value: u16) {
    output[offset..offset + 2].copy_from_slice(&value.to_be_bytes());
}

fn write_u64(output: &mut [u8], offset: usize, value: u64) {
    output[offset..offset + 8].copy_from_slice(&value.to_be_bytes());
}

fn write_u128(output: &mut [u8], offset: usize, value: u128) {
    output[offset..offset + 16].copy_from_slice(&value.to_be_bytes());
}

fn read_u16(input: &[u8], offset: usize) -> u16 {
    u16::from_be_bytes([input[offset], input[offset + 1]])
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

fn read_u128(input: &[u8], offset: usize) -> u128 {
    u128::from_be_bytes([
        input[offset],
        input[offset + 1],
        input[offset + 2],
        input[offset + 3],
        input[offset + 4],
        input[offset + 5],
        input[offset + 6],
        input[offset + 7],
        input[offset + 8],
        input[offset + 9],
        input[offset + 10],
        input[offset + 11],
        input[offset + 12],
        input[offset + 13],
        input[offset + 14],
        input[offset + 15],
    ])
}
