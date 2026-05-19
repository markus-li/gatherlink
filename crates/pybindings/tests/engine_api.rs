use std::net::UdpSocket;
use std::thread;
use std::time::Duration;

use gatherlink_protocol::control::{
    ClockSyncMode, ControlMessage, ControlPayload, InternalClockSync, NtpState, PathCapacity, PathLatency,
    PathMetadata, ServiceEndpointAssertion, ServiceMetadata, SinkTime,
};
use gatherlink_pybindings::dto::{PyPathConfig, PySchedulerConfig, PyUdpServiceConfig};
use gatherlink_pybindings::engine_api::PyCoreDataplane;
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn recv_with_retry(socket: &UdpSocket, buffer: &mut [u8]) -> (usize, std::net::SocketAddr) {
    for _ in 0..20 {
        match socket.recv_from(buffer) {
            Ok(received) => return received,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                thread::sleep(Duration::from_millis(10));
            }
            Err(error) => panic!("failed to receive UDP datagram: {error}"),
        }
    }
    socket.recv_from(buffer).unwrap()
}

#[test]
fn python_facing_dataplane_forwards_one_udp_payload() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
    let service_addr = dataplane.service_local_addr("udp-main").unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    sender.send_to(b"python-bridge-core", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = recv_with_retry(&target, &mut buffer);
    assert_eq!(&buffer[..length], b"python-bridge-core");
    assert_eq!(outcome.service(), "udp-main");
    assert_eq!(outcome.payload_len(), b"python-bridge-core".len());
    assert_eq!(outcome.sequence(), 1);

    Python::with_gil(|py| {
        let status = dataplane.status_snapshot(py).unwrap();
        let root = status.bind(py).downcast::<PyDict>().unwrap();
        let services_value = root.get_item("services").unwrap().unwrap();
        let services = services_value.downcast::<PyDict>().unwrap();
        let service_value = services.get_item("udp-main").unwrap().unwrap();
        let service = service_value.downcast::<PyDict>().unwrap();
        assert_eq!(
            service.get_item("packets").unwrap().unwrap().extract::<u64>().unwrap(),
            1
        );
        assert_eq!(
            service.get_item("bytes").unwrap().unwrap().extract::<u64>().unwrap(),
            b"python-bridge-core".len() as u64
        );
        assert_eq!(
            service
                .get_item("tx_packets")
                .unwrap()
                .unwrap()
                .extract::<u64>()
                .unwrap(),
            1
        );
    });
}

#[test]
fn python_facing_status_exposes_received_control_metadata() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
    let control = ControlPayload::new(vec![
        ControlMessage::PathMetadata(PathMetadata::new(3, "path-three").unwrap()),
        ControlMessage::PathCapacity(PathCapacity::new(3, Some(10_000_000), None).unwrap()),
        ControlMessage::PathLatency(PathLatency::new(3, Some(2_500), Some(2_000), None, Some(3_000)).unwrap()),
        ControlMessage::InternalClockSync(
            InternalClockSync::new(
                41,
                3,
                ClockSyncMode::Response,
                2_000_000,
                Some(2_010_000),
                Some(2_011_000),
            )
            .unwrap(),
        ),
        ControlMessage::SinkTime(SinkTime::new(3, 1_776_000_000_000_000, 77_000_000, NtpState::Synchronized).unwrap()),
        ControlMessage::ServiceMetadata(ServiceMetadata::new(256, "udp-main").unwrap()),
        ControlMessage::ServiceEndpointAssertion(
            ServiceEndpointAssertion::new(256, target.local_addr().unwrap().to_string()).unwrap(),
        ),
    ])
    .unwrap()
    .encode()
    .unwrap();

    dataplane.observe_received_data_frame(256, 3, 1, 512);
    dataplane.observe_received_reserved_service_payload(
        1,
        3,
        41,
        &control,
        control.len() + gatherlink_protocol::frame::V1_HEADER_LEN,
    );

    Python::with_gil(|py| {
        let events = dataplane.drain_reserved_service_events();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].service_id(), 1);
        assert_eq!(events[0].path_id(), 3);
        assert_eq!(events[0].sequence(), 41);
        assert_eq!(events[0].payload(), control);

        let status = dataplane.status_snapshot(py).unwrap();
        let root = status.bind(py).downcast::<PyDict>().unwrap();
        let path_stats_value = root.get_item("path_stats").unwrap().unwrap();
        let path_stats = path_stats_value.downcast::<PyDict>().unwrap();
        let path_three_value = path_stats.get_item("3").unwrap().unwrap();
        let path_three = path_three_value.downcast::<PyDict>().unwrap();
        assert_eq!(
            path_three
                .get_item("rx_packets")
                .unwrap()
                .unwrap()
                .extract::<u64>()
                .unwrap(),
            1
        );

        let control_value = root.get_item("control_metadata").unwrap().unwrap();
        let control_metadata = control_value.downcast::<PyDict>().unwrap();
        let received_value = control_metadata.get_item("received").unwrap().unwrap();
        let received = received_value.downcast::<PyDict>().unwrap();
        assert_eq!(
            received.get_item("frames").unwrap().unwrap().extract::<u64>().unwrap(),
            1
        );
        let path_control_value = control_metadata.get_item("path_control").unwrap().unwrap();
        let path_control = path_control_value.downcast::<PyDict>().unwrap();
        let path_three_control_value = path_control.get_item("3").unwrap().unwrap();
        let path_three_control = path_three_control_value.downcast::<PyDict>().unwrap();
        let rx_value = path_three_control.get_item("rx").unwrap().unwrap();
        let rx = rx_value.downcast::<PyDict>().unwrap();
        assert_eq!(rx.get_item("frames").unwrap().unwrap().extract::<u64>().unwrap(), 1);
        let security_drops_value = root.get_item("security_drops").unwrap().unwrap();
        let security_drops = security_drops_value.downcast::<PyDict>().unwrap();
        assert_eq!(
            security_drops
                .get_item("packets")
                .unwrap()
                .unwrap()
                .extract::<u64>()
                .unwrap(),
            0
        );
    });
}

#[test]
fn python_facing_dataplane_forwards_ipv6_udp_payload() {
    let target = UdpSocket::bind("[::1]:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-v6".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("[::1]:0".to_owned()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
    let service_addr = dataplane.service_local_addr("udp-v6").unwrap();
    let sender = UdpSocket::bind("[::1]:0").unwrap();

    sender.send_to(b"python-ipv6-core", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-v6").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"python-ipv6-core");
    assert_eq!(outcome.service(), "udp-v6");
    assert_eq!(outcome.target(), target.local_addr().unwrap().to_string());
}

#[test]
fn python_facing_dataplane_reapplies_target_update() {
    let first_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let second_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        first_target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
    let listen = dataplane.service_local_addr("udp-main").unwrap();
    let updated = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        second_target.local_addr().unwrap().to_string(),
        Some(listen.clone()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();

    let outcome = dataplane.reapply_config(vec![updated]).unwrap();

    assert_eq!(dataplane.service_local_addr("udp-main").unwrap(), listen);
    assert_eq!(outcome.unchanged(), 0);
    assert_eq!(outcome.updated(), 1);
    assert_eq!(outcome.rebound(), 0);
    assert_eq!(outcome.added(), 0);
    assert_eq!(outcome.removed(), 0);
}

#[test]
fn python_facing_dataplane_accepts_explicit_paths() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        200,
        "learned-single-source",
        0,
        1,
        0,
    )
    .unwrap();
    assert_eq!(service.priority(), 200);
    assert_eq!(service.return_mode(), "learned-single-source");
    let path = PyPathConfig::new(
        3, 1200, false, true, "active", 1, None, None, None, 0, 0, 0, 0, None, None,
    )
    .unwrap();
    assert_eq!(path.tx_capacity_bps(), None);
    let mut dataplane = PyCoreDataplane::bind_with_paths(vec![service], vec![path]).unwrap();
    let service_addr = dataplane.service_local_addr("udp-main").unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    sender.send_to(b"python-path-core", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"python-path-core");
    assert_eq!(outcome.path_id(), 3);
    assert_eq!(outcome.frame_count(), 1);
}

#[test]
fn python_facing_path_config_accepts_scheduler_primitives() {
    let path = PyPathConfig::new(
        7,
        1200,
        false,
        true,
        "active",
        4,
        Some(3_000_000),
        Some(1_500_000),
        Some(12_000),
        25_000,
        150_000,
        64,
        524_288,
        Some("127.0.0.1:10000".to_owned()),
        Some("127.0.0.1:10001".to_owned()),
    )
    .unwrap();

    assert_eq!(path.weight(), 4);
    assert_eq!(path.tx_capacity_bps(), Some(3_000_000));
    assert_eq!(path.rx_capacity_bps(), Some(1_500_000));
    assert_eq!(path.latency_us(), Some(12_000));
    assert_eq!(path.loss_ppm(), 25_000);
    assert_eq!(path.reorder_hold_us(), 150_000);
    assert_eq!(path.max_in_flight_packets(), 64);
    assert_eq!(path.max_in_flight_bytes(), 524_288);
    assert_eq!(path.transport_bind().as_deref(), Some("127.0.0.1:10000"));
    assert_eq!(path.transport_remote().as_deref(), Some("127.0.0.1:10001"));
}

#[test]
fn python_facing_dataplane_accepts_scheduler_config() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();
    let paths = vec![
        PyPathConfig::new(
            5, 1200, false, true, "active", 1, None, None, None, 0, 0, 0, 0, None, None,
        )
        .unwrap(),
        PyPathConfig::new(
            6, 1200, false, true, "active", 1, None, None, None, 0, 0, 0, 0, None, None,
        )
        .unwrap(),
    ];
    let scheduler = PySchedulerConfig::new("round_robin").unwrap();
    let mut dataplane = PyCoreDataplane::bind_with_scheduler(vec![service], paths, scheduler, None).unwrap();
    let service_addr = dataplane.service_local_addr("udp-main").unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    sender.send_to(b"one", &service_addr).unwrap();
    sender.send_to(b"two", &service_addr).unwrap();
    let outcomes = dataplane.forward_available_for_service("udp-main", 2).unwrap();

    let mut buffer = [0_u8; 8];
    recv_with_retry(&target, &mut buffer);
    recv_with_retry(&target, &mut buffer);
    assert_eq!(outcomes[0].path_id(), 5);
    assert_eq!(outcomes[1].path_id(), 6);
}

#[test]
fn python_facing_scheduler_reapply_preserves_bound_service_socket() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();
    let paths = vec![PyPathConfig::new(
        5, 1200, false, true, "active", 1, None, None, None, 0, 0, 0, 0, None, None,
    )
    .unwrap()];
    let scheduler = PySchedulerConfig::new("round_robin").unwrap();
    let mut dataplane = PyCoreDataplane::bind_with_scheduler(vec![service], paths, scheduler, None).unwrap();
    let original_listen = dataplane.service_local_addr("udp-main").unwrap();

    let updated_paths = vec![PyPathConfig::new(
        5,
        1180,
        false,
        true,
        "active",
        3,
        Some(3_000_000),
        Some(3_000_000),
        Some(2_000),
        0,
        2_000,
        0,
        0,
        None,
        None,
    )
    .unwrap()];
    let outcome = dataplane
        .reapply_scheduler(updated_paths, PySchedulerConfig::new("capacity_aware").unwrap())
        .unwrap();

    assert_eq!(dataplane.service_local_addr("udp-main").unwrap(), original_listen);
    assert_eq!(outcome.unchanged(), 1);
    assert_eq!(outcome.updated(), 1);
    assert_eq!(outcome.rebound(), 0);
}

#[test]
fn python_nonblocking_forward_returns_empty_when_app_socket_is_quiet() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
        "fixed",
        0,
        1,
        0,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();

    let outcomes = dataplane
        .forward_available_for_service_nonblocking("udp-main", 8)
        .unwrap();

    assert!(outcomes.is_empty());
}

#[test]
fn python_facing_scheduler_config_accepts_all_compiled_modes() {
    for mode in [
        "round_robin",
        "weighted_round_robin",
        "lowest_latency",
        "loss_aware",
        "capacity_aware",
        "least_queue",
        "earliest_completion_first",
        "blocking_estimation",
        "balanced",
        "adaptive",
    ] {
        let scheduler = PySchedulerConfig::new(mode).unwrap();
        assert_eq!(scheduler.mode(), mode);
    }
}
