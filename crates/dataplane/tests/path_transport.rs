use std::net::{SocketAddr, UdpSocket};
use std::sync::atomic::{AtomicU16, Ordering};
use std::thread;
use std::time::Duration;

use gatherlink_crypto::envelope::{
    decrypt_packet_without_replay, ENCRYPTED_DATA_HEADER_LEN, PACKET_TYPE_ENCRYPTED_DATA_V1,
};
use gatherlink_dataplane::engine::{CoreDataplane, PacketBatchSummary, RemoteDeliverOutcome};
use gatherlink_dataplane::relay::{RelayHopForwarder, RelaySessionConfig, RelaySessionExecutor};
use gatherlink_dataplane::runtime_config::{
    CorePathConfig, CoreRuntimeConfig, PathSchedulerPrimitives, PathSchedulerState, SchedulerConfig,
    TransportSecurityConfig, TransportSecuritySessionConfig,
};
use gatherlink_dataplane::udp_service::{
    ServicePathPolicy, ServiceReturnMode, ServiceSchedulerConfig, UdpServiceConfig,
};
use gatherlink_protocol::control::{ControlMessage, ControlPayload, PathMetadata};
use gatherlink_protocol::frame::{Frame, FrameKind};
use gatherlink_protocol::ids::SERVICE_ID_REMOTE_STATUS;

static NEXT_TEST_PORT: AtomicU16 = AtomicU16::new(0);

fn recv_with_retry(socket: &UdpSocket, buffer: &mut [u8]) -> (usize, SocketAddr) {
    for _ in 0..20 {
        match socket.recv_from(buffer) {
            Ok(received) => return received,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(10)),
            Err(error) if error.kind() == std::io::ErrorKind::TimedOut => thread::sleep(Duration::from_millis(10)),
            Err(error) => panic!("failed to receive UDP datagram: {error}"),
        }
    }
    socket.recv_from(buffer).unwrap()
}

fn receive_paths_with_retry(dataplane: &mut CoreDataplane, max_frames: usize) -> Vec<RemoteDeliverOutcome> {
    for _ in 0..20 {
        let delivered = dataplane.receive_available_from_paths(max_frames).unwrap();
        if !delivered.is_empty() {
            return delivered;
        }
        thread::sleep(Duration::from_millis(10));
    }
    dataplane.receive_available_from_paths(max_frames).unwrap()
}

fn receive_paths_summary_with_retry(dataplane: &mut CoreDataplane, max_frames: usize) -> PacketBatchSummary {
    for _ in 0..20 {
        let summary = dataplane.receive_available_from_paths_summary(max_frames).unwrap();
        if summary.packets > 0 {
            return summary;
        }
        thread::sleep(Duration::from_millis(10));
    }
    dataplane.receive_available_from_paths_summary(max_frames).unwrap()
}

fn wait_for_duplicate_counters(
    dataplane: &mut CoreDataplane,
    service_name: &str,
    expected_duplicates: u64,
    unexpected_duplicates: u64,
) {
    for _ in 0..20 {
        let delivered = dataplane.receive_available_from_paths(8).unwrap();
        assert!(
            delivered.is_empty(),
            "duplicate datagram should not be emitted to the application"
        );
        let snapshot = dataplane.metrics_snapshot();
        let Some(service) = snapshot.services.get(service_name) else {
            thread::sleep(Duration::from_millis(10));
            continue;
        };
        if service.expected_duplicate_packets == expected_duplicates
            && service.unexpected_duplicate_packets == unexpected_duplicates
        {
            return;
        }
        thread::sleep(Duration::from_millis(10));
    }
    let snapshot = dataplane.metrics_snapshot();
    let service = snapshot.services.get(service_name).unwrap();
    assert_eq!(service.expected_duplicate_packets, expected_duplicates);
    assert_eq!(service.unexpected_duplicate_packets, unexpected_duplicates);
}

fn reserve_loopback_addr() -> SocketAddr {
    for _ in 0..20_000 {
        let port = NEXT_TEST_PORT.fetch_add(1, Ordering::Relaxed);
        let port = 20_000 + (port % 10_000);
        let addr: SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();
        if UdpSocket::bind(addr).is_ok() {
            return addr;
        }
    }
    UdpSocket::bind("127.0.0.1:0").unwrap().local_addr().unwrap()
}

fn service(name: &str, target: SocketAddr) -> UdpServiceConfig {
    UdpServiceConfig::new(name, Some("127.0.0.1:0".parse().unwrap()), target).unwrap()
}

fn service_with_listen_return_mode(
    name: &str,
    listen: SocketAddr,
    target: SocketAddr,
    return_mode: ServiceReturnMode,
) -> UdpServiceConfig {
    UdpServiceConfig::new_with_return_mode(name, Some(listen), target, 100, return_mode).unwrap()
}

fn path(path_id: u16, bind: SocketAddr, remote: SocketAddr) -> CorePathConfig {
    CorePathConfig::new(path_id, 1200, false)
        .unwrap()
        .with_transport(bind, remote)
}

fn path_bind_only(path_id: u16, bind: SocketAddr) -> CorePathConfig {
    CorePathConfig::new(path_id, 1200, false)
        .unwrap()
        .with_transport_bind(bind)
}

fn path_bind_only_with_reorder_hold(path_id: u16, bind: SocketAddr, reorder_hold_us: u32) -> CorePathConfig {
    CorePathConfig::new_with_scheduler_primitives(
        path_id,
        1200,
        true,
        PathSchedulerState::Active,
        1,
        PathSchedulerPrimitives::new(None, None, None, 0, reorder_hold_us, 0, 0, 0, 0, 0, 0),
    )
    .unwrap()
    .with_transport_bind(bind)
}

fn path_with_mtu(path_id: u16, max_payload: usize, bind: SocketAddr, remote: SocketAddr) -> CorePathConfig {
    CorePathConfig::new(path_id, max_payload, false)
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
            local_receiver_index: receiver_index,
            remote_receiver_index: receiver_index,
            send_key,
            receive_key,
        },
    )
    .unwrap()
}

fn secure_multi_config(
    services: Vec<UdpServiceConfig>,
    paths: Vec<CorePathConfig>,
    sessions: Vec<TransportSecuritySessionConfig>,
) -> CoreRuntimeConfig {
    CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        services,
        paths,
        SchedulerConfig::default(),
        TransportSecurityConfig::StaticSessions(sessions),
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
    let (length, _source) = recv_with_retry(&path_remote, &mut buffer);
    assert_ne!(&buffer[..length], b"wireguard-like-payload");
    let frame = Frame::decode(&buffer[..length]).unwrap();
    assert_eq!(frame.kind, FrameKind::Data);
    assert_eq!(frame.service_id, 256);
    assert_eq!(frame.path_id, 42);
    assert_eq!(frame.payload, b"wireguard-like-payload");
    assert_eq!(outcome.path_id, 42);
    assert_eq!(outcome.payload_len, b"wireguard-like-payload".len());
}

#[test]
fn summary_path_transport_sends_multiple_carrier_frames() {
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
    let payloads = (0..4)
        .map(|index| {
            let mut payload = vec![b'x'; 700];
            payload[..9].copy_from_slice(format!("frame-{index:03}").as_bytes());
            payload
        })
        .collect::<Vec<_>>();

    for payload in &payloads {
        app_sender.send_to(payload, service_addr).unwrap();
    }
    let summary = dataplane
        .forward_available_for_service_nonblocking_summary("udp-main", payloads.len())
        .unwrap();
    assert_eq!(summary.packets, payloads.len());

    for expected in &payloads {
        let mut buffer = [0_u8; 1500];
        let (length, _source) = recv_with_retry(&path_remote, &mut buffer);
        let frame = Frame::decode(&buffer[..length]).unwrap();
        assert_eq!(frame.kind, FrameKind::Data);
        assert_eq!(frame.path_id, 42);
        assert_eq!(frame.payload, *expected);
    }
}

#[test]
fn budget_summary_stops_after_byte_budget_boundary() {
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
    let payloads = (0..4)
        .map(|index| {
            let mut payload = vec![b'x'; 700];
            payload[..9].copy_from_slice(format!("frame-{index:03}").as_bytes());
            payload
        })
        .collect::<Vec<_>>();

    for payload in &payloads {
        app_sender.send_to(payload, service_addr).unwrap();
    }
    let summary = dataplane
        .forward_available_for_service_budget_summary("udp-main", payloads.len(), 1_000)
        .unwrap();
    assert_eq!(summary.packets, 2);
    assert_eq!(summary.bytes, 1_400);

    for expected in &payloads[..2] {
        let mut buffer = [0_u8; 1500];
        let (length, _source) = recv_with_retry(&path_remote, &mut buffer);
        let frame = Frame::decode(&buffer[..length]).unwrap();
        assert_eq!(frame.kind, FrameKind::Data);
        assert_eq!(frame.path_id, 42);
        assert_eq!(frame.payload, *expected);
    }

    let summary = dataplane
        .forward_available_for_service_budget_summary("udp-main", payloads.len(), 1_000)
        .unwrap();
    assert_eq!(summary.packets, 2);
    assert_eq!(summary.bytes, 1_400);
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
    let (length, _source) = recv_with_retry(&path_remote, &mut buffer);
    let packet = &buffer[..length];
    assert_eq!(packet[0], PACKET_TYPE_ENCRYPTED_DATA_V1);
    let decrypted = decrypt_packet_without_replay(&send_key, packet).unwrap();
    assert_eq!(decrypted.receiver_index, 1234);
    assert!(Frame::decode(&decrypted.plaintext).is_err());
    let frame = Frame::decode_v2(&decrypted.plaintext).unwrap();
    assert_eq!(frame.service_id, 256);
    assert_eq!(frame.path_id, 42);
    assert_eq!(frame.payload, b"secret-payload");
}

#[test]
fn relay_wrapped_path_transport_sends_outer_hop_envelope() {
    let path_bind = reserve_loopback_addr();
    let relay_socket = UdpSocket::bind("127.0.0.1:0").unwrap();
    relay_socket.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let endpoint_send_key = [0x33_u8; 32];
    let relay_send_key = [0x55_u8; 32];
    let config = secure_config(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![path(42, path_bind, relay_socket.local_addr().unwrap()).with_relay_send(99, relay_send_key)],
        1234,
        endpoint_send_key,
        [0x44_u8; 32],
    );
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    app_sender.send_to(b"relayed-secret-payload", service_addr).unwrap();
    dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 1500];
    let (length, _source) = recv_with_retry(&relay_socket, &mut buffer);
    let hop_packet = &buffer[..length];
    let hop_decrypted = decrypt_packet_without_replay(&relay_send_key, hop_packet).unwrap();
    let endpoint_decrypted = decrypt_packet_without_replay(&endpoint_send_key, &hop_decrypted.plaintext).unwrap();
    let frame = Frame::decode_v2(&endpoint_decrypted.plaintext).unwrap();

    assert_eq!(hop_decrypted.receiver_index, 99);
    assert_eq!(endpoint_decrypted.receiver_index, 1234);
    assert_eq!(frame.service_id, 256);
    assert_eq!(frame.payload, b"relayed-secret-payload");
}

#[test]
fn relay_chain_carries_endpoint_packet_through_untrusted_middle_peer() {
    let b_path_bind = reserve_loopback_addr();
    let c_relay_listen = reserve_loopback_addr();
    let a_path_bind = reserve_loopback_addr();
    let a_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    a_target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();

    let endpoint_b_to_a = [0x21_u8; 32];
    let endpoint_a_to_b = [0x22_u8; 32];
    let hop_b_to_c = [0x31_u8; 32];

    let b_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("wireguard-main", "127.0.0.1:51821".parse().unwrap())],
        vec![path(7, b_path_bind, c_relay_listen).with_relay_send(701, hop_b_to_c)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 101,
            remote_receiver_index: 201,
            send_key: endpoint_b_to_a,
            receive_key: endpoint_a_to_b,
        },
    )
    .unwrap();
    let a_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("wireguard-main", a_target.local_addr().unwrap())],
        vec![path_bind_only(7, a_path_bind)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 201,
            remote_receiver_index: 101,
            send_key: endpoint_a_to_b,
            receive_key: endpoint_b_to_a,
        },
    )
    .unwrap();
    let mut b_core = CoreDataplane::bind(b_config).unwrap();
    let mut a_core = CoreDataplane::bind(a_config).unwrap();
    let mut c_relay = RelayHopForwarder::bind(
        c_relay_listen,
        a_path_bind,
        RelaySessionExecutor::new_with_hop_keys(
            RelaySessionConfig {
                relay_receiver_index: 701,
                expires_at_unix_us: 2_000,
                max_packet_size: Some(1_200),
                max_packets: None,
                max_bytes: None,
            },
            801,
            [0_u8; 32],
            hop_b_to_c,
        ),
    )
    .unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = b_core.service("wireguard-main").unwrap().local_addr().unwrap();

    app_sender
        .send_to(b"endpoint-payload-through-relay", service_addr)
        .unwrap();
    b_core.forward_one_for_service("wireguard-main").unwrap();
    assert!(matches!(
        c_relay.try_forward_one(1_000).unwrap(),
        gatherlink_dataplane::relay::RelayForwardOutcome::Forwarded { .. }
    ));
    let delivered = receive_paths_with_retry(&mut a_core, 8);
    let mut buffer = [0_u8; 128];
    let (length, _source) = recv_with_retry(&a_target, &mut buffer);

    assert_eq!(&buffer[..length], b"endpoint-payload-through-relay");
    assert_eq!(delivered.len(), 1);
    assert_eq!(delivered[0].service, "wireguard-main");
    assert_eq!(delivered[0].path_id, 7);
    assert_eq!(c_relay.counters().forwarded_packets, 1);
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
    let delivered = receive_paths_with_retry(&mut server, 8);

    let mut buffer = [0_u8; 128];
    let (length, _source) = recv_with_retry(&remote_target, &mut buffer);
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
fn path_transport_receive_drains_multiple_frames_from_one_hot_socket() {
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

    for index in 0..4 {
        app_sender
            .send_to(format!("hot-frame-{index}").as_bytes(), client_service_addr)
            .unwrap();
        client.forward_one_for_service("udp-main").unwrap();
    }

    thread::sleep(Duration::from_millis(20));
    let delivered = server.receive_available_from_paths(4).unwrap();

    assert_eq!(
        delivered.len(),
        4,
        "a hot path socket should drain to the caller budget, not one frame per runner loop"
    );
    for index in 0..4 {
        let mut buffer = [0_u8; 128];
        let (length, _source) = recv_with_retry(&remote_target, &mut buffer);
        assert_eq!(&buffer[..length], format!("hot-frame-{index}").as_bytes());
    }
}

#[test]
fn summary_path_receive_emits_batched_payloads_to_udp_target() {
    let client_path_addr = reserve_loopback_addr();
    let server_path_addr = reserve_loopback_addr();
    let remote_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    remote_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();

    let client_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![path_with_mtu(7, 9000, client_path_addr, server_path_addr)],
    )
    .unwrap();
    let server_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![path_with_mtu(7, 9000, server_path_addr, client_path_addr)],
    )
    .unwrap();
    let mut client = CoreDataplane::bind(client_config).unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let client_service_addr = client.service("udp-main").unwrap().local_addr().unwrap();
    let payloads = (0..6)
        .map(|index| {
            let mut payload = vec![b'x'; 1150];
            payload[..7].copy_from_slice(format!("pkt-{index:03}").as_bytes());
            payload
        })
        .collect::<Vec<_>>();

    for payload in &payloads {
        app_sender.send_to(payload, client_service_addr).unwrap();
    }
    let forwarded = client
        .forward_available_for_service_nonblocking_summary("udp-main", payloads.len())
        .unwrap();
    assert_eq!(forwarded.packets, payloads.len());
    assert_eq!(forwarded.bytes, payloads.iter().map(Vec::len).sum::<usize>());

    let received = receive_paths_summary_with_retry(&mut server, 8);
    assert_eq!(received.packets, payloads.len());
    assert_eq!(received.bytes, forwarded.bytes);

    let mut received_payloads = Vec::new();
    for _ in 0..payloads.len() {
        let mut buffer = vec![0_u8; 1400];
        let (length, _source) = recv_with_retry(&remote_target, &mut buffer);
        received_payloads.push(buffer[..length].to_vec());
    }
    assert_eq!(received_payloads, payloads);
    assert_eq!(
        server.metrics_snapshot().services.get("udp-main").unwrap().rx_packets,
        payloads.len() as u64
    );
}

#[test]
fn receiver_reorder_buffer_releases_oldest_when_hold_work_is_bounded() {
    let path_bind = reserve_loopback_addr();
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_nonblocking(true).unwrap();
    let config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", target.local_addr().unwrap())],
        vec![path_bind_only_with_reorder_hold(42, path_bind, 5_000_000)],
    )
    .unwrap();
    let mut receiver = CoreDataplane::bind(config).unwrap();
    let path_sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    let first = Frame::data(256, 42, 1, b"first".to_vec()).unwrap().encode().unwrap();
    path_sender.send_to(&first, path_bind).unwrap();
    let first_summary = receive_paths_summary_with_retry(&mut receiver, 1);
    assert_eq!(first_summary.packets, 1);

    let mut buffer = [0_u8; 64];
    let (length, _source) = recv_with_retry(&target, &mut buffer);
    assert_eq!(&buffer[..length], b"first");

    let mut released_packets = 0_u64;
    let mut sent_in_chunk = 0_usize;
    for sequence in 9000..17_300 {
        let frame = Frame::data(256, 42, sequence, b"bounded".to_vec())
            .unwrap()
            .encode()
            .unwrap();
        path_sender.send_to(&frame, path_bind).unwrap();
        sent_in_chunk += 1;
        if sent_in_chunk == 256 {
            released_packets += receive_paths_summary_with_retry(&mut receiver, sent_in_chunk).packets as u64;
            sent_in_chunk = 0;
        }
    }
    if sent_in_chunk > 0 {
        released_packets += receive_paths_summary_with_retry(&mut receiver, sent_in_chunk).packets as u64;
    }

    assert!(
        released_packets > 0,
        "bounded reorder work should release old payloads instead of waiting for every missing sequence"
    );
}

#[test]
fn summary_path_receive_rotates_across_hot_path_sockets() {
    let client_path_a = reserve_loopback_addr();
    let server_path_a = reserve_loopback_addr();
    let client_path_b = reserve_loopback_addr();
    let server_path_b = reserve_loopback_addr();
    let client_path_c = reserve_loopback_addr();
    let server_path_c = reserve_loopback_addr();
    let remote_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    remote_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();

    let client_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![
            path(1, client_path_a, server_path_a),
            path(2, client_path_b, server_path_b),
            path(3, client_path_c, server_path_c),
        ],
    )
    .unwrap();
    let server_config = CoreRuntimeConfig::new_with_paths(
        vec![service("udp-main", remote_target.local_addr().unwrap())],
        vec![
            path(1, server_path_a, client_path_a),
            path(2, server_path_b, client_path_b),
            path(3, server_path_c, client_path_c),
        ],
    )
    .unwrap();
    let mut client = CoreDataplane::bind(client_config).unwrap();
    let mut server = CoreDataplane::bind(server_config).unwrap();
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let client_service_addr = client.service("udp-main").unwrap().local_addr().unwrap();

    for index in 0..6 {
        let mut payload = vec![b'x'; 700];
        payload[..11].copy_from_slice(format!("fair-{index:06}").as_bytes());
        app_sender.send_to(payload.as_slice(), client_service_addr).unwrap();
        client.forward_one_for_service("udp-main").unwrap();
    }

    thread::sleep(Duration::from_millis(20));
    let received = receive_paths_summary_with_retry(&mut server, 3);
    assert_eq!(received.packets, 3);
    let snapshot = server.metrics_snapshot();
    assert_eq!(snapshot.paths.values().filter(|path| path.rx_packets == 1).count(), 3);
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

    let delivered = receive_paths_with_retry(&mut server, 8);
    let mut buffer = [0_u8; 128];
    let (length, _source) = recv_with_retry(&remote_target, &mut buffer);
    assert_eq!(&buffer[..length], b"encrypted-fixed-target");
    assert_eq!(delivered.len(), 1);
    assert_eq!(delivered[0].path_id, 7);

    // A random plaintext frame sent to an encrypted transport is silently ignored.
    let plaintext = Frame::data(256, 7, 123, b"not-authenticated".to_vec())
        .unwrap()
        .encode()
        .unwrap();
    sniffer.send_to(&plaintext, server_path_addr).unwrap();
    assert!(server.receive_available_from_paths(8).unwrap().is_empty());
    let snapshot = server.metrics_snapshot();
    assert_eq!(snapshot.security_drops.packets, 1);
    assert_eq!(snapshot.security_drops.bytes, plaintext.len() as u64);
    assert_eq!(snapshot.paths[&7].rx_packets, 1);
    assert_eq!(snapshot.paths[&7].security_drop_packets, 1);
    assert_eq!(snapshot.paths[&7].security_drop_bytes, plaintext.len() as u64);

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
fn shared_sink_path_port_accepts_two_authenticated_source_sessions() {
    let source_a_path_addr = reserve_loopback_addr();
    let source_c_path_addr = reserve_loopback_addr();
    let sink_path_addr = reserve_loopback_addr();
    let sink_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    sink_target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let a_to_sink = [0x10_u8; 32];
    let sink_to_a = [0x11_u8; 32];
    let c_to_sink = [0x20_u8; 32];
    let sink_to_c = [0x21_u8; 32];

    let source_a_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("udp-main", sink_target.local_addr().unwrap())],
        vec![path(7, source_a_path_addr, sink_path_addr)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 101,
            remote_receiver_index: 201,
            send_key: a_to_sink,
            receive_key: sink_to_a,
        },
    )
    .unwrap();
    let source_c_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("udp-main", sink_target.local_addr().unwrap())],
        vec![path(7, source_c_path_addr, sink_path_addr)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 102,
            remote_receiver_index: 202,
            send_key: c_to_sink,
            receive_key: sink_to_c,
        },
    )
    .unwrap();
    let sink_config = secure_multi_config(
        vec![service("udp-main", sink_target.local_addr().unwrap())],
        vec![path_bind_only(7, sink_path_addr)],
        vec![
            TransportSecuritySessionConfig::new(201, 101, sink_to_a, a_to_sink, vec![256]),
            TransportSecuritySessionConfig::new(202, 102, sink_to_c, c_to_sink, vec![256]),
        ],
    );
    let mut source_a = CoreDataplane::bind(source_a_config).unwrap();
    let mut source_c = CoreDataplane::bind(source_c_config).unwrap();
    let mut sink = CoreDataplane::bind(sink_config.clone()).unwrap();
    let app_a = UdpSocket::bind("127.0.0.1:0").unwrap();
    let app_c = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_a = source_a.service("udp-main").unwrap().local_addr().unwrap();
    let service_c = source_c.service("udp-main").unwrap().local_addr().unwrap();

    app_a.send_to(b"hello-from-a", service_a).unwrap();
    app_c.send_to(b"hello-from-c", service_c).unwrap();
    source_a.forward_one_for_service("udp-main").unwrap();
    source_c.forward_one_for_service("udp-main").unwrap();

    let mut delivered = Vec::new();
    for _ in 0..20 {
        delivered.extend(sink.receive_available_from_paths(8).unwrap());
        if delivered.len() >= 2 {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }

    let mut received_payloads = Vec::new();
    for _ in 0..2 {
        let mut buffer = [0_u8; 128];
        let (length, _source) = recv_with_retry(&sink_target, &mut buffer);
        received_payloads.push(buffer[..length].to_vec());
    }
    received_payloads.sort();
    assert_eq!(
        received_payloads,
        vec![b"hello-from-a".to_vec(), b"hello-from-c".to_vec()]
    );
    assert_eq!(delivered.len(), 2);
    assert!(delivered
        .iter()
        .all(|outcome| outcome.target == sink_target.local_addr().unwrap()));
    let service_metrics = sink.metrics_snapshot().services.get("udp-main").unwrap().clone();
    assert_eq!(service_metrics.rx_packets, 2);
}

#[test]
fn shared_sink_reserved_response_uses_authenticated_peer_scope() {
    let source_a_path_addr = reserve_loopback_addr();
    let source_c_path_addr = reserve_loopback_addr();
    let sink_path_addr = reserve_loopback_addr();
    let sink_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let a_to_sink = [0x50_u8; 32];
    let sink_to_a = [0x51_u8; 32];
    let c_to_sink = [0x60_u8; 32];
    let sink_to_c = [0x61_u8; 32];

    let source_a_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("udp-main", sink_target.local_addr().unwrap())],
        vec![path(7, source_a_path_addr, sink_path_addr)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 101,
            remote_receiver_index: 201,
            send_key: a_to_sink,
            receive_key: sink_to_a,
        },
    )
    .unwrap();
    let source_c_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("udp-main", sink_target.local_addr().unwrap())],
        vec![path(7, source_c_path_addr, sink_path_addr)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 102,
            remote_receiver_index: 202,
            send_key: c_to_sink,
            receive_key: sink_to_c,
        },
    )
    .unwrap();
    let sink_config = secure_multi_config(
        vec![service("udp-main", sink_target.local_addr().unwrap())],
        vec![path_bind_only(7, sink_path_addr)],
        vec![
            TransportSecuritySessionConfig::new(201, 101, sink_to_a, a_to_sink, vec![256]),
            TransportSecuritySessionConfig::new(202, 102, sink_to_c, c_to_sink, vec![256]),
        ],
    );
    let mut source_a = CoreDataplane::bind(source_a_config).unwrap();
    let mut source_c = CoreDataplane::bind(source_c_config).unwrap();
    let mut sink = CoreDataplane::bind(sink_config).unwrap();

    source_a
        .transmit_service_payload(SERVICE_ID_REMOTE_STATUS, b"status-request-a".to_vec())
        .unwrap();
    source_c
        .transmit_service_payload(SERVICE_ID_REMOTE_STATUS, b"status-request-c".to_vec())
        .unwrap();
    let mut events = Vec::new();
    for _ in 0..20 {
        sink.receive_available_from_paths(8).unwrap();
        events.extend(sink.drain_reserved_service_events());
        if events.len() >= 2 {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert!(events.iter().any(|event| event.peer_scope == Some(201)));
    assert!(events.iter().any(|event| event.peer_scope == Some(202)));

    sink.transmit_service_payload_to_peer(SERVICE_ID_REMOTE_STATUS, b"response-a".to_vec(), 201)
        .unwrap();
    let mut a_events = Vec::new();
    let mut c_events = Vec::new();
    for _ in 0..20 {
        source_a.receive_available_from_paths(8).unwrap();
        source_c.receive_available_from_paths(8).unwrap();
        a_events.extend(source_a.drain_reserved_service_events());
        c_events.extend(source_c.drain_reserved_service_events());
        if !a_events.is_empty() {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(a_events[0].payload, b"response-a");
    assert!(c_events.is_empty());
}

#[test]
fn shared_sink_peer_scoped_sources_route_replies_to_the_right_authenticated_source() {
    let source_a_path_addr = reserve_loopback_addr();
    let source_c_path_addr = reserve_loopback_addr();
    let sink_path_addr = reserve_loopback_addr();
    let app_server = UdpSocket::bind("127.0.0.1:0").unwrap();
    app_server.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let source_a_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let source_c_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    source_a_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    source_c_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let a_to_sink = [0x30_u8; 32];
    let sink_to_a = [0x31_u8; 32];
    let c_to_sink = [0x40_u8; 32];
    let sink_to_c = [0x41_u8; 32];

    let source_a_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("udp-main", source_a_target.local_addr().unwrap())],
        vec![path(7, source_a_path_addr, sink_path_addr)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 101,
            remote_receiver_index: 201,
            send_key: a_to_sink,
            receive_key: sink_to_a,
        },
    )
    .unwrap();
    let source_c_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("udp-main", source_c_target.local_addr().unwrap())],
        vec![path(7, source_c_path_addr, sink_path_addr)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 102,
            remote_receiver_index: 202,
            send_key: c_to_sink,
            receive_key: sink_to_c,
        },
    )
    .unwrap();
    let sink_config = secure_multi_config(
        vec![service_with_listen_return_mode(
            "udp-main",
            "0.0.0.0:0".parse().unwrap(),
            app_server.local_addr().unwrap(),
            ServiceReturnMode::PeerScopedSource,
        )],
        vec![path_bind_only(7, sink_path_addr)],
        vec![
            TransportSecuritySessionConfig::new(201, 101, sink_to_a, a_to_sink, vec![256]),
            TransportSecuritySessionConfig::new(202, 102, sink_to_c, c_to_sink, vec![256]),
        ],
    );
    let mut source_a = CoreDataplane::bind(source_a_config).unwrap();
    let mut source_c = CoreDataplane::bind(source_c_config).unwrap();
    let mut sink = CoreDataplane::bind(sink_config.clone()).unwrap();
    let app_a = UdpSocket::bind("127.0.0.1:0").unwrap();
    let app_c = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_a = source_a.service("udp-main").unwrap().local_addr().unwrap();
    let service_c = source_c.service("udp-main").unwrap().local_addr().unwrap();

    app_a.send_to(b"from-a", service_a).unwrap();
    app_c.send_to(b"from-c", service_c).unwrap();
    source_a.forward_one_for_service("udp-main").unwrap();
    source_c.forward_one_for_service("udp-main").unwrap();
    for _ in 0..20 {
        sink.receive_available_from_paths(8).unwrap();
        thread::sleep(Duration::from_millis(10));
    }

    let mut buffer = [0_u8; 128];
    let (a_len, a_reply_addr) = recv_with_retry(&app_server, &mut buffer);
    assert_eq!(&buffer[..a_len], b"from-a");
    let (c_len, c_reply_addr) = recv_with_retry(&app_server, &mut buffer);
    assert_eq!(&buffer[..c_len], b"from-c");
    assert_ne!(a_reply_addr, c_reply_addr);
    assert_eq!(a_reply_addr.ip(), app_server.local_addr().unwrap().ip());
    assert_eq!(c_reply_addr.ip(), app_server.local_addr().unwrap().ip());
    let sink_snapshot = sink.metrics_snapshot();
    let path_snapshot = sink_snapshot.paths.get(&7).unwrap();
    assert_eq!(path_snapshot.rx_packets, 2);
    assert_eq!(path_snapshot.packets_needing_reorder, 0);
    assert_eq!(path_snapshot.reordered_packets, 0);
    assert_eq!(sink_snapshot.services.get("udp-main").unwrap().rx_packets, 2);

    // A harmless config refresh must not invalidate the WireGuard-like app
    // endpoints that the sink-side server just learned. The peer source sockets
    // and authenticated carrier remotes are execution state, not lab scaffolding.
    sink.reapply_config(sink_config.clone()).unwrap();

    app_server.send_to(b"reply-a", a_reply_addr).unwrap();
    app_server.send_to(b"reply-c", c_reply_addr).unwrap();
    for _ in 0..20 {
        sink.forward_available_for_service_nonblocking("udp-main", 8).unwrap();
        source_a.receive_available_from_paths(8).unwrap();
        source_c.receive_available_from_paths(8).unwrap();
        thread::sleep(Duration::from_millis(10));
    }

    let (length, _source) = recv_with_retry(&source_a_target, &mut buffer);
    assert_eq!(&buffer[..length], b"reply-a");
    let (length, _source) = recv_with_retry(&source_c_target, &mut buffer);
    assert_eq!(&buffer[..length], b"reply-c");

    // Shared-sink replies are scoped to the authenticated peer/session. Each
    // receiver sees its own monotonic sequence space; packets intentionally
    // sent to another peer must not look like local reorder pressure.
    let source_a_snapshot = source_a.metrics_snapshot();
    let source_a_path = source_a_snapshot.paths.get(&7).unwrap();
    assert_eq!(source_a_path.packets_needing_reorder, 0);
    assert_eq!(source_a_path.reordered_packets, 0);
    let source_c_snapshot = source_c.metrics_snapshot();
    let source_c_path = source_c_snapshot.paths.get(&7).unwrap();
    assert_eq!(source_c_path.packets_needing_reorder, 0);
    assert_eq!(source_c_path.reordered_packets, 0);
}

#[test]
fn peer_scoped_replies_honor_service_specific_path_policy() {
    let source_path_a = reserve_loopback_addr();
    let source_path_b = reserve_loopback_addr();
    let sink_path_a = reserve_loopback_addr();
    let sink_path_b = reserve_loopback_addr();
    let app_server = UdpSocket::bind("127.0.0.1:0").unwrap();
    app_server.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let source_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    source_target
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let source_to_sink = [0x50_u8; 32];
    let sink_to_source = [0x51_u8; 32];

    let source_config = CoreRuntimeConfig::new_with_paths_scheduler_and_security(
        vec![service("udp-main", source_target.local_addr().unwrap())],
        vec![path(1, source_path_a, sink_path_a), path(2, source_path_b, sink_path_b)],
        SchedulerConfig::default(),
        TransportSecurityConfig::Static {
            local_receiver_index: 301,
            remote_receiver_index: 401,
            send_key: source_to_sink,
            receive_key: sink_to_source,
        },
    )
    .unwrap();
    let sink_service = UdpServiceConfig::new_with_scheduler(
        0,
        "udp-main",
        Some("0.0.0.0:0".parse().unwrap()),
        app_server.local_addr().unwrap(),
        100,
        ServiceReturnMode::PeerScopedSource,
        ServiceSchedulerConfig::new_with_path_policy(1, 0, 0, 0, 0, ServicePathPolicy::WeightedRoundRobin)
            .with_allowed_path_ids(vec![2])
            .with_path_weights(vec![(2, 1)]),
    )
    .unwrap();
    let sink_config = secure_multi_config(
        vec![sink_service],
        vec![path_bind_only(1, sink_path_a), path_bind_only(2, sink_path_b)],
        vec![TransportSecuritySessionConfig::new(
            401,
            301,
            sink_to_source,
            source_to_sink,
            vec![256],
        )],
    );
    let mut source = CoreDataplane::bind(source_config).unwrap();
    let mut sink = CoreDataplane::bind(sink_config).unwrap();
    source.set_service_scheduler(256, ServiceSchedulerConfig::new(0, 0));
    let app_sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let source_service_addr = source.service("udp-main").unwrap().local_addr().unwrap();

    app_sender.send_to(b"learn-both-paths", source_service_addr).unwrap();
    source.forward_one_for_service("udp-main").unwrap();
    let delivered = receive_paths_with_retry(&mut sink, 8);
    assert_eq!(delivered.len(), 1);

    let mut buffer = [0_u8; 128];
    let (length, reply_addr) = recv_with_retry(&app_server, &mut buffer);
    assert_eq!(&buffer[..length], b"learn-both-paths");
    app_server.send_to(b"reply-on-path-b", reply_addr).unwrap();
    let forwarded = {
        let mut forwarded = Vec::new();
        for _ in 0..20 {
            forwarded = sink.forward_available_for_service_nonblocking("udp-main", 8).unwrap();
            if !forwarded.is_empty() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        forwarded.into_iter().next().unwrap()
    };
    assert_eq!(forwarded.path_id, 2);
    let replies = receive_paths_with_retry(&mut source, 8);
    assert_eq!(replies.len(), 1);

    let sink_snapshot = sink.metrics_snapshot();
    assert_eq!(sink_snapshot.paths.get(&1).unwrap().tx_packets, 0);
    assert_eq!(sink_snapshot.paths.get(&2).unwrap().tx_packets, 1);
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
    let delivered = receive_paths_with_retry(&mut server, 8);

    let mut buffer = [0_u8; 128];
    let (length, _source) = recv_with_retry(&remote_target, &mut buffer);
    assert_eq!(&buffer[..length], b"dedupe-me");
    assert!(remote_target.recv_from(&mut buffer).is_err());
    assert_eq!(delivered.len(), 1);
    wait_for_duplicate_counters(&mut server, "udp-main", 1, 0);
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
    let encoded = Frame::data(256, 1, 99, b"surprise-duplicate".to_vec())
        .unwrap()
        .encode()
        .unwrap();

    sender.send_to(&encoded, server_path).unwrap();
    let delivered = receive_paths_with_retry(&mut server, 8);
    assert_eq!(delivered.len(), 1);
    sender.send_to(&encoded, server_path).unwrap();
    wait_for_duplicate_counters(&mut server, "udp-main", 0, 1);

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
    let delivered = receive_paths_with_retry(&mut server, 8);
    assert!(delivered.is_empty());
    let events = drain_reserved_events_with_retry(&mut server, 2);
    assert_eq!(events.len(), 2);
    assert!(events.iter().all(|event| event.payload == b"control-copy"));
    assert_eq!(
        events.iter().map(|event| event.sequence).collect::<Vec<_>>(),
        vec![1, 1]
    );
}

fn drain_reserved_events_with_retry(
    dataplane: &mut CoreDataplane,
    expected_events: usize,
) -> Vec<gatherlink_dataplane::engine::ReservedServiceEvent> {
    for _ in 0..20 {
        let events = dataplane.drain_reserved_service_events();
        if events.len() >= expected_events {
            return events;
        }
        thread::sleep(Duration::from_millis(10));
    }
    dataplane.drain_reserved_service_events()
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
        assert_eq!(frame.kind, FrameKind::Data);
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
        assert_eq!(frame.service_id, 9);
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
    assert_eq!(frame.kind, FrameKind::Data);
    assert_eq!(frame.service_id, 8);
    assert_eq!(frame.payload, payload);
    assert!(path_b_remote.recv_from(&mut buffer).is_err());
    assert_eq!(dataplane.metrics_snapshot().control_metadata.sent.frames, 1);
}

#[test]
fn python_composed_service_payload_can_be_pinned_to_one_path_transport() {
    let path_a_bind = reserve_loopback_addr();
    let path_a_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    let path_b_bind = reserve_loopback_addr();
    let path_b_remote = UdpSocket::bind("127.0.0.1:0").unwrap();
    path_a_remote
        .set_read_timeout(Some(Duration::from_millis(100)))
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
    let payload = br#"{"type":"path_clock_probe"}"#.to_vec();

    let plans = dataplane
        .transmit_service_payload_on_path(1, 2, payload.clone())
        .unwrap();

    assert_eq!(plans.len(), 1);
    assert_eq!(plans[0].path_id, 2);
    assert!(path_a_remote.recv_from(&mut [0_u8; 1500]).is_err());
    let mut buffer = [0_u8; 1500];
    let (length, _source) = path_b_remote.recv_from(&mut buffer).unwrap();
    let frame = Frame::decode(&buffer[..length]).unwrap();
    assert_eq!(frame.kind, FrameKind::Data);
    assert_eq!(frame.service_id, 1);
    assert_eq!(frame.path_id, 2);
    assert_eq!(frame.payload, payload);
}
