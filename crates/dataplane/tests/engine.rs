use std::net::UdpSocket;
use std::time::Duration;

use gatherlink_dataplane::engine::{CoreDataplane, ReapplyOutcome};
use gatherlink_dataplane::runtime_config::{CorePathConfig, CoreRuntimeConfig, PathSchedulerState};
use gatherlink_dataplane::udp_service::UdpServiceConfig;

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
    assert_eq!(outcome.frame_count, 5);
    assert_eq!(outcome.fragment_count, 4);
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
    assert_eq!(outcome.frame_count, 5);
    assert_eq!(outcome.fragment_count, 4);
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
