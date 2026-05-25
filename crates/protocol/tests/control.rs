use gatherlink_protocol::control::{
    ClockSyncMode, ControlMessage, ControlPayload, DataTransmitSample, GlobalSequenceTracker, InternalClockSync,
    MissingRange, NtpState, PathAssignment, PathCapacity, PathLatency, PathMetadata, PathMtu, PathPressure,
    SchedulerStatus, ServiceDisable, ServiceEndpointAssertion, ServiceMetadata, ServiceSchedulerPolicy, SinkTime,
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
        ControlMessage::PathMtu(PathMtu::new(7, Some(1500), Some(1200), Some(1400), Some(1180)).unwrap()),
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
        ControlMessage::ServiceMetadata(ServiceMetadata::new(256, "udp-main").unwrap()),
        ControlMessage::ServiceEndpointAssertion(ServiceEndpointAssertion::new(256, "127.0.0.1:51820").unwrap()),
        ControlMessage::ServiceDisable(ServiceDisable::new(256, "sink declined service").unwrap()),
        ControlMessage::ServiceSchedulerPolicy(ServiceSchedulerPolicy::new(256, 2, 512, 50_000, 500_000, 64).unwrap()),
        ControlMessage::PathPressure(
            PathPressure::new(7, 1200, 3, 4096, 2500, 1, 2, 3, 4, 5, 8192, 12_000, 6, 2500).unwrap(),
        ),
        ControlMessage::SchedulerStatus(
            SchedulerStatus::new("coordinated_adaptive", "flowlet_adaptive", "adaptive").unwrap(),
        ),
        ControlMessage::DataTransmitSample(DataTransmitSample::new(7, 2048, 16, 77_000_000).unwrap()),
    ])
    .unwrap();

    let encoded = payload.encode().unwrap();
    let decoded = ControlPayload::decode(&encoded).unwrap();

    assert_eq!(decoded, payload);
}

#[test]
fn control_payload_rejects_invalid_pressure_and_scheduler_status() {
    assert!(PathPressure::new(7, 1_000_001, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0).is_err());
    assert!(SchedulerStatus::new("", "flowlet_adaptive", "adaptive").is_err());
    assert!(SchedulerStatus::new("coordinated_adaptive", "x".repeat(64), "adaptive").is_err());
}

#[test]
fn control_payload_fits_inside_control_frame() {
    let control = ControlPayload::new(vec![ControlMessage::PathAssignment(
        PathAssignment::new(u64::MAX - 2, 3, 42).unwrap(),
    )])
    .unwrap();
    let frame = Frame::control(2, 42, 99, control.encode().unwrap()).unwrap();

    let decoded_frame = Frame::decode(&frame.encode().unwrap()).unwrap();
    let decoded_control = ControlPayload::decode(&decoded_frame.payload).unwrap();

    assert_eq!(decoded_frame.kind, FrameKind::Control);
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
