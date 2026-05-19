use std::net::{SocketAddr, UdpSocket};
use std::thread;
use std::time::Duration;

use gatherlink_crypto::envelope::{
    decrypt_packet_without_replay, ENCRYPTED_DATA_HEADER_LEN, PACKET_TYPE_ENCRYPTED_DATA_V1,
};
use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::runtime_config::{
    CorePathConfig, CoreRuntimeConfig, SchedulerConfig, TransportSecurityConfig,
};
use gatherlink_dataplane::udp_service::UdpServiceConfig;
use gatherlink_protocol::control::{ControlMessage, ControlPayload, PathMetadata};
use gatherlink_protocol::frame::{Frame, FrameKind};

fn reserve_loopback_addr() -> SocketAddr {
    UdpSocket::bind("127.0.0.1:0").unwrap().local_addr().unwrap()
}

fn service(name: &str, target: SocketAddr) -> UdpServiceConfig {
    UdpServiceConfig::new(name, Some("127.0.0.1:0".parse().unwrap()), target).unwrap()
}

fn path(path_id: u16, bind: SocketAddr, remote: SocketAddr) -> CorePathConfig {
    CorePathConfig::new(path_id, 0, 1200, false)
        .unwrap()
        .with_transport(bind, remote)
}

fn secure_config(
    services: Vec<UdpServiceConfig>,
    paths: Vec<CorePathConfig>,
    receiver_index: u32,
    send_key: [u8; 32],
    receive_key: [u8; 32],
) -> CoreRuntimeConfig {
    CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        services,
        paths,
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            receiver_index,
            send_key,
            receive_key,
        },
    )
    .unwrap()
}

#[test]
fn path_transport_sends_encoded_gatherlink_frame_not_raw_payload() {
    let path_bind = reserve_loopback_addr();
    let path_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    path_remote.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![path(42, path_bind, path_remote.local_addr().unwrap())],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    app_sender.send_to(b"wireguard-like-payload", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 1500];
    let (length, _source) = path_remote.recv_from(&mut buffer).unwrap();
    assert_ne!(&buffer[..length], b"wireguard-like-payload");
    let frame = Frame::decode(&buffer[..length]).unwrap();
    assert_eq!(frame.header.kind, FrameKind::Data);
    assert_eq!(frame.header.service_id, 256);
    assert_eq!(frame.header.path_id, 42);
    assert_eq!(frame.payload, b"wireguard-like-payload");
    assert_eq!(outcome.path_id, 42);
    assert_eq!(outcome.payload_len, b"wireguard-like-payload".len());
}

#[test]
fn encrypted_path_transport_sends_aead_packet_not_plain_frame() {
    let path_bind = reserve_loopback_addr();
    let path_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    path_remote.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let send_key = [0x33_u8; 32];
    let config = secure_config(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![path(42, path_bind, path_remote.local_addr().unwrap())],
        1234,
        send_key,
        [0x44_u8; 32],
    );
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    app_sender.send_to(b"secret-payload", service_addr).unwrap();
    dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 1500];
    let (length, _source) = path_remote.recv_from(&mut buffer).unwrap();
    let packet = &buffer[..length];
    assert_eq!(packet[0], PACKET_TYPE_ENCRYPTED_DATA_V1);
    let decrypted = decrypt_packet_without_replay(&send_key, packet).unwrap();
    assert_eq!(decrypted.receiver_index, 1234);
    assert!(Frame::decode(&decrypted.plaintext).is_err());
    let frame = Frame::decode_v2(&decrypted.plaintext).unwrap();
    assert_eq!(frame.header.service_id, 256);
    assert_eq!(frame.header.path_id, 42);
    assert_eq!(frame.payload, b"secret-payload");
}

#[test]
fn two_dataplanes_forward_payload_over_path_transport_to_fixed_target() {
    let client_path_addr = reserve_loopback_addr();
    let server_path_addr = reserve_loopback_addr();
    let remote_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    remote_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();

    let client_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![path(7, client_path_addr, server_path_addr)],
    )
    .unwrap();
    let server_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![path(7, server_path_addr, client_path_addr)],
    )
    .unwrap();
    let mut client = CoreDataplane::bind(client_config).unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let client_service_addr = client.service("udp-main").unwrap().local_addr().unwrap();

    app_sender
        .send_to(b"fixed-target-forward", client_service_addr)
        .unwrap();
    client.forward_one_for_service("udp-main").unwrap();
    let delivered = server.receive_available_from_paths(8).unwrap();

    let mut buffer = [0_u8; 128];
    let (length, _source) = remote_target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"fixed-target-forward");
    assert_eq!(delivered.len(), 1);
    assert_eq!(delivered[0].service, "udp-main");
    assert_eq!(delivered[0].target, remote_target.local_addr().unwrap());
    assert_eq!(delivered[0].path_id, 7);
    assert_eq!(
        server.metrics_snapshot().services.get("udp-main").unwrap().rx_packets,
        1
    );
}

#[test]
fn encrypted_path_transport_carries_aead_packet_and_receiver_decrypts_frame() {
    let client_path_addr = reserve_loopback_addr();
    let server_path_addr = reserve_loopback_addr();
    let remote_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    remote_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let client_to_server = [0x11_u8; 32];
    let server_to_client = [0x22_u8; 32];

    let client_config = secure_config(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![path(7, client_path_addr, server_path_addr)],
        99,
        client_to_server,
        server_to_client,
    );
    let server_config = secure_config(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![path(7, server_path_addr, client_path_addr)],
        99,
        server_to_client,
        client_to_server,
    );
    let mut client = CoreDataplane::bind(client_config).unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    let sniffer = UdpSocket::bind("127.0.0.1:0").unwrap();
    let client_service_addr = client.service("udp-main").unwrap().local_addr().unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    app_sender
        .send_to(b"encrypted-fixed-target", client_service_addr)
        .unwrap();
    client.forward_one_for_service("udp-main").unwrap();

    let delivered = server.receive_available_from_paths(8).unwrap();
    let mut buffer = [0_u8; 128];
    let (length, _source) = remote_target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"encrypted-fixed-target");
    assert_eq!(delivered.len(), 1);
    assert_eq!(delivered[0].path_id, 7);

    // A random plaintext frame sent to an encrypted transport is silently ignored.
    let plaintext = Frame::data(0, 256, 7, 0, 123, b"not-authenticated".to_vec())
        .unwrap()
        .encode()
        .unwrap();
    sniffer.send_to(&plaintext, server_path_addr).unwrap();
    assert!(server.receive_available_from_paths(8).unwrap().is_empty());

    // The packet format itself is validated by the crypto crate: clear type,
    // receiver index, counter, ciphertext, and tag around the inner frame.
    let compact_v2 = Frame::decode(&plaintext).unwrap().encode_v2().unwrap();
    let packet =
        gatherlink_crypto::envelope::encrypt_frame_with_counter(99, &client_to_server, 77, &compact_v2).unwrap();
    assert_eq!(packet[0], PACKET_TYPE_ENCRYPTED_DATA_V1);
    assert!(packet.len() >= ENCRYPTED_DATA_HEADER_LEN + 16);
    let decrypted = decrypt_packet_without_replay(&client_to_server, &packet).unwrap();
    assert_eq!(decrypted.receiver_index, 99);
    assert_eq!(decrypted.counter, 77);
    assert_eq!(decrypted.plaintext, compact_v2);
    assert_eq!(
        Frame::decode_v2(&decrypted.plaintext).unwrap().payload,
        b"not-authenticated"
    );
}

#[test]
fn user_service_fanout_is_deduped_before_udp_delivery() {
    let client_path_a = reserve_loopback_addr();
    let server_path_a = reserve_loopback_addr();
    let client_path_b = reserve_loopback_addr();
    let server_path_b = reserve_loopback_addr();
    let remote_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    remote_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();

    let client_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![
            path(1, client_path_a, server_path_a),
            path(2, client_path_b, server_path_b),
        ],
    )
    .unwrap();
    let server_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![
            path(1, server_path_a, client_path_a),
            path(2, server_path_b, client_path_b),
        ],
    )
    .unwrap();
    let mut client = CoreDataplane::bind(client_config).unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    client.set_service_scheduler(
        256,
        gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(2, 64),
    );
    server.set_service_scheduler(
        256,
        gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(2, 64),
    );
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let client_service_addr = client.service("udp-main").unwrap().local_addr().unwrap();

    app_sender.send_to(b"dedupe-me", client_service_addr).unwrap();
    let forwarded = client.forward_one_for_service("udp-main").unwrap();
    assert_eq!(forwarded.path_id, 1);
    let delivered = server.receive_available_from_paths(8).unwrap();

    let mut buffer = [0_u8; 128];
    let (length, _source) = remote_target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"dedupe-me");
    assert!(remote_target.recv_from(&mut buffer).is_err());
    assert_eq!(delivered.len(), 1);
    let snapshot = server.metrics_snapshot();
    let service = snapshot.services.get("udp-main").unwrap();
    assert_eq!(service.rx_packets, 1);
    assert_eq!(service.expected_duplicate_packets, 1);
    assert_eq!(service.duplicate_packets, 0);
    assert_eq!(
        snapshot
            .paths
            .values()
            .map(|path| path.expected_duplicate_packets)
            .sum::<u64>(),
        1
    );
}

#[test]
fn unexpected_duplicate_is_counted_separately_from_expected_fanout() {
    let server_path = reserve_loopback_addr();
    let remote_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    remote_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let server_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![path(1, server_path, reserve_loopback_addr())],
    )
    .unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let encoded = Frame::data(0, 256, 1, 0, 99, b"surprise-duplicate".to_vec())
        .unwrap()
        .encode()
        .unwrap();

    sender.send_to(&encoded, server_path).unwrap();
    let delivered = server.receive_available_from_paths(8).unwrap();
    assert_eq!(delivered.len(), 1);
    sender.send_to(&encoded, server_path).unwrap();
    let delivered = server.receive_available_from_paths(8).unwrap();
    assert!(delivered.is_empty());

    let snapshot = server.metrics_snapshot();
    let service = snapshot.services.get("udp-main").unwrap();
    assert_eq!(service.rx_packets, 1);
    assert_eq!(service.expected_duplicate_packets, 0);
    assert_eq!(service.unexpected_duplicate_packets, 1);
    assert_eq!(service.duplicate_packets, 1);
}

#[test]
fn oversized_user_service_fanout_fragments_and_dedupes_before_udp_delivery() {
    let client_path_a = reserve_loopback_addr();
    let server_path_a = reserve_loopback_addr();
    let client_path_b = reserve_loopback_addr();
    let server_path_b = reserve_loopback_addr();
    let remote_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    remote_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();

    let client_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![
            path(1, client_path_a, server_path_a),
            path(2, client_path_b, server_path_b),
        ],
    )
    .unwrap();
    let server_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![
            path(1, server_path_a, client_path_a),
            path(2, server_path_b, client_path_b),
        ],
    )
    .unwrap();
    let mut client = CoreDataplane::bind(client_config).unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    client.set_service_scheduler(
        256,
        gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(2, 0),
    );
    server.set_service_scheduler(
        256,
        gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(2, 0),
    );
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let client_service_addr = client.service("udp-main").unwrap().local_addr().unwrap();
    let payload = vec![b'x'; 3000];

    app_sender.send_to(&payload, client_service_addr).unwrap();
    let forwarded = client.forward_available_for_service("udp-main", 16).unwrap();
    assert!(forwarded.len() > 2);
    let mut delivered = Vec::new();
    for _attempt in 0..10 {
        delivered.extend(server.receive_available_from_paths(32).unwrap());
        if !delivered.is_empty() {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    for _attempt in 0..10 {
        server.receive_available_from_paths(32).unwrap();
        thread::sleep(Duration::from_millis(10));
    }

    let mut buffer = vec![0_u8; 4096];
    let (length, _source) = remote_target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], payload.as_slice());
    assert!(remote_target.recv_from(&mut buffer).is_err());
    assert_eq!(delivered.len(), 1);
    let snapshot = server.metrics_snapshot();
    let service = snapshot.services.get("udp-main").unwrap();
    assert_eq!(service.rx_packets, 1);
}

#[test]
fn reserved_service_fanout_keeps_each_copy_for_python() {
    let client_path_a = reserve_loopback_addr();
    let server_path_a = reserve_loopback_addr();
    let client_path_b = reserve_loopback_addr();
    let server_path_b = reserve_loopback_addr();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let client_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![
            path(1, client_path_a, server_path_a),
            path(2, client_path_b, server_path_b),
        ],
    )
    .unwrap();
    let server_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![
            path(1, server_path_a, client_path_a),
            path(2, server_path_b, client_path_b),
        ],
    )
    .unwrap();
    let mut client = CoreDataplane::bind(client_config).unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    client.set_service_scheduler(1, gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(2, 0));

    let plans = client.transmit_service_payload(1, b"control-copy".to_vec()).unwrap();
    assert_eq!(plans.len(), 2);
    let delivered = server.receive_available_from_paths(8).unwrap();
    assert!(delivered.is_empty());
    let events = server.drain_reserved_service_events();
    assert_eq!(events.len(), 2);
    assert!(events.iter().all(|event| event.payload == b"control-copy"));
    assert_eq!(
        events.iter().map(|event| event.sequence).collect::<Vec<_>>(),
        vec![1, 1]
    );
}

#[test]
fn nonblocking_forward_returns_empty_when_app_socket_is_quiet() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::new(vec![service("udp-main", target.local_addr().unwrap())]).unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();

    let outcomes = dataplane
        .forward_available_for_service_nonblocking("udp-main", 8)
        .unwrap();

    assert!(outcomes.is_empty());
}

#[test]
fn all_paths_service_scheduler_duplicates_payload_over_path_transports() {
    let path_a_bind = reserve_loopback_addr();
    let path_a_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    let path_b_bind = reserve_loopback_addr();
    let path_b_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    path_a_remote
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    path_b_remote
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![
            path(1, path_a_bind, path_a_remote.local_addr().unwrap()),
            path(2, path_b_bind, path_b_remote.local_addr().unwrap()),
        ],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let control = ControlPayload::new(vec![ControlMessage::PathMetadata(PathMetadata {
        path_id: 1,
        name: "path-a".to_owned(),
    })])
    .unwrap()
    .encode()
    .unwrap();

    dataplane.set_service_scheduler(1, gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(0, 0));
    let plans = dataplane.transmit_service_payload(1, control.clone()).unwrap();

    assert_eq!(plans.len(), 2);
    for remote in [path_a_remote, path_b_remote] {
        let mut buffer = [0_u8; 1500];
        let (length, _source) = remote.recv_from(&mut buffer).unwrap();
        let frame = Frame::decode(&buffer[..length]).unwrap();
        assert_eq!(frame.header.kind, FrameKind::Data);
        assert_eq!(frame.payload, control);
    }
    assert_eq!(dataplane.metrics_snapshot().control_metadata.sent.frames, 2);
}

#[test]
fn service_scheduler_fanout_below_bytes_only_duplicates_small_payloads() {
    let path_a_bind = reserve_loopback_addr();
    let path_a_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    let path_b_bind = reserve_loopback_addr();
    let path_b_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    path_a_remote
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    path_b_remote
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![
            path(1, path_a_bind, path_a_remote.local_addr().unwrap()),
            path(2, path_b_bind, path_b_remote.local_addr().unwrap()),
        ],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    dataplane.set_service_scheduler(9, gatherlink_dataplane::udp_service::ServiceSchedulerConfig::new(2, 4));

    let small_plans = dataplane.transmit_service_payload(9, b"tiny".to_vec()).unwrap();
    assert_eq!(small_plans.len(), 2);
    for remote in [&path_a_remote, &path_b_remote] {
        let mut buffer = [0_u8; 1500];
        let (length, _source) = remote.recv_from(&mut buffer).unwrap();
        let frame = Frame::decode(&buffer[..length]).unwrap();
        assert_eq!(frame.header.service_id, 9);
        assert_eq!(frame.payload, b"tiny");
    }

    path_b_remote
        .set_read_timeout(Some(Duration::from_millis(100)))
        .unwrap();
    let large_plans = dataplane
        .transmit_service_payload(9, b"larger-than-threshold".to_vec())
        .unwrap();
    assert_eq!(large_plans.len(), 1);
    assert_eq!(large_plans[0].path_id, 1);
    let mut buffer = [0_u8; 1500];
    let (length, _source) = path_a_remote.recv_from(&mut buffer).unwrap();
    let frame = Frame::decode(&buffer[..length]).unwrap();
    assert_eq!(frame.payload, b"larger-than-threshold");
    assert!(path_b_remote.recv_from(&mut buffer).is_err());
}

#[test]
fn python_composed_service_payload_is_sent_through_one_scheduled_path_transport() {
    let path_a_bind = reserve_loopback_addr();
    let path_a_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    let path_b_bind = reserve_loopback_addr();
    let path_b_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    path_a_remote
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    path_b_remote
        .set_read_timeout(Some(Duration::from_millis(100)))
        .unwrap();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![
            path(1, path_a_bind, path_a_remote.local_addr().unwrap()),
            path(2, path_b_bind, path_b_remote.local_addr().unwrap()),
        ],
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let payload = br#"{"type":"status_request"}"#.to_vec();

    let plans = dataplane.transmit_service_payload(8, payload.clone()).unwrap();

    assert_eq!(plans.len(), 1);
    assert_eq!(plans[0].path_id, 1);
    let mut buffer = [0_u8; 1500];
    let (length, _source) = path_a_remote.recv_from(&mut buffer).unwrap();
    let frame = Frame::decode(&buffer[..length]).unwrap();
    assert_eq!(frame.header.kind, FrameKind::Data);
    assert_eq!(frame.header.service_id, 8);
    assert_eq!(frame.payload, payload);
    assert!(path_b_remote.recv_from(&mut buffer).is_err());
    assert_eq!(dataplane.metrics_snapshot().control_metadata.sent.frames, 1);
}
