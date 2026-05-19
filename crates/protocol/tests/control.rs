use gatherlink_protocol::control::{
    ClockSyncMode, ControlMessage, ControlPayload, GlobalSequenceTracker, InternalClockSync, MissingRange, NtpState,
    PathAssignment, PathCapacity, PathLatency, PathMetadata, SinkTime,
};
use gatherlink_protocol::frame::{Frame, FrameKind};

#[test]
fn control_payload_round_trips_telemetry_messages() {
    let payload = ControlPayload::new(vec![
        ControlMessage::PathAssignment(PathAssignment::new(100, 25, 7).unwrap()),
        ControlMessage::MissingRange(MissingRange::new(110, 3).unwrap()),
        ControlMessage::PathMetadata(PathMetadata::new(7, "path-a").unwrap()),
        ControlMessage::PathCapacity(PathCapacity::new(7, Some(3_000_000), Some(1_500_000)).unwrap()),
        ControlMessage::PathLatency(
            PathLatency::new(7, Some(12_000), Some(10_000), Some(14_000), Some(11_000)).unwrap(),
        ),
        ControlMessage::InternalClockSync(
            InternalClockSync::new(
                99,
                7,
                ClockSyncMode::Response,
                1_000_000,
                Some(1_010_000),
                Some(1_011_000),
            )
            .unwrap(),
        ),
        ControlMessage::SinkTime(SinkTime::new(7, 1_776_000_000_000_000, 77_000_000, NtpState::Synchronized).unwrap()),
    ])
    .unwrap();

    let encoded = payload.encode().unwrap();
    let decoded = ControlPayload::decode(&encoded).unwrap();

    assert_eq!(decoded, payload);
}

#[test]
fn control_payload_fits_inside_control_frame() {
    let control = ControlPayload::new(vec![ControlMessage::PathAssignment(
        PathAssignment::new(u64::MAX - 2, 3, 42).unwrap(),
    )])
    .unwrap();
    let frame = Frame::control(1, 2, 42, 0, 99, control.encode().unwrap()).unwrap();

    let decoded_frame = Frame::decode(&frame.encode().unwrap()).unwrap();
    let decoded_control = ControlPayload::decode(&decoded_frame.payload).unwrap();

    assert_eq!(decoded_frame.header.kind, FrameKind::Control);
    assert_eq!(decoded_control, control);
}

#[test]
fn global_sequence_tracker_reports_missing_ranges() {
    let mut tracker = GlobalSequenceTracker::default();

    assert_eq!(tracker.observe(10).missing_packets, 0);
    assert_eq!(tracker.observe(11).missing_packets, 0);
    assert_eq!(tracker.observe(15).missing_packets, 3);
}

#[test]
fn global_sequence_tracker_handles_wraparound() {
    let mut tracker = GlobalSequenceTracker::default();

    assert_eq!(tracker.observe(u64::MAX - 1).missing_packets, 0);
    assert_eq!(tracker.observe(u64::MAX).missing_packets, 0);
    assert_eq!(tracker.observe(1).missing_packets, 1);
}

#[test]
fn global_sequence_tracker_marks_late_packets_out_of_order() {
    let mut tracker = GlobalSequenceTracker::default();

    tracker.observe(100);
    tracker.observe(103);
    let observation = tracker.observe(102);

    assert_eq!(observation.missing_packets, 0);
    assert!(observation.out_of_order);
    assert!(observation.duplicate_or_late);
}
