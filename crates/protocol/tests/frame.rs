use gatherlink_protocol::errors::ProtocolError;
use gatherlink_protocol::frame::{FragmentInfo, Frame, FrameKind, FRAGMENT_EXTENSION_LEN, V1_HEADER_LEN};
use gatherlink_protocol::version::PROTOCOL_VERSION;

#[test]
fn data_frame_round_trips_protocol_context_and_payload() {
    let frame = Frame::data(7, 11, 13, 0, 17, b"hello-core".to_vec()).unwrap();

    let encoded = frame.encode().unwrap();
    let decoded = Frame::decode(&encoded).unwrap();

    assert_eq!(encoded.len(), V1_HEADER_LEN + b"hello-core".len());
    assert_eq!(decoded.header.version, PROTOCOL_VERSION);
    assert_eq!(decoded.header.kind, FrameKind::Data);
    assert_eq!(decoded.header.header_len, V1_HEADER_LEN as u16);
    assert_eq!(decoded.header.session_id, 7);
    assert_eq!(decoded.header.service_id, 11);
    assert_eq!(decoded.header.path_id, 13);
    assert_eq!(decoded.header.route_id, 0);
    assert_eq!(decoded.header.sequence, 17);
    assert_eq!(decoded.payload, b"hello-core");
}

#[test]
fn optional_extension_bytes_round_trip_only_when_present() {
    let mut frame = Frame::data(7, 11, 13, 0, 17, b"hello-core".to_vec()).unwrap();
    frame.extensions = vec![0xaa, 0xbb, 0xcc];

    let encoded = frame.encode().unwrap();
    let decoded = Frame::decode(&encoded).unwrap();

    assert_eq!(encoded.len(), V1_HEADER_LEN + 3 + b"hello-core".len());
    assert_eq!(decoded.header.header_len, (V1_HEADER_LEN + 3) as u16);
    assert_eq!(decoded.extensions, [0xaa, 0xbb, 0xcc]);
    assert_eq!(decoded.payload, b"hello-core");
}

#[test]
fn batch_frame_coalesces_same_path_payloads() {
    let payloads = vec![b"a".to_vec(), b"hello".to_vec(), b"core".to_vec()];
    let frame = Frame::batch(7, 11, 13, 0, 17, &payloads).unwrap();

    let encoded = frame.encode().unwrap();
    let decoded = Frame::decode(&encoded).unwrap();

    assert_eq!(decoded.header.kind, FrameKind::Batch);
    assert_eq!(decoded.header.sequence, 17);
    assert_eq!(encoded.len(), V1_HEADER_LEN + 2 + 2 + 1 + 2 + 5 + 2 + 4);
    assert_eq!(decoded.batch_payloads().unwrap(), payloads);
}

#[test]
fn fragmented_data_frame_carries_optional_fragment_metadata() {
    let fragment = FragmentInfo::new(99, 1, 3, 1200).unwrap();
    let frame = Frame::fragment(7, 11, 13, 0, 17, fragment, b"chunk".to_vec()).unwrap();

    let encoded = frame.encode().unwrap();
    let decoded = Frame::decode(&encoded).unwrap();

    assert_eq!(encoded.len(), V1_HEADER_LEN + FRAGMENT_EXTENSION_LEN + b"chunk".len());
    assert_eq!(decoded.header.kind, FrameKind::Data);
    assert_eq!(decoded.extensions.len(), FRAGMENT_EXTENSION_LEN);
    assert_eq!(decoded.fragment_info().unwrap(), Some(fragment));
    assert_eq!(decoded.payload, b"chunk");
}

#[test]
fn rejects_empty_batch_frames() {
    let error = Frame::batch(7, 11, 13, 0, 17, &[]).unwrap_err();

    assert!(matches!(error, ProtocolError::EmptyBatch));
}

#[test]
fn rejects_malformed_batch_payloads() {
    let mut frame = Frame::batch(7, 11, 13, 0, 17, &[b"abc".to_vec()]).unwrap();
    frame.payload.pop();

    let error = frame.batch_payloads().unwrap_err();

    assert!(matches!(error, ProtocolError::MalformedBatch));
}

#[test]
fn rejects_unsupported_versions_without_response_context() {
    let frame = Frame::data(1, 1, 1, 0, 1, b"x".to_vec()).unwrap();
    let mut encoded = frame.encode().unwrap();
    encoded[0] = 99;

    let error = Frame::decode(&encoded).unwrap_err();

    assert!(matches!(error, ProtocolError::UnsupportedVersion(99)));
}

#[test]
fn rejects_reserved_flags_for_v1() {
    let frame = Frame::data(1, 1, 1, 0, 1, b"x".to_vec()).unwrap();
    let mut encoded = frame.encode().unwrap();
    encoded[5] = 1;

    let error = Frame::decode(&encoded).unwrap_err();

    assert!(matches!(error, ProtocolError::UnknownFlags(1)));
}

#[test]
fn rejects_headers_smaller_than_v1_base_header() {
    let frame = Frame::data(1, 1, 1, 0, 1, b"x".to_vec()).unwrap();
    let mut encoded = frame.encode().unwrap();
    encoded[2..4].copy_from_slice(&43_u16.to_be_bytes());

    let error = Frame::decode(&encoded).unwrap_err();

    assert!(matches!(
        error,
        ProtocolError::HeaderLengthMismatch {
            expected: V1_HEADER_LEN,
            actual: 43,
        }
    ));
}

#[test]
fn rejects_payload_length_mismatch() {
    let frame = Frame::data(1, 1, 1, 0, 1, b"abc".to_vec()).unwrap();
    let mut encoded = frame.encode().unwrap();
    encoded.pop();

    let error = Frame::decode(&encoded).unwrap_err();

    assert!(matches!(error, ProtocolError::PayloadLengthMismatch { .. }));
}
