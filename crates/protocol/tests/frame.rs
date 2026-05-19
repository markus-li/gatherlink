use gatherlink_protocol::errors::ProtocolError;
use gatherlink_protocol::frame::{FragmentInfo, Frame, FrameKind, FRAGMENT_METADATA_LEN, V1_HEADER_LEN, V2_HEADER_LEN};

#[test]
fn data_frame_round_trips_protocol_context_and_payload() {
    let frame = Frame::data(11, 13, 17, b"hello-core".to_vec()).unwrap();

    let encoded = frame.encode().unwrap();
    let decoded = Frame::decode(&encoded).unwrap();

    assert_eq!(encoded.len(), V1_HEADER_LEN + b"hello-core".len());
    assert_eq!(decoded.kind, FrameKind::Data);
    assert_eq!(decoded.service_id, 11);
    assert_eq!(decoded.path_id, 13);
    assert_eq!(decoded.sequence, 17);
    assert_eq!(decoded.payload, b"hello-core");
}

#[test]
fn batch_frame_coalesces_same_path_payloads() {
    let payloads = vec![b"a".to_vec(), b"hello".to_vec(), b"core".to_vec()];
    let frame = Frame::batch(11, 13, 17, &payloads).unwrap();

    let encoded = frame.encode().unwrap();
    let decoded = Frame::decode(&encoded).unwrap();

    assert_eq!(decoded.kind, FrameKind::Batch);
    assert_eq!(decoded.sequence, 17);
    assert_eq!(encoded.len(), V1_HEADER_LEN + 2 + 2 + 1 + 2 + 5 + 2 + 4);
    assert_eq!(decoded.batch_payloads().unwrap(), payloads);
}

#[test]
fn fragmented_data_frame_carries_optional_fragment_metadata() {
    let fragment = FragmentInfo::new(99, 1, 3, 1200).unwrap();
    let frame = Frame::fragment(11, 13, 17, fragment, b"chunk".to_vec()).unwrap();

    let encoded = frame.encode().unwrap();
    let decoded = Frame::decode(&encoded).unwrap();

    assert_eq!(encoded.len(), V1_HEADER_LEN + FRAGMENT_METADATA_LEN + b"chunk".len());
    assert_eq!(decoded.kind, FrameKind::Data);
    assert_eq!(decoded.fragment_info().unwrap(), Some(fragment));
    assert_eq!(decoded.payload, b"chunk");
}

#[test]
fn compact_v2_omits_visible_version_and_round_trips() {
    let fragment = FragmentInfo::new(99, 1, 3, 1200).unwrap();
    let frame = Frame::fragment(11, 13, 17, fragment, b"chunk".to_vec()).unwrap();

    let encoded = frame.encode_v2().unwrap();
    let decoded = Frame::decode_v2(&encoded).unwrap();

    assert_eq!(encoded.len(), V2_HEADER_LEN + FRAGMENT_METADATA_LEN + b"chunk".len());
    assert_eq!(encoded[0], FrameKind::Data.to_u8() | 0b0000_0100);
    assert_eq!(decoded.kind, FrameKind::Data);
    assert_eq!(decoded.service_id, 11);
    assert_eq!(decoded.path_id, 13);
    assert_eq!(decoded.sequence, 17);
    assert_eq!(decoded.fragment_info().unwrap(), Some(fragment));
    assert_eq!(decoded.payload, b"chunk");
}

#[test]
fn rejects_empty_batch_frames() {
    let error = Frame::batch(11, 13, 17, &[]).unwrap_err();

    assert!(matches!(error, ProtocolError::EmptyBatch));
}

#[test]
fn rejects_malformed_batch_payloads() {
    let mut frame = Frame::batch(11, 13, 17, &[b"abc".to_vec()]).unwrap();
    frame.payload.pop();

    let error = frame.batch_payloads().unwrap_err();

    assert!(matches!(error, ProtocolError::MalformedBatch));
}

#[test]
fn rejects_unsupported_versions_without_response_context() {
    let frame = Frame::data(1, 1, 1, b"x".to_vec()).unwrap();
    let mut encoded = frame.encode().unwrap();
    encoded[0] = 99;

    let error = Frame::decode(&encoded).unwrap_err();

    assert!(matches!(error, ProtocolError::UnsupportedVersion(99)));
}

#[test]
fn rejects_reserved_flags_for_v1() {
    let frame = Frame::data(1, 1, 1, b"x".to_vec()).unwrap();
    let mut encoded = frame.encode().unwrap();
    encoded[1] = 0b1000_0000;

    let error = Frame::decode(&encoded).unwrap_err();

    assert!(matches!(error, ProtocolError::UnknownFlags(0b1000_0000)));
}

#[test]
fn rejects_fragment_bit_without_fragment_metadata() {
    let frame = Frame::data(1, 1, 1, b"x".to_vec()).unwrap();
    let mut encoded = frame.encode().unwrap();
    encoded[1] |= 0b0000_0100;

    let error = Frame::decode(&encoded).unwrap_err();

    assert!(matches!(error, ProtocolError::BufferTooSmall));
}

#[test]
fn rejects_payload_too_large_for_compact_frame() {
    let frame = Frame::data(1, 1, 1, vec![0; u16::MAX as usize + 1]);

    let error = frame.unwrap_err();

    assert!(matches!(error, ProtocolError::PayloadTooLarge(_)));
}
