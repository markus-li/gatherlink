//! Cheap relay-hop executor primitives.
//!
//! Python owns relay meaning: topology, roles, peer identities, next-hop policy,
//! and operator diagnostics. This module only checks compact facts compiled by
//! Python so the eventual relay hot path can fail closed without string policy.

use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};

use gatherlink_crypto::envelope::TransportKeys;

/// Compiled relay-hop facts accepted by the Rust executor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelaySessionConfig {
    /// Receiver index accepted by this relay-hop session.
    pub relay_receiver_index: u32,
    /// Session expiry as Unix microseconds.
    pub expires_at_unix_us: u64,
    /// Optional maximum authenticated packet size for this relay hop.
    pub max_packet_size: Option<usize>,
    /// Optional maximum forwarded packet count.
    pub max_packets: Option<u64>,
    /// Optional maximum forwarded byte count.
    pub max_bytes: Option<u64>,
}

/// Counters for one compiled relay-hop session.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct RelaySessionCounters {
    /// Packets accepted for forwarding.
    pub forwarded_packets: u64,
    /// Bytes accepted for forwarding.
    pub forwarded_bytes: u64,
    /// Packets dropped by compiled relay checks.
    pub dropped_packets: u64,
    /// Packets successfully emitted to the compiled next-hop socket.
    pub emitted_packets: u64,
    /// Bytes successfully emitted to the compiled next-hop socket.
    pub emitted_bytes: u64,
}

/// Local fail-closed reason for relay executor decisions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RelayDropReason {
    /// No compiled session accepted the receiver index.
    UnknownReceiverIndex,
    /// Session expired before this packet arrived.
    ExpiredSession,
    /// Packet exceeds the compiled maximum packet size.
    PacketTooLarge,
    /// Packet or byte forwarding limit would be exceeded.
    LimitExceeded,
    /// Relay-hop crypto keys were not compiled for this executor.
    HopCryptoUnavailable,
    /// Hop packet failed authentication, receiver-index, or replay checks.
    HopAuthFailed,
}

/// Local relay forwarding I/O failure.
#[derive(Debug)]
pub enum RelayForwardError {
    /// Failed to bind the relay receive socket.
    BindFailed(std::io::Error),
    /// Failed to configure relay socket mode.
    ConfigureSocketFailed(std::io::Error),
    /// Failed while receiving from the relay socket.
    ReceiveFailed(std::io::Error),
    /// Failed while sending to the compiled next-hop endpoint.
    SendFailed(std::io::Error),
}

/// Observable result from one relay socket poll.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RelayForwardOutcome {
    /// No packet was available on the relay socket.
    NoPacket,
    /// One packet was authenticated, resealed, and sent to the next hop.
    Forwarded {
        /// Source socket address of the received hop packet.
        source: SocketAddr,
        /// Authenticated hop packet bytes received from the previous hop.
        received_bytes: usize,
        /// Resealed bytes sent to the next hop.
        emitted_bytes: usize,
    },
    /// One packet was rejected locally and no network response was emitted.
    Dropped {
        /// Source socket address of the received hop packet.
        source: SocketAddr,
        /// Local drop reason.
        reason: RelayDropReason,
        /// Received packet size.
        received_bytes: usize,
    },
}

/// One relay executor decision.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RelayPacketDecision {
    /// Packet can continue to hop AEAD/replay/forwarding.
    Forward,
    /// Packet must be silently dropped on the network.
    Drop(RelayDropReason),
}

/// Mutable executor state for one relay-hop session.
#[derive(Debug, Clone)]
pub struct RelaySessionExecutor {
    config: RelaySessionConfig,
    counters: RelaySessionCounters,
    hop_keys: Option<TransportKeys>,
}

impl RelaySessionExecutor {
    /// Create executor state from Python-compiled relay facts.
    #[must_use]
    pub fn new(config: RelaySessionConfig) -> Self {
        Self {
            config,
            counters: RelaySessionCounters::default(),
            hop_keys: None,
        }
    }

    /// Create executor state with packet-rate hop AEAD outer-envelope unwrap/reseal keys.
    #[must_use]
    pub fn new_with_hop_keys(
        config: RelaySessionConfig,
        next_hop_receiver_index: u32,
        send_key: [u8; 32],
        receive_key: [u8; 32],
    ) -> Self {
        let relay_receiver_index = config.relay_receiver_index;
        Self {
            config,
            counters: RelaySessionCounters::default(),
            hop_keys: Some(TransportKeys::new_with_receiver_indexes(
                relay_receiver_index,
                next_hop_receiver_index,
                send_key,
                receive_key,
            )),
        }
    }

    /// Check and account one authenticated relay-hop packet.
    pub fn authorize_packet(
        &mut self,
        relay_receiver_index: u32,
        packet_size: usize,
        now_unix_us: u64,
    ) -> RelayPacketDecision {
        let decision = self.check_packet(relay_receiver_index, packet_size, now_unix_us);
        match decision {
            RelayPacketDecision::Forward => {
                self.counters.forwarded_packets = self.counters.forwarded_packets.saturating_add(1);
                self.counters.forwarded_bytes = self.counters.forwarded_bytes.saturating_add(packet_size as u64);
            }
            RelayPacketDecision::Drop(_) => {
                self.counters.dropped_packets = self.counters.dropped_packets.saturating_add(1);
            }
        }
        decision
    }

    /// Authenticate one outer hop envelope, enforce limits, and reseal opaque bytes for the next hop.
    ///
    /// The outer hop plaintext is still the endpoint-encrypted packet. Relay
    /// code must treat it as opaque bytes; only the final endpoint session can
    /// decrypt endpoint service/frame contents.
    pub fn rewrap_authenticated_hop_packet(
        &mut self,
        packet: &[u8],
        now_unix_us: u64,
    ) -> Result<Vec<u8>, RelayDropReason> {
        let decrypted = {
            let Some(hop_keys) = self.hop_keys.as_mut() else {
                self.counters.dropped_packets = self.counters.dropped_packets.saturating_add(1);
                return Err(RelayDropReason::HopCryptoUnavailable);
            };
            hop_keys.decrypt_packet(packet).map_err(|_error| {
                self.counters.dropped_packets = self.counters.dropped_packets.saturating_add(1);
                RelayDropReason::HopAuthFailed
            })?
        };
        match self.check_packet(decrypted.receiver_index, decrypted.plaintext.len(), now_unix_us) {
            RelayPacketDecision::Forward => {
                self.counters.forwarded_packets = self.counters.forwarded_packets.saturating_add(1);
                self.counters.forwarded_bytes = self
                    .counters
                    .forwarded_bytes
                    .saturating_add(decrypted.plaintext.len() as u64);
                self.hop_keys
                    .as_mut()
                    .ok_or(RelayDropReason::HopCryptoUnavailable)?
                    .encrypt_frame(&decrypted.plaintext)
                    .map_err(|_error| RelayDropReason::HopAuthFailed)
            }
            RelayPacketDecision::Drop(reason) => {
                self.counters.dropped_packets = self.counters.dropped_packets.saturating_add(1);
                Err(reason)
            }
        }
    }

    /// Return current counters without exposing mutable state.
    #[must_use]
    pub fn counters(&self) -> RelaySessionCounters {
        self.counters.clone()
    }

    fn record_emitted(&mut self, bytes: usize) {
        self.counters.emitted_packets = self.counters.emitted_packets.saturating_add(1);
        self.counters.emitted_bytes = self.counters.emitted_bytes.saturating_add(bytes as u64);
    }

    fn check_packet(&self, relay_receiver_index: u32, packet_size: usize, now_unix_us: u64) -> RelayPacketDecision {
        if relay_receiver_index != self.config.relay_receiver_index {
            return RelayPacketDecision::Drop(RelayDropReason::UnknownReceiverIndex);
        }
        if now_unix_us >= self.config.expires_at_unix_us {
            return RelayPacketDecision::Drop(RelayDropReason::ExpiredSession);
        }
        if self
            .config
            .max_packet_size
            .is_some_and(|max_packet_size| packet_size > max_packet_size)
        {
            return RelayPacketDecision::Drop(RelayDropReason::PacketTooLarge);
        }
        if self
            .config
            .max_packets
            .is_some_and(|max_packets| self.counters.forwarded_packets >= max_packets)
            || self
                .config
                .max_bytes
                .is_some_and(|max_bytes| self.counters.forwarded_bytes.saturating_add(packet_size as u64) > max_bytes)
        {
            return RelayPacketDecision::Drop(RelayDropReason::LimitExceeded);
        }
        RelayPacketDecision::Forward
    }
}

/// One compiled relay-hop UDP forwarder.
///
/// Python owns session authorization, key selection, next-hop selection, and
/// operator diagnostics. This type deliberately contains no endpoint service
/// id, route label, topology role, or helper behavior.
#[derive(Debug)]
pub struct RelayHopForwarder {
    socket: UdpSocket,
    next_hop: SocketAddr,
    executor: RelaySessionExecutor,
}

impl RelayHopForwarder {
    /// Bind a relay receive socket and pair it with one compiled next-hop endpoint.
    pub fn bind(
        listen: SocketAddr,
        next_hop: SocketAddr,
        executor: RelaySessionExecutor,
    ) -> Result<Self, RelayForwardError> {
        let socket = UdpSocket::bind(listen).map_err(RelayForwardError::BindFailed)?;
        socket
            .set_nonblocking(true)
            .map_err(RelayForwardError::ConfigureSocketFailed)?;
        Ok(Self {
            socket,
            next_hop,
            executor,
        })
    }

    /// Return the actual bound relay socket address.
    pub fn local_addr(&self) -> Result<SocketAddr, RelayForwardError> {
        self.socket.local_addr().map_err(RelayForwardError::ReceiveFailed)
    }

    /// Poll one relay-hop packet and forward it when it authenticates.
    ///
    /// Invalid packets are dropped silently on the network. The returned outcome
    /// is only for local counters, diagnostics, and tests.
    pub fn try_forward_one(&mut self, now_unix_us: u64) -> Result<RelayForwardOutcome, RelayForwardError> {
        let mut buffer = vec![0_u8; u16::MAX as usize];
        let (received_len, source) = match self.socket.recv_from(&mut buffer) {
            Ok(received) => received,
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) =>
            {
                return Ok(RelayForwardOutcome::NoPacket);
            }
            Err(error) => return Err(RelayForwardError::ReceiveFailed(error)),
        };
        let packet = &buffer[..received_len];
        let resealed = match self.executor.rewrap_authenticated_hop_packet(packet, now_unix_us) {
            Ok(resealed) => resealed,
            Err(reason) => {
                return Ok(RelayForwardOutcome::Dropped {
                    source,
                    reason,
                    received_bytes: received_len,
                });
            }
        };
        let emitted_bytes = self
            .socket
            .send_to(&resealed, self.next_hop)
            .map_err(RelayForwardError::SendFailed)?;
        self.executor.record_emitted(emitted_bytes);
        Ok(RelayForwardOutcome::Forwarded {
            source,
            received_bytes: received_len,
            emitted_bytes,
        })
    }

    /// Return current relay executor counters.
    #[must_use]
    pub fn counters(&self) -> RelaySessionCounters {
        self.executor.counters()
    }
}
