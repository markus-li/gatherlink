//! Bounded per-path scheduler queues.
//!
//! These queues are intentionally small and explicit. Gatherlink does not hide
//! ordinary UDP loss with retransmission; if a path queue overflows, Rust drops
//! locally and reports counters so Python can adjust scheduling policy.

use std::collections::VecDeque;

use gatherlink_protocol::ids::PathId;

/// One queued encoded payload waiting for a path transport to become writable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueuedPacket {
    payload: Vec<u8>,
    enqueued_at_us: u64,
}

impl QueuedPacket {
    /// Build a packet with the caller's monotonic timestamp in microseconds.
    pub fn new(payload: Vec<u8>, enqueued_at_us: u64) -> Self {
        Self {
            payload,
            enqueued_at_us,
        }
    }

    /// Packet bytes that would be sent on the underlying path.
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }

    /// Number of bytes currently buffered by this packet.
    pub fn len(&self) -> usize {
        self.payload.len()
    }

    /// Return whether the queued payload is empty.
    pub fn is_empty(&self) -> bool {
        self.payload.is_empty()
    }
}

/// Static queue limits compiled by Python for one path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PathQueueLimits {
    pub max_packets: usize,
    pub max_bytes: usize,
}

impl PathQueueLimits {
    /// Build queue limits. A zero value means that dimension is unlimited.
    pub fn new(max_packets: usize, max_bytes: usize) -> Self {
        Self { max_packets, max_bytes }
    }

    fn allows_packets(&self, packets: usize) -> bool {
        self.max_packets == 0 || packets <= self.max_packets
    }

    fn allows_bytes(&self, bytes: usize) -> bool {
        self.max_bytes == 0 || bytes <= self.max_bytes
    }
}

/// Observable queue counters for service status and scheduler policy.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PathQueueSnapshot {
    pub path_id: PathId,
    pub depth_packets: usize,
    pub depth_bytes: usize,
    pub oldest_age_us: u64,
    pub dropped_packets: u64,
    pub dropped_bytes: u64,
}

/// Small FIFO queue for one path.
#[derive(Debug, Clone)]
pub struct BoundedPathQueue {
    path_id: PathId,
    limits: PathQueueLimits,
    packets: VecDeque<QueuedPacket>,
    depth_bytes: usize,
    dropped_packets: u64,
    dropped_bytes: u64,
}

impl BoundedPathQueue {
    /// Create an empty path queue with Python-selected limits.
    pub fn new(path_id: PathId, limits: PathQueueLimits) -> Self {
        Self {
            path_id,
            limits,
            packets: VecDeque::new(),
            depth_bytes: 0,
            dropped_packets: 0,
            dropped_bytes: 0,
        }
    }

    /// Push a packet or count it as locally dropped when limits would overflow.
    pub fn push(&mut self, packet: QueuedPacket) -> Result<(), QueueDrop> {
        let next_packets = self.packets.len() + 1;
        let next_bytes = self.depth_bytes.saturating_add(packet.len());
        if !self.limits.allows_packets(next_packets) || !self.limits.allows_bytes(next_bytes) {
            self.dropped_packets += 1;
            self.dropped_bytes = self.dropped_bytes.saturating_add(packet.len() as u64);
            return Err(QueueDrop {
                path_id: self.path_id,
                payload_len: packet.len(),
            });
        }

        self.depth_bytes = next_bytes;
        self.packets.push_back(packet);
        Ok(())
    }

    /// Pop the oldest packet for FIFO path transmission.
    pub fn pop(&mut self) -> Option<QueuedPacket> {
        let packet = self.packets.pop_front()?;
        self.depth_bytes = self.depth_bytes.saturating_sub(packet.len());
        Some(packet)
    }

    /// Return current counters using the caller's monotonic time.
    pub fn snapshot(&self, now_us: u64) -> PathQueueSnapshot {
        let oldest_age_us = self
            .packets
            .front()
            .map(|packet| now_us.saturating_sub(packet.enqueued_at_us))
            .unwrap_or(0);
        PathQueueSnapshot {
            path_id: self.path_id,
            depth_packets: self.packets.len(),
            depth_bytes: self.depth_bytes,
            oldest_age_us,
            dropped_packets: self.dropped_packets,
            dropped_bytes: self.dropped_bytes,
        }
    }
}

/// A packet was intentionally dropped because a bounded queue was full.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QueueDrop {
    pub path_id: PathId,
    pub payload_len: usize,
}
