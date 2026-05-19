//! Versioned Gatherlink UDP frame format.
//!
//! The v1 frame deliberately models the durable protocol concepts before their
//! full implementations exist: frame class, flags, session/service/path/route
//! IDs, sequence number, MTU-relevant payload length, and reserved fragmentation
//! fields. Authentication/encryption wraps this context later; unauthenticated
//! public listeners must still silently drop invalid frames.

use crate::errors::ProtocolError;
use crate::ids::{PathId, RouteId, SequenceNumber, ServiceId, SessionId};
use crate::version::PROTOCOL_VERSION;

/// Current fixed header length in bytes.
pub const V1_HEADER_LEN: usize = 64;

/// Max v1 payload length before future fragmentation is needed.
pub const MAX_V1_PAYLOAD_LEN: usize = u16::MAX as usize;

const KIND_DATA: u8 = 1;
const KIND_CONTROL: u8 = 2;
const FLAG_FRAGMENTED: u16 = 0x0001;
const KNOWN_FLAGS: u16 = FLAG_FRAGMENTED;

/// Broad frame class carried over UDP.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum FrameKind {
    Data,
    Control,
}

impl FrameKind {
    fn to_u8(self) -> u8 {
        match self {
            Self::Data => KIND_DATA,
            Self::Control => KIND_CONTROL,
        }
    }

    fn from_u8(value: u8) -> Result<Self, ProtocolError> {
        match value {
            KIND_DATA => Ok(Self::Data),
            KIND_CONTROL => Ok(Self::Control),
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

    /// Whether reserved fragmentation fields are active.
    pub fn fragmented(self) -> bool {
        self.bits & FLAG_FRAGMENTED != 0
    }
}

/// Fixed v1 header. Fragmentation fields are reserved and must be zero unless
/// the fragmented flag is set by a future version.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameHeader {
    pub version: u16,
    pub kind: FrameKind,
    pub flags: FrameFlags,
    pub session_id: SessionId,
    pub service_id: ServiceId,
    pub path_id: PathId,
    pub route_id: RouteId,
    pub sequence: SequenceNumber,
    pub payload_len: u16,
    pub fragment_id: u16,
    pub fragment_offset: u16,
    pub fragment_count: u16,
}

impl FrameHeader {
    /// Build a data-frame header for one virtual UDP payload.
    pub fn data(
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
            kind: FrameKind::Data,
            flags: FrameFlags::default(),
            session_id,
            service_id,
            path_id,
            route_id,
            sequence,
            payload_len: payload_len as u16,
            fragment_id: 0,
            fragment_offset: 0,
            fragment_count: 0,
        })
    }

    /// Encode the fixed header into a caller-provided buffer.
    pub fn encode_into(&self, output: &mut [u8]) -> Result<(), ProtocolError> {
        if output.len() < V1_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }
        if self.flags.fragmented()
            || self.fragment_id != 0
            || self.fragment_offset != 0
            || self.fragment_count != 0
        {
            return Err(ProtocolError::FragmentationNotSupported);
        }

        output[..V1_HEADER_LEN].fill(0);
        write_u16(output, 0, self.version);
        output[2] = self.kind.to_u8();
        output[3] = 0;
        write_u16(output, 4, self.flags.bits());
        write_u16(output, 6, V1_HEADER_LEN as u16);
        write_u128(output, 8, self.session_id);
        write_u64(output, 24, self.service_id);
        write_u64(output, 32, self.path_id);
        write_u64(output, 40, self.route_id);
        write_u64(output, 48, self.sequence);
        write_u16(output, 56, self.payload_len);
        write_u16(output, 58, self.fragment_id);
        write_u16(output, 60, self.fragment_offset);
        write_u16(output, 62, self.fragment_count);
        Ok(())
    }

    /// Decode and validate a fixed v1 header.
    pub fn decode(input: &[u8]) -> Result<Self, ProtocolError> {
        if input.len() < V1_HEADER_LEN {
            return Err(ProtocolError::BufferTooSmall);
        }

        let version = read_u16(input, 0);
        if version != PROTOCOL_VERSION {
            return Err(ProtocolError::UnsupportedVersion(version));
        }

        let header_len = read_u16(input, 6) as usize;
        if header_len != V1_HEADER_LEN {
            return Err(ProtocolError::HeaderLengthMismatch {
                expected: V1_HEADER_LEN,
                actual: header_len,
            });
        }

        let flags = FrameFlags::from_bits(read_u16(input, 4))?;
        let fragment_id = read_u16(input, 58);
        let fragment_offset = read_u16(input, 60);
        let fragment_count = read_u16(input, 62);
        if flags.fragmented() || fragment_id != 0 || fragment_offset != 0 || fragment_count != 0 {
            return Err(ProtocolError::FragmentationNotSupported);
        }

        Ok(Self {
            version,
            kind: FrameKind::from_u8(input[2])?,
            flags,
            session_id: read_u128(input, 8),
            service_id: read_u64(input, 24),
            path_id: read_u64(input, 32),
            route_id: read_u64(input, 40),
            sequence: read_u64(input, 48),
            payload_len: read_u16(input, 56),
            fragment_id,
            fragment_offset,
            fragment_count,
        })
    }
}

/// Owned frame with header and payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub header: FrameHeader,
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
        let header = FrameHeader::data(
            session_id,
            service_id,
            path_id,
            route_id,
            sequence,
            payload.len(),
        )?;
        Ok(Self { header, payload })
    }

    /// Encode this frame into wire bytes.
    pub fn encode(&self) -> Result<Vec<u8>, ProtocolError> {
        if self.payload.len() != self.header.payload_len as usize {
            return Err(ProtocolError::PayloadLengthMismatch {
                expected: self.header.payload_len as usize,
                actual: self.payload.len(),
            });
        }

        let mut output = vec![0_u8; V1_HEADER_LEN + self.payload.len()];
        self.header.encode_into(&mut output[..V1_HEADER_LEN])?;
        output[V1_HEADER_LEN..].copy_from_slice(&self.payload);
        Ok(output)
    }

    /// Decode one complete frame from wire bytes.
    pub fn decode(input: &[u8]) -> Result<Self, ProtocolError> {
        let header = FrameHeader::decode(input)?;
        let expected = V1_HEADER_LEN + header.payload_len as usize;
        if input.len() != expected {
            return Err(ProtocolError::PayloadLengthMismatch {
                expected,
                actual: input.len(),
            });
        }
        Ok(Self {
            header,
            payload: input[V1_HEADER_LEN..].to_vec(),
        })
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn data_frame_round_trips_protocol_context_and_payload() {
        let frame = Frame::data(7, 11, 13, 0, 17, b"hello-core".to_vec()).unwrap();

        let encoded = frame.encode().unwrap();
        let decoded = Frame::decode(&encoded).unwrap();

        assert_eq!(decoded.header.version, PROTOCOL_VERSION);
        assert_eq!(decoded.header.kind, FrameKind::Data);
        assert_eq!(decoded.header.session_id, 7);
        assert_eq!(decoded.header.service_id, 11);
        assert_eq!(decoded.header.path_id, 13);
        assert_eq!(decoded.header.route_id, 0);
        assert_eq!(decoded.header.sequence, 17);
        assert_eq!(decoded.payload, b"hello-core");
    }

    #[test]
    fn rejects_unsupported_versions_without_response_context() {
        let frame = Frame::data(1, 1, 1, 0, 1, b"x".to_vec()).unwrap();
        let mut encoded = frame.encode().unwrap();
        encoded[1] = 99;

        let error = Frame::decode(&encoded).unwrap_err();

        assert!(matches!(error, ProtocolError::UnsupportedVersion(99)));
    }

    #[test]
    fn rejects_reserved_fragmentation_for_v1() {
        let frame = Frame::data(1, 1, 1, 0, 1, b"x".to_vec()).unwrap();
        let mut encoded = frame.encode().unwrap();
        encoded[5] = FLAG_FRAGMENTED as u8;

        let error = Frame::decode(&encoded).unwrap_err();

        assert!(matches!(error, ProtocolError::FragmentationNotSupported));
    }

    #[test]
    fn rejects_payload_length_mismatch() {
        let frame = Frame::data(1, 1, 1, 0, 1, b"abc".to_vec()).unwrap();
        let mut encoded = frame.encode().unwrap();
        encoded.pop();

        let error = Frame::decode(&encoded).unwrap_err();

        assert!(matches!(error, ProtocolError::PayloadLengthMismatch { .. }));
    }
}
