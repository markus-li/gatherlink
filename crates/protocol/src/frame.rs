//! Compact Gatherlink UDP frame format.
//!
//! Plaintext/lab traffic uses compact v1, which carries a visible version byte.
//! Secure transport encrypts compact v2, which is the same logical frame without
//! that visible version byte. This module deliberately models only the compact
//! fields that exist on the wire.

use crate::errors::ProtocolError;
use crate::ids::{PathId, SequenceNumber, ServiceId};
use crate::version::PROTOCOL_VERSION;

/// Compact v1 plaintext header length in bytes.
pub const V1_HEADER_LEN: usize = 14;

/// Compact v2 decrypted secure header length in bytes.
pub const V2_HEADER_LEN: usize = 13;

/// Maximum compact-frame payload length before future higher-level splitting is needed.
pub const MAX_FRAME_PAYLOAD_LEN: usize = u16::MAX as usize;

/// Fixed fragment metadata length after a compact v1/v2 header.
pub const FRAGMENT_METADATA_LEN: usize = 10;

const KIND_DATA: u8 = 0;
const KIND_CONTROL: u8 = 1;
const KIND_BATCH: u8 = 2;
const KIND_MASK: u8 = 0b0000_0011;
const FLAG_FRAGMENT_PRESENT: u8 = 0b0000_0100;
const KIND_FLAGS_RESERVED_MASK: u8 = 0b1111_1000;

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

/// Owned compact frame with its logical payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub kind: FrameKind,
    pub service_id: ServiceId,
    pub path_id: PathId,
    pub sequence: SequenceNumber,
    pub fragment: Option<FragmentInfo>,
    pub payload: Vec<u8>,
}

impl Frame {
    /// Build a data frame around a virtual UDP payload.
    pub fn data(
        service_id: ServiceId,
        path_id: PathId,
        sequence: SequenceNumber,
        payload: Vec<u8>,
    ) -> Result<Self, ProtocolError> {
        Self::new(FrameKind::Data, service_id, path_id, sequence, None, payload)
    }

    /// Build a batch frame containing multiple virtual UDP payloads for one path.
    ///
    /// Batch frames keep byte usage low for many small UDP datagrams. The frame
    /// sequence is the first payload's sequence number; each following item is
    /// implicitly `sequence + index` in payload order.
    pub fn batch(
        service_id: ServiceId,
        path_id: PathId,
        sequence: SequenceNumber,
        payloads: &[Vec<u8>],
    ) -> Result<Self, ProtocolError> {
        let payload = encode_batch_payload(payloads)?;
        Self::new(FrameKind::Batch, service_id, path_id, sequence, None, payload)
    }

    /// Build a control metaband frame.
    ///
    /// Control payloads carry telemetry and future safe state changes without
    /// increasing the fixed data header. They may be batched or sent sparsely by
    /// higher layers.
    pub fn control(
        service_id: ServiceId,
        path_id: PathId,
        sequence: SequenceNumber,
        payload: Vec<u8>,
    ) -> Result<Self, ProtocolError> {
        Self::new(FrameKind::Control, service_id, path_id, sequence, None, payload)
    }

    /// Build one data-frame fragment for a virtual UDP payload that does not fit one path MTU.
    pub fn fragment(
        service_id: ServiceId,
        path_id: PathId,
        sequence: SequenceNumber,
        fragment: FragmentInfo,
        payload: Vec<u8>,
    ) -> Result<Self, ProtocolError> {
        Self::new(FrameKind::Data, service_id, path_id, sequence, Some(fragment), payload)
    }

    fn new(
        kind: FrameKind,
        service_id: ServiceId,
        path_id: PathId,
        sequence: SequenceNumber,
        fragment: Option<FragmentInfo>,
        payload: Vec<u8>,
    ) -> Result<Self, ProtocolError> {
        if payload.len() > MAX_FRAME_PAYLOAD_LEN {
            return Err(ProtocolError::PayloadTooLarge(payload.len()));
        }
        Ok(Self {
            kind,
            service_id,
            path_id,
            sequence,
            fragment,
            payload,
        })
    }

    /// Decode this frame's batch payload into individual virtual UDP payloads.
    pub fn batch_payloads(&self) -> Result<Vec<Vec<u8>>, ProtocolError> {
        if self.kind != FrameKind::Batch {
            return Err(ProtocolError::UnknownFrameKind(self.kind.to_u8()));
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
        let mut output = Vec::with_capacity(self.encoded_v1_len());
        self.encode_v1_into(&mut output)?;
        Ok(output)
    }

    /// Encode this frame into compact v2 bytes for AEAD plaintext.
    pub fn encode_v2(&self) -> Result<Vec<u8>, ProtocolError> {
        let mut output = Vec::with_capacity(self.encoded_v2_len());
        self.encode_v2_into(&mut output)?;
        Ok(output)
    }

    /// Return the encoded compact v1 length without allocating.
    pub fn encoded_v1_len(&self) -> usize {
        V1_HEADER_LEN + self.fragment.map_or(0, |_| FRAGMENT_METADATA_LEN) + self.payload.len()
    }

    /// Return the encoded compact v2 length without allocating.
    pub fn encoded_v2_len(&self) -> usize {
        V2_HEADER_LEN + self.fragment.map_or(0, |_| FRAGMENT_METADATA_LEN) + self.payload.len()
    }

    /// Append compact v1 bytes to an existing output buffer.
    pub fn encode_v1_into(&self, output: &mut Vec<u8>) -> Result<(), ProtocolError> {
        let start = output.len();
        let header_len = V1_HEADER_LEN + self.fragment.map_or(0, |_| FRAGMENT_METADATA_LEN);
        output.resize(start + header_len + self.payload.len(), 0);
        let frame = &mut output[start..];
        frame[0] = PROTOCOL_VERSION;
        frame[1] = self.kind.to_u8();
        if self.fragment.is_some() {
            frame[1] |= FLAG_FRAGMENT_PRESENT;
        }
        write_u16(frame, 2, self.service_id);
        write_u16(frame, 4, self.path_id);
        write_u64(frame, 6, self.sequence);
        if let Some(fragment) = self.fragment {
            encode_fragment_metadata(fragment, &mut frame[V1_HEADER_LEN..header_len]);
        }
        frame[header_len..].copy_from_slice(&self.payload);
        Ok(())
    }

    /// Append compact v2 bytes to an existing output buffer.
    pub fn encode_v2_into(&self, output: &mut Vec<u8>) -> Result<(), ProtocolError> {
        let start = output.len();
        let header_len = V2_HEADER_LEN + self.fragment.map_or(0, |_| FRAGMENT_METADATA_LEN);
        output.resize(start + header_len + self.payload.len(), 0);
        let frame = &mut output[start..];
        frame[0] = self.kind.to_u8();
        if self.fragment.is_some() {
            frame[0] |= FLAG_FRAGMENT_PRESENT;
        }
        write_u16(frame, 1, self.service_id);
        write_u16(frame, 3, self.path_id);
        write_u64(frame, 5, self.sequence);
        if let Some(fragment) = self.fragment {
            encode_fragment_metadata(fragment, &mut frame[V2_HEADER_LEN..header_len]);
        }
        frame[header_len..].copy_from_slice(&self.payload);
        Ok(())
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
        let version = input[0];
        if version != PROTOCOL_VERSION {
            return Err(ProtocolError::UnsupportedVersion(version));
        }
        let kind_flags = input[1];
        let has_fragment = parse_kind_flags(kind_flags)?;
        let header_len = V1_HEADER_LEN + if has_fragment { FRAGMENT_METADATA_LEN } else { 0 };
        if input.len() < header_len {
            return Err(ProtocolError::BufferTooSmall);
        }
        let fragment = if has_fragment {
            Some(decode_fragment_metadata(&input[V1_HEADER_LEN..header_len])?)
        } else {
            None
        };
        Self::new(
            FrameKind::from_u8(kind_flags & KIND_MASK)?,
            read_u16(input, 2),
            read_u16(input, 4),
            read_u64(input, 6),
            fragment,
            input[header_len..].to_vec(),
        )
    }

    /// Decode one complete compact v2 AEAD plaintext frame.
    pub fn decode_v2(input: &[u8]) -> Result<Self, ProtocolError> {
        if input.len() < V2_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }
        let kind_flags = input[0];
        let kind = FrameKind::from_u8(kind_flags & KIND_MASK)?;
        let has_fragment = parse_kind_flags(kind_flags)?;
        let header_len = V2_HEADER_LEN + if has_fragment { FRAGMENT_METADATA_LEN } else { 0 };
        if input.len() < header_len {
            return Err(ProtocolError::BufferTooSmall);
        }
        let fragment = if has_fragment {
            Some(decode_fragment_metadata(&input[V2_HEADER_LEN..header_len])?)
        } else {
            None
        };
        Self::new(
            kind,
            read_u16(input, 1),
            read_u16(input, 3),
            read_u64(input, 5),
            fragment,
            input[header_len..].to_vec(),
        )
    }

    /// Decode one owned compact v2 AEAD plaintext frame.
    ///
    /// The encrypted dataplane already has an owned plaintext buffer after
    /// AEAD open. Reusing that allocation avoids allocating a second payload
    /// buffer for every received user packet on the hot path.
    pub fn decode_v2_owned(mut input: Vec<u8>) -> Result<Self, ProtocolError> {
        if input.len() < V2_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }
        let kind_flags = input[0];
        let kind = FrameKind::from_u8(kind_flags & KIND_MASK)?;
        let has_fragment = parse_kind_flags(kind_flags)?;
        let header_len = V2_HEADER_LEN + if has_fragment { FRAGMENT_METADATA_LEN } else { 0 };
        if input.len() < header_len {
            return Err(ProtocolError::BufferTooSmall);
        }
        let fragment = if has_fragment {
            Some(decode_fragment_metadata(&input[V2_HEADER_LEN..header_len])?)
        } else {
            None
        };
        let service_id = read_u16(&input, 1);
        let path_id = read_u16(&input, 3);
        let sequence = read_u64(&input, 5);
        let payload_len = input.len() - header_len;
        input.copy_within(header_len.., 0);
        input.truncate(payload_len);
        Self::new(kind, service_id, path_id, sequence, fragment, input)
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
        return Err(ProtocolError::MalformedFragment);
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
        if total_len > MAX_FRAME_PAYLOAD_LEN {
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
