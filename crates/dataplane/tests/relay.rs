use std::net::UdpSocket;
use std::time::Duration;

use gatherlink_crypto::envelope::encrypt_frame_with_counter;
use gatherlink_dataplane::relay::{
    RelayDropReason, RelayForwardOutcome, RelayHopExitForwarder, RelayHopForwarder, RelayPacketDecision,
    RelaySessionConfig, RelaySessionExecutor,
};

#[test]
fn relay_executor_accepts_compiled_session_and_counts_forwarding() {
    let mut executor = RelaySessionExecutor::new(RelaySessionConfig {
        relay_receiver_index: 77,
        expires_at_unix_us: 2_000,
        max_packet_size: Some(1_200),
        max_packets: Some(2),
        max_bytes: Some(2_500),
    });

    assert_eq!(
        executor.authorize_packet(77, 1_000, 1_000),
        RelayPacketDecision::Forward
    );
    assert_eq!(
        executor.authorize_packet(77, 1_100, 1_100),
        RelayPacketDecision::Forward
    );
    assert_eq!(
        executor.authorize_packet(77, 100, 1_200),
        RelayPacketDecision::Drop(RelayDropReason::LimitExceeded)
    );
    let counters = executor.counters();
    assert_eq!(counters.forwarded_packets, 2);
    assert_eq!(counters.forwarded_bytes, 2_100);
    assert_eq!(counters.dropped_packets, 1);
    assert_eq!(counters.emitted_packets, 0);
    assert_eq!(counters.emitted_bytes, 0);
}

#[test]
fn relay_executor_fails_closed_for_wrong_receiver_expiry_and_size() {
    let mut executor = RelaySessionExecutor::new(RelaySessionConfig {
        relay_receiver_index: 77,
        expires_at_unix_us: 2_000,
        max_packet_size: Some(1_200),
        max_packets: None,
        max_bytes: None,
    });

    assert_eq!(
        executor.authorize_packet(88, 100, 1_000),
        RelayPacketDecision::Drop(RelayDropReason::UnknownReceiverIndex)
    );
    assert_eq!(
        executor.authorize_packet(77, 100, 2_000),
        RelayPacketDecision::Drop(RelayDropReason::ExpiredSession)
    );
    assert_eq!(
        executor.authorize_packet(77, 1_201, 1_000),
        RelayPacketDecision::Drop(RelayDropReason::PacketTooLarge)
    );
    assert_eq!(executor.counters().dropped_packets, 3);
}

#[test]
fn relay_executor_authenticates_and_unwraps_one_outer_hop_layer() {
    let upstream_to_relay = [11_u8; 32];
    let mut executor = RelaySessionExecutor::new_with_hop_keys(
        RelaySessionConfig {
            relay_receiver_index: 77,
            expires_at_unix_us: 2_000,
            max_packet_size: Some(1_200),
            max_packets: None,
            max_bytes: None,
        },
        88,
        [22_u8; 32],
        upstream_to_relay,
    );
    let inner_packet = b"endpoint-encrypted-packet";
    let hop_packet = encrypt_frame_with_counter(77, &upstream_to_relay, 1, inner_packet).unwrap();

    let unwrapped = executor.unwrap_authenticated_hop_packet(&hop_packet, 1_000).unwrap();

    assert_eq!(unwrapped, inner_packet);
    assert_eq!(executor.counters().forwarded_packets, 1);
    assert_eq!(executor.counters().forwarded_bytes, inner_packet.len() as u64);
}

#[test]
fn relay_executor_silently_drops_invalid_hop_packets() {
    let mut executor = RelaySessionExecutor::new_with_hop_keys(
        RelaySessionConfig {
            relay_receiver_index: 77,
            expires_at_unix_us: 2_000,
            max_packet_size: Some(1_200),
            max_packets: None,
            max_bytes: None,
        },
        88,
        [22_u8; 32],
        [11_u8; 32],
    );

    assert_eq!(
        executor.unwrap_authenticated_hop_packet(b"not-authentic", 1_000),
        Err(RelayDropReason::HopAuthFailed)
    );
    assert_eq!(executor.counters().dropped_packets, 1);
}

#[test]
fn relay_forwarder_unwraps_and_sends_opaque_inner_packet_to_compiled_next_hop() {
    let upstream_to_relay = [11_u8; 32];
    let next_hop = UdpSocket::bind("127.0.0.1:0").unwrap();
    next_hop.set_read_timeout(Some(Duration::from_millis(250))).unwrap();
    let mut forwarder = RelayHopForwarder::bind(
        "127.0.0.1:0".parse().unwrap(),
        next_hop.local_addr().unwrap(),
        RelaySessionExecutor::new_with_hop_keys(
            RelaySessionConfig {
                relay_receiver_index: 77,
                expires_at_unix_us: 2_000,
                max_packet_size: Some(1_200),
                max_packets: None,
                max_bytes: None,
            },
            88,
            [22_u8; 32],
            upstream_to_relay,
        ),
    )
    .unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let inner_packet = b"endpoint-encrypted-packet";
    let hop_packet = encrypt_frame_with_counter(77, &upstream_to_relay, 1, inner_packet).unwrap();
    sender.send_to(&hop_packet, forwarder.local_addr().unwrap()).unwrap();

    let outcome = forwarder.try_forward_one(1_000).unwrap();
    let mut buffer = [0_u8; 2048];
    let (length, _source) = next_hop.recv_from(&mut buffer).unwrap();

    assert!(matches!(outcome, RelayForwardOutcome::Forwarded { .. }));
    assert_eq!(&buffer[..length], inner_packet);
    assert_eq!(forwarder.counters().forwarded_packets, 1);
    assert_eq!(forwarder.counters().emitted_packets, 1);
    assert_eq!(forwarder.counters().emitted_bytes, length as u64);
}

#[test]
fn relay_forwarder_drains_ready_packets_in_one_batch() {
    let upstream_to_relay = [11_u8; 32];
    let next_hop = UdpSocket::bind("127.0.0.1:0").unwrap();
    next_hop.set_read_timeout(Some(Duration::from_millis(250))).unwrap();
    let mut forwarder = RelayHopForwarder::bind(
        "127.0.0.1:0".parse().unwrap(),
        next_hop.local_addr().unwrap(),
        RelaySessionExecutor::new_with_hop_keys(
            RelaySessionConfig {
                relay_receiver_index: 77,
                expires_at_unix_us: 2_000,
                max_packet_size: Some(1_200),
                max_packets: None,
                max_bytes: None,
            },
            88,
            [22_u8; 32],
            upstream_to_relay,
        ),
    )
    .unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    for counter in 1..=3 {
        let inner_packet = format!("endpoint-encrypted-packet-{counter}");
        let hop_packet = encrypt_frame_with_counter(77, &upstream_to_relay, counter, inner_packet.as_bytes()).unwrap();
        sender.send_to(&hop_packet, forwarder.local_addr().unwrap()).unwrap();
    }

    let batch = forwarder.try_forward_many(8, 1_000).unwrap();
    let mut buffer = [0_u8; 2048];
    let mut received = Vec::new();
    for _ in 0..3 {
        let (length, _source) = next_hop.recv_from(&mut buffer).unwrap();
        received.push(String::from_utf8(buffer[..length].to_vec()).unwrap());
    }

    assert_eq!(batch.forwarded_packets, 3);
    assert_eq!(batch.emitted_packets, 3);
    assert_eq!(batch.dropped_packets, 0);
    assert_eq!(
        received,
        [
            "endpoint-encrypted-packet-1",
            "endpoint-encrypted-packet-2",
            "endpoint-encrypted-packet-3",
        ]
    );
    assert_eq!(forwarder.counters().forwarded_packets, 3);
    assert_eq!(forwarder.counters().emitted_packets, 3);
}

#[test]
fn relay_exit_unwraps_hop_packet_and_emits_opaque_inner_packet() {
    let upstream_to_exit = [11_u8; 32];
    let endpoint_core = UdpSocket::bind("127.0.0.1:0").unwrap();
    endpoint_core
        .set_read_timeout(Some(Duration::from_millis(250)))
        .unwrap();
    let mut forwarder = RelayHopExitForwarder::bind(
        "127.0.0.1:0".parse().unwrap(),
        endpoint_core.local_addr().unwrap(),
        RelaySessionExecutor::new_with_hop_keys(
            RelaySessionConfig {
                relay_receiver_index: 77,
                expires_at_unix_us: 2_000,
                max_packet_size: Some(1_200),
                max_packets: None,
                max_bytes: None,
            },
            77,
            [0_u8; 32],
            upstream_to_exit,
        ),
    )
    .unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let inner_packet = b"endpoint-encrypted-packet";
    let hop_packet = encrypt_frame_with_counter(77, &upstream_to_exit, 1, inner_packet).unwrap();
    sender.send_to(&hop_packet, forwarder.local_addr().unwrap()).unwrap();

    let outcome = forwarder.try_forward_one(1_000).unwrap();
    let mut buffer = [0_u8; 2048];
    let (length, _source) = endpoint_core.recv_from(&mut buffer).unwrap();

    assert!(matches!(outcome, RelayForwardOutcome::Forwarded { .. }));
    assert_eq!(&buffer[..length], inner_packet);
    assert_eq!(forwarder.counters().forwarded_packets, 1);
    assert_eq!(forwarder.counters().emitted_packets, 1);
    assert_eq!(forwarder.counters().emitted_bytes, inner_packet.len() as u64);
}

#[test]
fn relay_forwarder_drops_invalid_packets_without_network_response() {
    let next_hop = UdpSocket::bind("127.0.0.1:0").unwrap();
    next_hop.set_read_timeout(Some(Duration::from_millis(50))).unwrap();
    let mut forwarder = RelayHopForwarder::bind(
        "127.0.0.1:0".parse().unwrap(),
        next_hop.local_addr().unwrap(),
        RelaySessionExecutor::new_with_hop_keys(
            RelaySessionConfig {
                relay_receiver_index: 77,
                expires_at_unix_us: 2_000,
                max_packet_size: Some(1_200),
                max_packets: None,
                max_bytes: None,
            },
            88,
            [22_u8; 32],
            [11_u8; 32],
        ),
    )
    .unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    sender
        .send_to(b"not-authentic", forwarder.local_addr().unwrap())
        .unwrap();

    let outcome = forwarder.try_forward_one(1_000).unwrap();
    let mut buffer = [0_u8; 2048];

    assert!(matches!(
        outcome,
        RelayForwardOutcome::Dropped {
            reason: RelayDropReason::HopAuthFailed,
            ..
        }
    ));
    assert!(next_hop.recv_from(&mut buffer).is_err());
    assert_eq!(forwarder.counters().dropped_packets, 1);
    assert_eq!(forwarder.counters().emitted_packets, 0);
}
