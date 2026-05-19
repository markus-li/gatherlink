use std::net::SocketAddr;

use gatherlink_dataplane::runtime_config::{
    CorePathConfig, CoreRuntimeConfig, PathSchedulerPrimitives, PathSchedulerState,
};
use gatherlink_dataplane::udp_service::{UdpServiceConfig, UdpServiceError};
use gatherlink_protocol::frame::{FRAGMENT_METADATA_LEN, V1_HEADER_LEN};

#[test]
fn rejects_duplicate_service_names() {
    let listen: SocketAddr = "127.0.0.1:55180".parse().unwrap();
    let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
    let first = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();
    let second = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();

    let err = CoreRuntimeConfig::new(vec![first, second]).unwrap_err();

    assert!(matches!(err, UdpServiceError::DuplicateServiceName(name) if name == "udp-main"));
}

#[test]
fn rejects_duplicate_listen_addresses() {
    let listen: SocketAddr = "127.0.0.1:55180".parse().unwrap();
    let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
    let first = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();
    let second = UdpServiceConfig::new("udp-secondary", Some(listen), target).unwrap();

    let err = CoreRuntimeConfig::new(vec![first, second]).unwrap_err();

    assert!(matches!(err, UdpServiceError::DuplicateListenAddress(addr) if addr == listen));
}

#[test]
fn assigns_user_service_ids_without_colliding_with_explicit_ids() {
    let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
    let explicit = UdpServiceConfig::new_with_service_id(257, "explicit", None, target).unwrap();
    let automatic = UdpServiceConfig::new("auto", None, target).unwrap();

    let config = CoreRuntimeConfig::new(vec![explicit, automatic]).unwrap();

    assert_eq!(config.services()[0].service_id(), 257);
    assert_eq!(config.services()[1].service_id(), 256);
}

#[test]
fn rejects_reserved_explicit_service_ids() {
    let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();

    let err =
        UdpServiceConfig::new_with_service_id(7, "reserved", Some("127.0.0.1:0".parse().unwrap()), target).unwrap_err();

    assert!(matches!(err, UdpServiceError::ReservedServiceId { service_id: 7, .. }));
}

#[test]
fn rejects_duplicate_path_ids() {
    let service = UdpServiceConfig::new(
        "udp-main",
        Some("127.0.0.1:55180".parse().unwrap()),
        "127.0.0.1:51820".parse().unwrap(),
    )
    .unwrap();
    let first = CorePathConfig::new(7, 1200, false).unwrap();
    let second = CorePathConfig::new(7, 1200, false).unwrap();

    let err = CoreRuntimeConfig::new_with_paths(vec![service], vec![first, second]).unwrap_err();

    assert!(matches!(err, UdpServiceError::DuplicatePathId(7)));
}

#[test]
fn rejects_path_mtu_that_cannot_carry_a_fragment() {
    let err = CorePathConfig::new(7, V1_HEADER_LEN + FRAGMENT_METADATA_LEN, false).unwrap_err();

    assert!(matches!(err, UdpServiceError::PathMtuTooSmall { path_id: 7, .. }));
}

#[test]
fn accepts_compiled_scheduler_primitives() {
    let primitives = PathSchedulerPrimitives::new(
        Some(3_000_000),
        Some(1_500_000),
        Some(12_000),
        25_000,
        150_000,
        64,
        524_288,
    );
    let path = CorePathConfig::new_with_scheduler_primitives(9, 1200, true, PathSchedulerState::Active, 3, primitives)
        .unwrap();

    assert_eq!(path.weight(), 3);
    assert_eq!(path.primitives().tx_capacity_bps(), Some(3_000_000));
    assert_eq!(path.primitives().rx_capacity_bps(), Some(1_500_000));
    assert_eq!(path.primitives().latency_us(), Some(12_000));
    assert_eq!(path.primitives().loss_ppm(), 25_000);
    assert_eq!(path.primitives().reorder_hold_us(), 150_000);
    assert_eq!(path.primitives().max_in_flight_packets(), 64);
    assert_eq!(path.primitives().max_in_flight_bytes(), 524_288);
}

#[test]
fn accepts_ipv6_udp_service_addresses() {
    let listen: SocketAddr = "[::1]:55180".parse().unwrap();
    let target: SocketAddr = "[::1]:51820".parse().unwrap();
    let service = UdpServiceConfig::new("udp-v6", Some(listen), target).unwrap();

    let config = CoreRuntimeConfig::new(vec![service]).unwrap();

    assert_eq!(config.services()[0].listen(), Some(listen));
    assert_eq!(config.services()[0].target(), target);
}
