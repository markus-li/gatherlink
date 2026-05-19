//! Compact Gatherlink UDP frame format.
//!
//! Plaintext/lab traffic uses compact v1, which carries a visible version byte.
//! Secure transport encrypts compact v2, which is the same logical frame without
//! that visible version byte. The richer [`FrameHeader`] fields are a synthesized
//! compatibility view for older engine code while the dataplane migrates.

use crate::errors::ProtocolError;
use crate::ids::{PathId, RouteId, SequenceNumber, ServiceId, SessionId};
use crate::version::PROTOCOL_VERSION;

/// Compact v1 plaintext header length in bytes.
pub const V1_HEADER_LEN: usize = 14;

/// Compact v2 decrypted secure header length in bytes.
pub const V2_HEADER_LEN: usize = 13;

/// Max v1 payload length before future fragmentation is needed.
pub const MAX_V1_PAYLOAD_LEN: usize = u16::MAX as usize;

/// Fixed fragment metadata length after a compact v1/v2 header.
pub const FRAGMENT_METADATA_LEN: usize = 10;

/// Compatibility alias for code that still calls the fragment metadata an extension.
pub const FRAGMENT_EXTENSION_LEN: usize = FRAGMENT_METADATA_LEN;

const KIND_DATA: u8 = 0;
const KIND_CONTROL: u8 = 1;
const KIND_BATCH: u8 = 2;
const KIND_MASK: u8 = 0b0000_0011;
const FLAG_FRAGMENT_PRESENT: u8 = 0b0000_0100;
const KIND_FLAGS_RESERVED_MASK: u8 = 0b1111_1000;
const KNOWN_FLAGS: u16 = 0;

/// Broad frame class carried over UDP.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum FrameKind {
    Data,
    Control,
    Batch,
}

impl FrameKind {
    /// Return this frame kind's compact flag bits.
    pub fn to_u8(self) -> u8 {
        match self {
            Self::Data => KIND_DATA,
            Self::Control => KIND_CONTROL,
            Self::Batch => KIND_BATCH,
        }
    }

    /// Parse compact kind bits into a frame kind.
    pub fn from_u8(value: u8) -> Result<Self, ProtocolError> {
        match value {
            KIND_DATA => Ok(Self::Data),
            KIND_CONTROL => Ok(Self::Control),
            KIND_BATCH => Ok(Self::Batch),
            other => Err(ProtocolError::UnknownFrameKind(other)),
        }
    }
}

/// Compatibility flags reserved for protocol behavior.
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

    /// Encode the compact v1 header into a caller-provided buffer.
    pub fn encode_into(&self, output: &mut [u8]) -> Result<(), ProtocolError> {
        if output.len() < V1_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }

        output[..V1_HEADER_LEN].fill(0);
        output[0] = self.version;
        output[1] = self.kind.to_u8();
        write_u16(output, 2, self.service_id);
        write_u16(output, 4, self.path_id);
        write_u64(output, 6, self.sequence);
        Ok(())
    }

    fn decode_v1(input: &[u8], has_fragment: bool, payload_len: usize) -> Result<Self, ProtocolError> {
        let version = *input.first().ok_or(ProtocolError::BufferTooSmall)?;
        if version != PROTOCOL_VERSION {
            return Err(ProtocolError::UnsupportedVersion(version));
        }
        Self::decode_logical(version, input[1], &input[2..], has_fragment, payload_len, V1_HEADER_LEN)
    }

    fn decode_v2(input: &[u8], has_fragment: bool, payload_len: usize) -> Result<Self, ProtocolError> {
        Self::decode_logical(
            PROTOCOL_VERSION,
            input[0],
            &input[1..],
            has_fragment,
            payload_len,
            V2_HEADER_LEN,
        )
    }

    fn decode_logical(
        version: u8,
        kind_flags: u8,
        fields: &[u8],
        has_fragment: bool,
        payload_len: usize,
        base_header_len: usize,
    ) -> Result<Self, ProtocolError> {
        if kind_flags & KIND_FLAGS_RESERVED_MASK != 0 {
            return Err(ProtocolError::UnknownFlags(u16::from(
                kind_flags & KIND_FLAGS_RESERVED_MASK,
            )));
        }
        if payload_len > u16::MAX as usize {
            return Err(ProtocolError::PayloadTooLarge(payload_len));
        }
        Ok(Self {
            version,
            kind: FrameKind::from_u8(kind_flags & KIND_MASK)?,
            header_len: (base_header_len + if has_fragment { FRAGMENT_METADATA_LEN } else { 0 }) as u16,
            flags: FrameFlags::default(),
            session_id: 0,
            service_id: read_u16(fields, 0),
            path_id: read_u16(fields, 2),
            route_id: 0,
            sequence: read_u64(fields, 4),
            payload_len: payload_len as u16,
        })
    }
}

/// Owned frame with header and payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub header: FrameHeader,
    pub fragment: Option<FragmentInfo>,
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
            fragment: None,
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
            fragment: None,
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
            fragment: None,
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
        frame.header.header_len = (V1_HEADER_LEN + FRAGMENT_METADATA_LEN) as u16;
        frame.fragment = Some(fragment);
        Ok(frame)
    }

    /// Decode this frame's batch payload into individual virtual UDP payloads.
    pub fn batch_payloads(&self) -> Result<Vec<Vec<u8>>, ProtocolError> {
        if self.header.kind != FrameKind::Batch {
            return Err(ProtocolError::UnknownFrameKind(self.header.kind.to_u8()));
        }

        decode_batch_payload(&self.payload)
    }

    /// Return fragment metadata when this frame carries fixed compact fragment metadata.
    pub fn fragment_info(&self) -> Result<Option<FragmentInfo>, ProtocolError> {
        Ok(self.fragment)
    }

    /// Encode this frame into compact v1 wire bytes.
    pub fn encode(&self) -> Result<Vec<u8>, ProtocolError> {
        self.encode_v1()
    }

    /// Encode this frame into compact v1 plaintext wire bytes.
    pub fn encode_v1(&self) -> Result<Vec<u8>, ProtocolError> {
        if self.payload.len() != self.header.payload_len as usize {
            return Err(ProtocolError::PayloadLengthMismatch {
                expected: self.header.payload_len as usize,
                actual: self.payload.len(),
            });
        }

        let header_len = V1_HEADER_LEN + self.fragment.map_or(0, |_| FRAGMENT_METADATA_LEN);
        let mut output = vec![0_u8; header_len + self.payload.len()];
        let mut header = self.header.clone();
        header.header_len = header_len as u16;
        header.encode_into(&mut output[..V1_HEADER_LEN])?;
        if let Some(fragment) = self.fragment {
            output[1] |= FLAG_FRAGMENT_PRESENT;
            encode_fragment_metadata(fragment, &mut output[V1_HEADER_LEN..header_len]);
        }
        output[header_len..].copy_from_slice(&self.payload);
        Ok(output)
    }

    /// Encode this frame into compact v2 bytes for AEAD plaintext.
    pub fn encode_v2(&self) -> Result<Vec<u8>, ProtocolError> {
        if self.payload.len() != self.header.payload_len as usize {
            return Err(ProtocolError::PayloadLengthMismatch {
                expected: self.header.payload_len as usize,
                actual: self.payload.len(),
            });
        }

        let header_len = V2_HEADER_LEN + self.fragment.map_or(0, |_| FRAGMENT_METADATA_LEN);
        let mut output = vec![0_u8; header_len + self.payload.len()];
        output[0] = self.header.kind.to_u8();
        if self.fragment.is_some() {
            output[0] |= FLAG_FRAGMENT_PRESENT;
        }
        write_u16(&mut output, 1, self.header.service_id);
        write_u16(&mut output, 3, self.header.path_id);
        write_u64(&mut output, 5, self.header.sequence);
        if let Some(fragment) = self.fragment {
            encode_fragment_metadata(fragment, &mut output[V2_HEADER_LEN..header_len]);
        }
        output[header_len..].copy_from_slice(&self.payload);
        Ok(output)
    }

    /// Decode one complete compact v1 frame from wire bytes.
    pub fn decode(input: &[u8]) -> Result<Self, ProtocolError> {
        Self::decode_v1(input)
    }

    /// Decode one complete compact v1 frame from wire bytes.
    pub fn decode_v1(input: &[u8]) -> Result<Self, ProtocolError> {
        if input.len() < V1_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }
        let has_fragment = parse_kind_flags(input[1])?;
        let header_len = V1_HEADER_LEN + if has_fragment { FRAGMENT_METADATA_LEN } else { 0 };
        if input.len() < header_len {
            return Err(ProtocolError::BufferTooSmall);
        }
        let fragment = if has_fragment {
            Some(decode_fragment_metadata(&input[V1_HEADER_LEN..header_len])?)
        } else {
            None
        };
        let payload_len = input.len() - header_len;
        let header = FrameHeader::decode_v1(input, has_fragment, payload_len)?;
        Ok(Self {
            header,
            fragment,
            payload: input[header_len..].to_vec(),
        })
    }

    /// Decode one complete compact v2 AEAD plaintext frame.
    pub fn decode_v2(input: &[u8]) -> Result<Self, ProtocolError> {
        if input.len() < V2_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }
        let has_fragment = parse_kind_flags(input[0])?;
        let header_len = V2_HEADER_LEN + if has_fragment { FRAGMENT_METADATA_LEN } else { 0 };
        if input.len() < header_len {
            return Err(ProtocolError::BufferTooSmall);
        }
        let fragment = if has_fragment {
            Some(decode_fragment_metadata(&input[V2_HEADER_LEN..header_len])?)
        } else {
            None
        };
        let payload_len = input.len() - header_len;
        let header = FrameHeader::decode_v2(input, has_fragment, payload_len)?;
        Ok(Self {
            header,
            fragment,
            payload: input[header_len..].to_vec(),
        })
    }
}

fn parse_kind_flags(kind_flags: u8) -> Result<bool, ProtocolError> {
    if kind_flags & KIND_FLAGS_RESERVED_MASK != 0 {
        return Err(ProtocolError::UnknownFlags(u16::from(
            kind_flags & KIND_FLAGS_RESERVED_MASK,
        )));
    }
    FrameKind::from_u8(kind_flags & KIND_MASK)?;
    Ok(kind_flags & FLAG_FRAGMENT_PRESENT != 0)
}

fn encode_fragment_metadata(fragment: FragmentInfo, output: &mut [u8]) {
    output[..4].copy_from_slice(&fragment.datagram_id.to_be_bytes());
    output[4..6].copy_from_slice(&fragment.fragment_index.to_be_bytes());
    output[6..8].copy_from_slice(&fragment.fragment_count.to_be_bytes());
    output[8..10].copy_from_slice(&fragment.original_len.to_be_bytes());
}

fn decode_fragment_metadata(input: &[u8]) -> Result<FragmentInfo, ProtocolError> {
    if input.len() != FRAGMENT_METADATA_LEN {
        return Err(ProtocolError::MalformedExtension);
    }
    let datagram_id = u32::from_be_bytes([input[0], input[1], input[2], input[3]]);
    let fragment_index = read_u16(input, 4);
    let fragment_count = read_u16(input, 6);
    let original_len = read_u16(input, 8);
    FragmentInfo::new(datagram_id, fragment_index, fragment_count, usize::from(original_len))
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
