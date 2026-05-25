//! Cheap relay-hop executor primitives.
//!
//! Python owns relay meaning: topology, roles, peer identities, next-hop policy,
//! and operator diagnostics. This module only checks compact facts compiled by
//! Python so the eventual relay hot path can fail closed without string policy.

use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};
use std::ops::Range;
#[cfg(unix)]
use std::os::fd::AsRawFd;

use gatherlink_crypto::envelope::TransportKeys;

use crate::udp_batch::{drain_udp_socket_mut, send_udp_many};

const UDP_SOCKET_BUFFER_BYTES: usize = 1024 * 1024 * 1024;

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
    /// One packet was authenticated, unwrapped by one hop layer, and sent to the next hop.
    Forwarded {
        /// Source socket address of the received hop packet.
        source: SocketAddr,
        /// Authenticated hop packet bytes received from the previous hop.
        received_bytes: usize,
        /// Opaque inner bytes sent to the next hop.
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

/// Aggregate result from a bounded relay socket drain.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct RelayForwardBatch {
    /// Authenticated hop packets accepted by this relay.
    pub forwarded_packets: u64,
    /// Packets dropped locally before any network response.
    pub dropped_packets: u64,
    /// Packets emitted to the compiled next hop.
    pub emitted_packets: u64,
    /// UDP bytes received by this relay drain.
    pub received_bytes: u64,
    /// UDP bytes emitted to the compiled next hop.
    pub emitted_bytes: u64,
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

    /// Create executor state with packet-rate hop AEAD outer-envelope unwrap keys.
    #[must_use]
    pub fn new_with_hop_keys(
        config: RelaySessionConfig,
        _next_hop_receiver_index: u32,
        _send_key: [u8; 32],
        receive_key: [u8; 32],
    ) -> Self {
        let relay_receiver_index = config.relay_receiver_index;
        Self {
            config,
            counters: RelaySessionCounters::default(),
            hop_keys: Some(TransportKeys::new_with_receiver_indexes(
                relay_receiver_index,
                relay_receiver_index,
                [0_u8; 32],
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

    /// Authenticate one outer hop envelope and return the remaining opaque packet.
    ///
    /// This is the final-hop exit primitive: the relay/exit process may remove
    /// only the hop envelope it is authorized to see, then hand the still
    /// endpoint-encrypted packet to the local endpoint core. It still does not
    /// inspect endpoint service ids, path ids, payloads, or control meaning.
    pub fn unwrap_authenticated_hop_packet(
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
        match self.authorize_packet(decrypted.receiver_index, decrypted.plaintext.len(), now_unix_us) {
            RelayPacketDecision::Forward => Ok(decrypted.plaintext),
            RelayPacketDecision::Drop(reason) => Err(reason),
        }
    }

    /// Authenticate one outer hop envelope in-place and return the remaining opaque packet range.
    pub fn unwrap_authenticated_hop_packet_in_place(
        &mut self,
        packet: &mut [u8],
        now_unix_us: u64,
    ) -> Result<Range<usize>, RelayDropReason> {
        let decrypted = {
            let Some(hop_keys) = self.hop_keys.as_mut() else {
                self.counters.dropped_packets = self.counters.dropped_packets.saturating_add(1);
                return Err(RelayDropReason::HopCryptoUnavailable);
            };
            hop_keys.decrypt_packet_in_place(packet).map_err(|_error| {
                self.counters.dropped_packets = self.counters.dropped_packets.saturating_add(1);
                RelayDropReason::HopAuthFailed
            })?
        };
        let plaintext_len = decrypted.plaintext_range.len();
        match self.authorize_packet(decrypted.receiver_index, plaintext_len, now_unix_us) {
            RelayPacketDecision::Forward => Ok(decrypted.plaintext_range),
            RelayPacketDecision::Drop(reason) => Err(reason),
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
    receive_buffers: Vec<Vec<u8>>,
    forward_ranges: Vec<(usize, Range<usize>)>,
}

impl RelayHopForwarder {
    /// Bind a relay receive socket and pair it with one compiled next-hop endpoint.
    pub fn bind(
        listen: SocketAddr,
        next_hop: SocketAddr,
        executor: RelaySessionExecutor,
    ) -> Result<Self, RelayForwardError> {
        let socket = UdpSocket::bind(listen).map_err(RelayForwardError::BindFailed)?;
        request_udp_socket_buffers(&socket);
        socket
            .set_nonblocking(true)
            .map_err(RelayForwardError::ConfigureSocketFailed)?;
        Ok(Self {
            socket,
            next_hop,
            executor,
            receive_buffers: Vec::new(),
            forward_ranges: Vec::new(),
        })
    }

    /// Return the actual bound relay socket address.
    pub fn local_addr(&self) -> Result<SocketAddr, RelayForwardError> {
        self.socket.local_addr().map_err(RelayForwardError::ReceiveFailed)
    }

    /// Poll one relay-hop packet, remove this hop envelope, and forward the remaining opaque packet.
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
        buffer.truncate(received_len);
        let inner_range = match self
            .executor
            .unwrap_authenticated_hop_packet_in_place(&mut buffer, now_unix_us)
        {
            Ok(range) => range,
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
            .send_to(&buffer[inner_range], self.next_hop)
            .map_err(RelayForwardError::SendFailed)?;
        self.executor.record_emitted(emitted_bytes);
        Ok(RelayForwardOutcome::Forwarded {
            source,
            received_bytes: received_len,
            emitted_bytes,
        })
    }

    /// Drain up to `max_packets` ready UDP packets without crossing back into Python per packet.
    ///
    /// Python still owns lifecycle, policy, and diagnostics cadence. This is only
    /// a packet-speed primitive for a Python-compiled relay service.
    pub fn try_forward_many(
        &mut self,
        max_packets: usize,
        now_unix_us: u64,
    ) -> Result<RelayForwardBatch, RelayForwardError> {
        let mut batch = RelayForwardBatch::default();
        if max_packets == 0 {
            return Ok(batch);
        }
        self.forward_ranges.clear();
        let drain_result = drain_udp_socket_mut(
            &self.socket,
            &mut self.receive_buffers,
            max_packets,
            |index, datagram| {
                batch.received_bytes = batch.received_bytes.saturating_add(datagram.payload.len() as u64);
                match self
                    .executor
                    .unwrap_authenticated_hop_packet_in_place(datagram.payload, now_unix_us)
                {
                    Ok(range) => self.forward_ranges.push((index, range)),
                    Err(_reason) => {
                        batch.dropped_packets = batch.dropped_packets.saturating_add(1);
                    }
                }
                Ok::<(), RelayForwardError>(())
            },
        )
        .map_err(RelayForwardError::ReceiveFailed)?;
        match drain_result {
            Ok(_count) => {}
            Err(error) => return Err(error),
        }
        if self.forward_ranges.is_empty() {
            return Ok(batch);
        }
        let packet_slices = self
            .forward_ranges
            .iter()
            .map(|(index, range)| &self.receive_buffers[*index][range.clone()])
            .collect::<Vec<_>>();
        let emitted =
            send_udp_many(&self.socket, self.next_hop, &packet_slices).map_err(RelayForwardError::SendFailed)?;
        for (_index, range) in self.forward_ranges.iter().take(emitted) {
            let packet_len = range.len();
            self.executor.record_emitted(packet_len);
            batch.forwarded_packets = batch.forwarded_packets.saturating_add(1);
            batch.emitted_packets = batch.emitted_packets.saturating_add(1);
            batch.emitted_bytes = batch.emitted_bytes.saturating_add(packet_len as u64);
        }
        Ok(batch)
    }

    /// Return current relay executor counters.
    #[must_use]
    pub fn counters(&self) -> RelaySessionCounters {
        self.executor.counters()
    }
}

/// One final-hop relay exit that unwraps an outer hop envelope and emits the opaque endpoint packet.
#[derive(Debug)]
pub struct RelayHopExitForwarder {
    socket: UdpSocket,
    next_hop: SocketAddr,
    executor: RelaySessionExecutor,
    receive_buffers: Vec<Vec<u8>>,
    forward_ranges: Vec<(usize, Range<usize>)>,
}

impl RelayHopExitForwarder {
    /// Bind a relay exit receive socket and endpoint-core next-hop endpoint.
    pub fn bind(
        listen: SocketAddr,
        next_hop: SocketAddr,
        executor: RelaySessionExecutor,
    ) -> Result<Self, RelayForwardError> {
        let socket = UdpSocket::bind(listen).map_err(RelayForwardError::BindFailed)?;
        request_udp_socket_buffers(&socket);
        socket
            .set_nonblocking(true)
            .map_err(RelayForwardError::ConfigureSocketFailed)?;
        Ok(Self {
            socket,
            next_hop,
            executor,
            receive_buffers: Vec::new(),
            forward_ranges: Vec::new(),
        })
    }

    /// Return the actual bound relay-exit socket address.
    pub fn local_addr(&self) -> Result<SocketAddr, RelayForwardError> {
        self.socket.local_addr().map_err(RelayForwardError::ReceiveFailed)
    }

    /// Poll one relay-hop packet, unwrap it, and forward the inner endpoint packet.
    pub fn try_forward_one(&mut self, now_unix_us: u64) -> Result<RelayForwardOutcome, RelayForwardError> {
        let mut buffer = vec![0_u8; u16::MAX as usize];
        let (length, source) = match self.socket.recv_from(&mut buffer) {
            Ok(received) => received,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut | std::io::ErrorKind::Interrupted
                ) =>
            {
                return Ok(RelayForwardOutcome::NoPacket);
            }
            Err(error) => return Err(RelayForwardError::ReceiveFailed(error)),
        };
        buffer.truncate(length);
        let inner_range = match self
            .executor
            .unwrap_authenticated_hop_packet_in_place(&mut buffer, now_unix_us)
        {
            Ok(range) => range,
            Err(reason) => {
                return Ok(RelayForwardOutcome::Dropped {
                    source,
                    reason,
                    received_bytes: length,
                });
            }
        };
        let emitted_bytes = self
            .socket
            .send_to(&buffer[inner_range], self.next_hop)
            .map_err(RelayForwardError::SendFailed)?;
        self.executor.record_emitted(emitted_bytes);
        Ok(RelayForwardOutcome::Forwarded {
            source,
            received_bytes: length,
            emitted_bytes,
        })
    }

    /// Drain up to `max_packets` ready final-hop packets.
    ///
    /// This keeps the relay-exit hot loop in Rust while leaving Python in
    /// charge of service lifecycle and operator-facing meaning.
    pub fn try_forward_many(
        &mut self,
        max_packets: usize,
        now_unix_us: u64,
    ) -> Result<RelayForwardBatch, RelayForwardError> {
        let mut batch = RelayForwardBatch::default();
        if max_packets == 0 {
            return Ok(batch);
        }
        self.forward_ranges.clear();
        let drain_result = drain_udp_socket_mut(
            &self.socket,
            &mut self.receive_buffers,
            max_packets,
            |index, datagram| {
                batch.received_bytes = batch.received_bytes.saturating_add(datagram.payload.len() as u64);
                match self
                    .executor
                    .unwrap_authenticated_hop_packet_in_place(datagram.payload, now_unix_us)
                {
                    Ok(range) => self.forward_ranges.push((index, range)),
                    Err(_reason) => {
                        batch.dropped_packets = batch.dropped_packets.saturating_add(1);
                    }
                }
                Ok::<(), RelayForwardError>(())
            },
        )
        .map_err(RelayForwardError::ReceiveFailed)?;
        match drain_result {
            Ok(_count) => {}
            Err(error) => return Err(error),
        }
        if self.forward_ranges.is_empty() {
            return Ok(batch);
        }
        let packet_slices = self
            .forward_ranges
            .iter()
            .map(|(index, range)| &self.receive_buffers[*index][range.clone()])
            .collect::<Vec<_>>();
        let emitted =
            send_udp_many(&self.socket, self.next_hop, &packet_slices).map_err(RelayForwardError::SendFailed)?;
        for (_index, range) in self.forward_ranges.iter().take(emitted) {
            let packet_len = range.len();
            self.executor.record_emitted(packet_len);
            batch.forwarded_packets = batch.forwarded_packets.saturating_add(1);
            batch.emitted_packets = batch.emitted_packets.saturating_add(1);
            batch.emitted_bytes = batch.emitted_bytes.saturating_add(packet_len as u64);
        }
        Ok(batch)
    }

    /// Return current relay executor counters.
    pub fn counters(&self) -> RelaySessionCounters {
        self.executor.counters()
    }
}

#[cfg(unix)]
fn request_udp_socket_buffers(socket: &UdpSocket) {
    let value = UDP_SOCKET_BUFFER_BYTES as libc::c_int;
    let value_ptr = (&value as *const libc::c_int).cast::<libc::c_void>();
    let value_len = std::mem::size_of_val(&value) as libc::socklen_t;
    unsafe {
        let _ = libc::setsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_RCVBUF,
            value_ptr,
            value_len,
        );
        let _ = libc::setsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            value_ptr,
            value_len,
        );
    }
}

#[cfg(not(unix))]
fn request_udp_socket_buffers(_socket: &UdpSocket) {}
