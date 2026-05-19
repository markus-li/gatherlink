use std::net::UdpSocket;
use std::time::Duration;

use gatherlink_dataplane::engine::{CoreDataplane, ReapplyOutcome};
use gatherlink_dataplane::runtime_config::{CorePathConfig, CoreRuntimeConfig, PathSchedulerState};
use gatherlink_dataplane::udp_service::UdpServiceConfig;
use gatherlink_protocol::control::{ControlMessage, ControlPayload, PathMetadata};

#[test]
fn forwards_one_udp_payload_through_core_frame_boundary() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config =
        CoreRuntimeConfig::single_udp_service("udp-main", "127.0.0.1:0".parse().unwrap(), target.local_addr().unwrap())
            .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    sender.send_to(b"plain-user-udp", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"plain-user-udp");
    assert_eq!(outcome.service, "udp-main");
    assert_eq!(outcome.payload_len, b"plain-user-udp".len());
    assert_eq!(outcome.sequence, 1);
    assert_eq!(outcome.path_id, 0);
    assert_eq!(outcome.frame_count, 1);
    assert_eq!(outcome.batch_count, 0);
    assert_eq!(outcome.fragment_count, 0);

    let metrics = dataplane.metrics_snapshot();
    assert_eq!(metrics.services["udp-main"].packets, 1);
    assert_eq!(metrics.services["udp-main"].bytes, b"plain-user-udp".len() as u64);
    assert_eq!(metrics.services["udp-main"].tx_packets, 1);
    assert_eq!(metrics.services["udp-main"].tx_bytes, b"plain-user-udp".len() as u64);
    assert_eq!(metrics.paths[&0].packets, 1);
    assert_eq!(metrics.paths[&0].bytes, b"plain-user-udp".len() as u64);
    assert_eq!(metrics.paths[&0].tx_packets, 1);
    assert_eq!(metrics.paths[&0].tx_bytes, b"plain-user-udp".len() as u64);
}

#[test]
fn python_applied_service_disable_stops_service_traffic() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config =
        CoreRuntimeConfig::single_udp_service("udp-main", "127.0.0.1:0".parse().unwrap(), target.local_addr().unwrap())
            .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();

    dataplane.disable_service(256, "python policy stopped service");
    let disabled = dataplane.disabled_services_snapshot();
    assert!(disabled[&256].contains("python policy"));
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    sender.send_to(b"must-stop", service_addr).unwrap();
    let err = dataplane.forward_one_for_service("udp-main").unwrap_err();
    assert!(err.to_string().contains("service id 256 is disabled"));
}

#[test]
fn telemetry_records_received_data_and_control_facts() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config =
        CoreRuntimeConfig::single_udp_service("udp-main", "127.0.0.1:0".parse().unwrap(), target.local_addr().unwrap())
            .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let control = ControlPayload::new(vec![ControlMessage::PathMetadata(
        PathMetadata::new(7, "path-a").unwrap(),
    )])
    .unwrap()
    .encode()
    .unwrap();

    dataplane.observe_received_data_frame(256, 7, 1, 1200);
    dataplane.observe_received_reserved_service_payload(1, 7, 99, control.clone(), 128);

    let metrics = dataplane.metrics_snapshot();
    assert_eq!(metrics.paths[&7].rx_packets, 1);
    assert_eq!(metrics.paths[&7].rx_bytes, 1200);
    assert_eq!(metrics.control_metadata.received.frames, 1);
    assert_eq!(metrics.control_metadata.received.bytes, 128);
    assert_eq!(metrics.control_metadata.path_control[&7].rx.frames, 1);
    assert_eq!(metrics.control_metadata.path_control[&7].rx.bytes, 128);
    let events = dataplane.drain_reserved_service_events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].service_id, 1);
    assert_eq!(events[0].path_id, 7);
    assert_eq!(events[0].sequence, 99);
    assert_eq!(events[0].payload, control);
}

#[test]
fn control_payload_duplication_uses_all_enabled_paths_with_one_sequence() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let path_a_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    let path_b_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    let path_c_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![
            CorePathConfig::new_with_scheduler(10, 0, 1200, true, PathSchedulerState::Active, 1)
                .unwrap()
                .with_transport("127.0.0.1:0".parse().unwrap(), path_a_remote.local_addr().unwrap()),
            CorePathConfig::new_with_scheduler(20, 0, 1200, true, PathSchedulerState::Busy, 1)
                .unwrap()
                .with_transport("127.0.0.1:0".parse().unwrap(), path_b_remote.local_addr().unwrap()),
            CorePathConfig::new_with_scheduler(30, 0, 1200, false, PathSchedulerState::Disabled, 1)
                .unwrap()
                .with_transport("127.0.0.1:0".parse().unwrap(), path_c_remote.local_addr().unwrap()),
        ],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let control = ControlPayload::new(vec![ControlMessage::PathMetadata(
        PathMetadata::new(10, "path-a").unwrap(),
    )])
    .unwrap()
    .encode()
    .unwrap();

    dataplane.set_service_scheduler(1, gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(0, 0));
    let plans = dataplane.transmit_service_payload(1, control.clone()).unwrap();

    assert_eq!(plans.len(), 2);
    assert_eq!(plans.iter().map(|plan| plan.path_id).collect::<Vec<_>>(), vec![10, 20]);
    assert!(plans.iter().all(|plan| plan.sequence == plans[0].sequence));
    assert!(plans.iter().all(|plan| plan.payload_len == control.len()));
}

#[test]
fn forwards_ipv6_udp_payload_through_core_frame_boundary() {
    let target = UdpSocket::bind("[::1]:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config =
        CoreRuntimeConfig::single_udp_service("udp-v6", "[::1]:0".parse().unwrap(), target.local_addr().unwrap())
            .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("[::1]:0").unwrap();
    let service_addr = dataplane.service("udp-v6").unwrap().local_addr().unwrap();

    sender.send_to(b"plain-ipv6-udp", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-v6").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"plain-ipv6-udp");
    assert_eq!(outcome.service, "udp-v6");
    assert_eq!(outcome.source.ip(), sender.local_addr().unwrap().ip());
    assert_eq!(outcome.target, target.local_addr().unwrap());
}

#[test]
fn coalesces_tiny_udp_payloads_into_one_batch_frame() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![CorePathConfig::new(7, 0, 1200, false).unwrap()],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    sender.send_to(b"a", service_addr).unwrap();
    sender.send_to(b"bb", service_addr).unwrap();
    sender.send_to(b"ccc", service_addr).unwrap();
    let outcomes = dataplane.forward_available_for_service("udp-main", 3).unwrap();

    let mut received = Vec::new();
    for _ in 0..3 {
        let mut buffer = [0_u8; 8];
        let (length, _source) = target.recv_from(&mut buffer).unwrap();
        received.push(buffer[..length].to_vec());
    }
    assert_eq!(received, vec![b"a".to_vec(), b"bb".to_vec(), b"ccc".to_vec()]);
    assert_eq!(outcomes.len(), 3);
    assert!(outcomes.iter().all(|outcome| outcome.path_id == 7));
    assert!(outcomes.iter().all(|outcome| outcome.frame_count == 1));
    assert!(outcomes.iter().all(|outcome| outcome.batch_count == 3));
    assert!(outcomes.iter().all(|outcome| outcome.fragment_count == 0));
}

#[test]
fn round_robin_scheduler_rotates_across_eligible_paths() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![
            CorePathConfig::new(10, 0, 1200, false).unwrap(),
            CorePathConfig::new(20, 0, 1200, false).unwrap(),
        ],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    for payload in [
        b"one".as_slice(),
        b"two".as_slice(),
        b"three".as_slice(),
        b"four".as_slice(),
    ] {
        sender.send_to(payload, service_addr).unwrap();
    }
    let outcomes = dataplane.forward_available_for_service("udp-main", 4).unwrap();

    for _ in 0..4 {
        let mut buffer = [0_u8; 8];
        target.recv_from(&mut buffer).unwrap();
    }
    let path_ids = outcomes.iter().map(|outcome| outcome.path_id).collect::<Vec<_>>();
    assert_eq!(path_ids, vec![10, 20, 10, 20]);
    assert!(outcomes.iter().all(|outcome| outcome.batch_count == 0));

    let metrics = dataplane.metrics_snapshot();
    assert_eq!(metrics.services["udp-main"].packets, 4);
    assert_eq!(metrics.paths[&10].packets, 2);
    assert_eq!(metrics.paths[&20].packets, 2);
}

#[test]
fn round_robin_scheduler_honors_compiled_path_weights_and_disabled_paths() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![
            CorePathConfig::new_with_scheduler(10, 0, 1200, true, PathSchedulerState::Active, 2).unwrap(),
            CorePathConfig::new_with_scheduler(99, 0, 1200, false, PathSchedulerState::Disabled, 1).unwrap(),
            CorePathConfig::new_with_scheduler(20, 0, 1200, true, PathSchedulerState::Active, 1).unwrap(),
        ],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    for payload in [
        b"one".as_slice(),
        b"two".as_slice(),
        b"three".as_slice(),
        b"four".as_slice(),
    ] {
        sender.send_to(payload, service_addr).unwrap();
    }
    let outcomes = dataplane.forward_available_for_service("udp-main", 4).unwrap();

    for _ in 0..4 {
        let mut buffer = [0_u8; 8];
        target.recv_from(&mut buffer).unwrap();
    }
    let path_ids = outcomes.iter().map(|outcome| outcome.path_id).collect::<Vec<_>>();
    assert_eq!(path_ids, vec![10, 10, 20, 10]);
}

#[test]
fn fragments_oversized_udp_payload_when_no_path_can_fit_it_whole() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![CorePathConfig::new(9, 0, 100, false).unwrap()],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();
    let payload = vec![b'x'; 200];

    sender.send_to(&payload, service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 256];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], payload.as_slice());
    assert_eq!(outcome.path_id, 9);
    assert_eq!(outcome.frame_count, 4);
    assert_eq!(outcome.fragment_count, 3);
    assert_eq!(outcome.batch_count, 0);
}

#[test]
fn fragments_onto_available_path_when_whole_fit_path_is_busy() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![
            CorePathConfig::new(1, 0, 300, true).unwrap(),
            CorePathConfig::new(2, 0, 80, false).unwrap(),
        ],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();
    let payload = vec![b'y'; 100];

    sender.send_to(&payload, service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 128];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], payload.as_slice());
    assert_eq!(outcome.path_id, 2);
    assert_eq!(outcome.frame_count, 4);
    assert_eq!(outcome.fragment_count, 3);
}

#[test]
fn reapply_updates_target_without_rebinding_listener() {
    let first_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let second_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    second_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let config = CoreRuntimeConfig::single_udp_service(
        "udp-main",
        "127.0.0.1:0".parse().unwrap(),
        first_target.local_addr().unwrap(),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let original_listen = dataplane.service("udp-main").unwrap().local_addr().unwrap();
    let updated_config = CoreRuntimeConfig::new(vec![UdpServiceConfig::new(
        "udp-main",
        Some(original_listen),
        second_target.local_addr().unwrap(),
    )
    .unwrap()])
    .unwrap();

    let outcome = dataplane.reapply_config(updated_config).unwrap();
    let updated_listen = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    assert_eq!(
        outcome,
        ReapplyOutcome {
            unchanged: 0,
            updated: 1,
            rebound: 0,
            added: 0,
            removed: 0,
        },
    );
    assert_eq!(updated_listen, original_listen);
}

#[test]
fn reapply_can_add_and_remove_services() {
    let first_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let second_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::single_udp_service(
        "udp-main",
        "127.0.0.1:0".parse().unwrap(),
        first_target.local_addr().unwrap(),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let replacement = CoreRuntimeConfig::new(vec![UdpServiceConfig::new(
        "udp-secondary",
        Some("127.0.0.1:0".parse().unwrap()),
        second_target.local_addr().unwrap(),
    )
    .unwrap()])
    .unwrap();

    let outcome = dataplane.reapply_config(replacement).unwrap();

    assert_eq!(
        outcome,
        ReapplyOutcome {
            unchanged: 0,
            updated: 0,
            rebound: 0,
            added: 1,
            removed: 1,
        },
    );
    assert!(dataplane.service("udp-main").is_none());
    assert!(dataplane.service("udp-secondary").is_some());
}
