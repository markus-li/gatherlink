use std::net::{SocketAddr, UdpSocket};
use std::time::Duration;

use gatherlink_dataplane::udp_service::{UdpServiceConfig, UdpServiceError, UserlandUdpService};

#[test]
fn binds_loopback_udp_socket_without_root() {
    let listen: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
    let config = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();

    let service = UserlandUdpService::bind(config).unwrap();

    assert_eq!(service.local_addr().unwrap().ip(), listen.ip());
}

#[test]
fn receives_userland_udp_datagram() {
    let listen: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
    let config = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();
    let service = UserlandUdpService::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    sender
        .send_to(b"gatherlink-core-test", service.local_addr().unwrap())
        .unwrap();

    let mut buffer = [0_u8; 64];
    let (length, source) = service.recv_from(&mut buffer).unwrap();

    assert_eq!(&buffer[..length], b"gatherlink-core-test");
    assert_eq!(source, sender.local_addr().unwrap());
}

#[test]
fn emits_userland_udp_datagram_to_target() {
    let target_socket = UdpSocket::bind("127.0.0.1:0").unwrap();
    target_socket
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let config = UdpServiceConfig::new(
        "udp-main",
        Some("127.0.0.1:0".parse().unwrap()),
        target_socket.local_addr().unwrap(),
    )
    .unwrap();
    let service = UserlandUdpService::bind(config).unwrap();

    service.emit_to_target(b"target-payload").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target_socket.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"target-payload");
}

#[test]
fn forwards_ipv6_loopback_udp_datagram() {
    let target_socket = UdpSocket::bind("[::1]:0").unwrap();
    target_socket
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let config = UdpServiceConfig::new(
        "udp-v6",
        Some("[::1]:0".parse().unwrap()),
        target_socket.local_addr().unwrap(),
    )
    .unwrap();
    let service = UserlandUdpService::bind(config).unwrap();
    let sender = UdpSocket::bind("[::1]:0").unwrap();

    sender
        .send_to(b"ipv6-userland-udp", service.local_addr().unwrap())
        .unwrap();
    let mut receive_buffer = [0_u8; 64];
    let (length, source) = service.recv_from(&mut receive_buffer).unwrap();

    assert_eq!(source.ip(), sender.local_addr().unwrap().ip());
    assert_eq!(&receive_buffer[..length], b"ipv6-userland-udp");

    service.emit_to_target(&receive_buffer[..length]).unwrap();
    let mut target_buffer = [0_u8; 64];
    let (target_length, _source) = target_socket.recv_from(&mut target_buffer).unwrap();
    assert_eq!(&target_buffer[..target_length], b"ipv6-userland-udp");
}

#[test]
fn clones_socket_when_listener_is_unchanged() {
    let first_target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
    let second_target: SocketAddr = "127.0.0.1:51821".parse().unwrap();
    let config = UdpServiceConfig::new("udp-main", Some("127.0.0.1:0".parse().unwrap()), first_target).unwrap();
    let service = UserlandUdpService::bind(config).unwrap();
    let listen = service.local_addr().unwrap();
    let updated_config = UdpServiceConfig::new("udp-main", Some(listen), second_target).unwrap();

    let updated = service.clone_with_config(updated_config).unwrap();

    assert_eq!(updated.local_addr().unwrap(), listen);
    assert_eq!(updated.config().target(), second_target);
}

#[test]
fn rejects_empty_service_name() {
    let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();

    let err = UdpServiceConfig::new("  ", None, target).unwrap_err();

    assert!(matches!(err, UdpServiceError::EmptyServiceName));
}
