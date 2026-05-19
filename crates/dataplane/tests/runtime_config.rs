use std::net::SocketAddr;

use gatherlink_dataplane::runtime_config::{CorePathConfig, CoreRuntimeConfig};
use gatherlink_dataplane::udp_service::{UdpServiceConfig, UdpServiceError};
use gatherlink_protocol::frame::{FRAGMENT_EXTENSION_LEN, V1_HEADER_LEN};

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
fn rejects_duplicate_path_ids() {
    let service = UdpServiceConfig::new(
        "udp-main",
        Some("127.0.0.1:55180".parse().unwrap()),
        "127.0.0.1:51820".parse().unwrap(),
    )
    .unwrap();
    let first = CorePathConfig::new(7, 0, 1200, false).unwrap();
    let second = CorePathConfig::new(7, 1, 1200, false).unwrap();

    let err = CoreRuntimeConfig::new_with_paths(vec![service], vec![first, second]).unwrap_err();

    assert!(matches!(err, UdpServiceError::DuplicatePathId(7)));
}

#[test]
fn rejects_path_mtu_that_cannot_carry_a_fragment() {
    let err = CorePathConfig::new(7, 0, V1_HEADER_LEN + FRAGMENT_EXTENSION_LEN, false).unwrap_err();

    assert!(matches!(err, UdpServiceError::PathMtuTooSmall { path_id: 7, .. }));
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
