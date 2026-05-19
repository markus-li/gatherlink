use std::net::UdpSocket;
use std::time::Duration;

use gatherlink_crypto::envelope::{decrypt_packet_without_replay, encrypt_frame_with_counter};
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
fn relay_executor_authenticates_and_reseals_opaque_inner_packet() {
    let upstream_to_relay = [11_u8; 32];
    let relay_to_next_hop = [22_u8; 32];
    let mut executor = RelaySessionExecutor::new_with_hop_keys(
        RelaySessionConfig {
            relay_receiver_index: 77,
            expires_at_unix_us: 2_000,
            max_packet_size: Some(1_200),
            max_packets: None,
            max_bytes: None,
        },
        88,
        relay_to_next_hop,
        upstream_to_relay,
    );
    let inner_packet = b"endpoint-encrypted-packet";
    let hop_packet = encrypt_frame_with_counter(77, &upstream_to_relay, 1, inner_packet).unwrap();

    let rewrapped = executor.rewrap_authenticated_hop_packet(&hop_packet, 1_000).unwrap();
    let decrypted_for_next = decrypt_packet_without_replay(&relay_to_next_hop, &rewrapped).unwrap();

    assert_eq!(decrypted_for_next.receiver_index, 88);
    assert_eq!(decrypted_for_next.plaintext, inner_packet);
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
        executor.rewrap_authenticated_hop_packet(b"not-authentic", 1_000),
        Err(RelayDropReason::HopAuthFailed)
    );
    assert_eq!(executor.counters().dropped_packets, 1);
}

#[test]
fn relay_forwarder_reseals_and_sends_to_compiled_next_hop() {
    let upstream_to_relay = [11_u8; 32];
    let relay_to_next_hop = [22_u8; 32];
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
            relay_to_next_hop,
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
    let decrypted_for_next = decrypt_packet_without_replay(&relay_to_next_hop, &buffer[..length]).unwrap();

    assert!(matches!(outcome, RelayForwardOutcome::Forwarded { .. }));
    assert_eq!(decrypted_for_next.receiver_index, 88);
    assert_eq!(decrypted_for_next.plaintext, inner_packet);
    assert_eq!(forwarder.counters().forwarded_packets, 1);
    assert_eq!(forwarder.counters().emitted_packets, 1);
    assert_eq!(forwarder.counters().emitted_bytes, length as u64);
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
