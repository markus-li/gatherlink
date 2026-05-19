//! Dataplane telemetry counters.
//!
//! These structs deliberately stay boring: they count what Rust actually did
//! or observed, and leave interpretation to Python. That keeps monitoring,
//! scheduling decisions, labels, alerts, and policy out of the fast path while
//! still making real-world service behavior visible through one status shape.

use std::collections::BTreeMap;

use gatherlink_protocol::control::GlobalSequenceTracker;
use gatherlink_protocol::ids::ServiceId;

use crate::engine::ForwardOutcome;
use crate::runtime_config::CorePathConfig;

/// Snapshot for one service or path row in Python/service monitor status.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CounterSnapshot {
    pub packets: u64,
    pub bytes: u64,
    pub tx_packets: u64,
    pub tx_bytes: u64,
    pub rx_packets: u64,
    pub rx_bytes: u64,
    pub missed_packets: u64,
    pub reordered_packets: u64,
    pub packets_needing_reorder: u64,
    pub expected_duplicate_packets: u64,
    pub unexpected_duplicate_packets: u64,
    pub duplicate_packets: u64,
    pub send_failed_packets: u64,
    pub send_failed_bytes: u64,
    pub fanout_send_failed_packets: u64,
    pub fanout_send_failed_bytes: u64,
    pub security_drop_packets: u64,
    pub security_drop_bytes: u64,
}

/// Reserved-service frame counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ControlCounterSnapshot {
    pub frames: u64,
    pub bytes: u64,
}

/// Per-path control metaband counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PathControlSnapshot {
    pub tx: ControlCounterSnapshot,
    pub rx: ControlCounterSnapshot,
}

/// Reserved-service traffic counters observed by the Rust dataplane.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ControlMetadataSnapshot {
    pub sent: ControlCounterSnapshot,
    pub received: ControlCounterSnapshot,
    pub path_control: BTreeMap<u16, PathControlSnapshot>,
}

/// Local-only security drop counters.
///
/// Rust deliberately keeps this aggregate. The crypto envelope collapses
/// malformed, unauthenticated, wrong-receiver, and replayed packets into one
/// silent-drop result so the network behavior cannot reveal which check failed.
/// Python can still turn the aggregate into operator diagnostics.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SecurityDropSnapshot {
    pub packets: u64,
    pub bytes: u64,
}

/// Full dataplane metrics snapshot.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MetricsSnapshot {
    pub services: BTreeMap<String, CounterSnapshot>,
    pub paths: BTreeMap<u16, CounterSnapshot>,
    pub control_metadata: ControlMetadataSnapshot,
    pub security_drops: SecurityDropSnapshot,
}

/// Runtime counters owned by the Rust dataplane.
#[derive(Debug, Clone, Default)]
pub struct DataplaneMetrics {
    services: BTreeMap<String, CounterSnapshot>,
    paths: BTreeMap<u16, CounterSnapshot>,
    receive_sequences: BTreeMap<ServiceId, GlobalSequenceTracker>,
    control_metadata: ControlMetadataSnapshot,
    security_drops: SecurityDropSnapshot,
}

impl DataplaneMetrics {
    /// Create counters with path rows pre-registered for stable monitor output.
    pub fn new(paths: &[CorePathConfig]) -> Self {
        let mut metrics = Self::default();
        metrics.reconcile_paths(paths);
        metrics
    }

    /// Keep path rows aligned with the current Python-compiled runtime config.
    pub fn reconcile_paths(&mut self, paths: &[CorePathConfig]) {
        for path in paths {
            self.paths.entry(path.path_id()).or_default();
        }
    }

    /// Record one virtual UDP datagram transmitted through a Gatherlink frame.
    pub fn record_forward(&mut self, outcome: &ForwardOutcome) {
        let bytes = outcome.payload_len as u64;
        let service = self.services.entry(outcome.service.clone()).or_default();
        service.packets += 1;
        service.bytes += bytes;
        service.tx_packets += 1;
        service.tx_bytes += bytes;

        let path = self.paths.entry(outcome.path_id).or_default();
        path.packets += 1;
        path.bytes += bytes;
        path.tx_packets += 1;
        path.tx_bytes += bytes;
    }

    /// Record one received Gatherlink data sequence before emitting the virtual UDP payload.
    ///
    /// This uses the protocol's global sequence tracker so normal traffic, not
    /// test payloads, drives missing and reorder counters.
    pub fn record_receive(&mut self, service_id: ServiceId, path_id: u16, sequence: u64, payload_len: usize) {
        let observation = self.receive_sequences.entry(service_id).or_default().observe(sequence);

        let path = self.paths.entry(path_id).or_default();
        path.packets += 1;
        path.bytes += payload_len as u64;
        path.rx_packets += 1;
        path.rx_bytes += payload_len as u64;
        path.missed_packets += observation.missing_packets;
        if observation.out_of_order {
            path.reordered_packets += 1;
            path.packets_needing_reorder += 1;
        } else {
            path.packets_needing_reorder += observation.missing_packets;
        }
    }

    /// Record one received Gatherlink data sequence and the local service it was emitted to.
    pub fn record_receive_for_service(
        &mut self,
        service_name: &str,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload_len: usize,
    ) {
        self.record_receive(service_id, path_id, sequence, payload_len);

        let service = self.services.entry(service_name.to_owned()).or_default();
        service.packets += 1;
        service.bytes += payload_len as u64;
        service.rx_packets += 1;
        service.rx_bytes += payload_len as u64;
    }

    /// Record a duplicate user-service frame that was not emitted locally.
    pub fn record_duplicate_receive(
        &mut self,
        service_name: Option<&str>,
        service_id: ServiceId,
        path_id: u16,
        payload_len: usize,
        expected: bool,
    ) {
        let path = self.paths.entry(path_id).or_default();
        path.rx_packets += 1;
        path.rx_bytes += payload_len as u64;
        if expected {
            path.expected_duplicate_packets += 1;
        } else {
            path.unexpected_duplicate_packets += 1;
            path.duplicate_packets += 1;
        }

        let service_key = service_name
            .map(str::to_owned)
            .unwrap_or_else(|| format!("service-id:{service_id}"));
        let service = self.services.entry(service_key).or_default();
        if expected {
            service.expected_duplicate_packets += 1;
        } else {
            service.unexpected_duplicate_packets += 1;
            service.duplicate_packets += 1;
        }
    }

    /// Record one failed path send. Python decides what this means for path policy.
    pub fn record_send_failed(&mut self, path_id: u16, frame_bytes: usize, expected_fanout: bool) {
        let path = self.paths.entry(path_id).or_default();
        path.send_failed_packets += 1;
        path.send_failed_bytes += frame_bytes as u64;
        if expected_fanout {
            path.fanout_send_failed_packets += 1;
            path.fanout_send_failed_bytes += frame_bytes as u64;
        }
    }

    /// Record one received reserved-service frame without decoding its payload.
    pub fn record_reserved_received(&mut self, path_id: u16, frame_bytes: usize) {
        self.control_metadata.received.frames += 1;
        self.control_metadata.received.bytes += frame_bytes as u64;
        let path = self.control_metadata.path_control.entry(path_id).or_default();
        path.rx.frames += 1;
        path.rx.bytes += frame_bytes as u64;
    }

    /// Record one sent reserved-service frame without decoding its payload.
    pub fn record_reserved_sent(&mut self, path_id: u16, frame_bytes: usize) {
        self.control_metadata.sent.frames += 1;
        self.control_metadata.sent.bytes += frame_bytes as u64;
        let path = self.control_metadata.path_control.entry(path_id).or_default();
        path.tx.frames += 1;
        path.tx.bytes += frame_bytes as u64;
    }

    /// Record one local-only transport/security drop.
    ///
    /// These packets must not generate network responses. Counting them here
    /// gives Python enough fact data to emit diagnostics and scheduler/security
    /// observations without teaching Rust policy or operator meaning.
    pub fn record_security_drop(&mut self, path_id: u16, packet_bytes: usize) {
        self.security_drops.packets += 1;
        self.security_drops.bytes += packet_bytes as u64;

        let path = self.paths.entry(path_id).or_default();
        path.security_drop_packets += 1;
        path.security_drop_bytes += packet_bytes as u64;
    }

    /// Return an immutable snapshot suitable for Python IPC/status conversion.
    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            services: self.services.clone(),
            paths: self.paths.clone(),
            control_metadata: self.control_metadata.clone(),
            security_drops: self.security_drops,
        }
    }
}
