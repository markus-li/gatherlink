//! Dataplane telemetry counters.
//!
//! These structs deliberately stay boring: they count what Rust actually did
//! or observed, and leave interpretation to Python. That keeps monitoring,
//! scheduling decisions, labels, alerts, and policy out of the fast path while
//! still making real-world service behavior visible through one status shape.

use std::collections::BTreeMap;
use std::time::Instant;

use gatherlink_protocol::control::{GlobalSequenceTracker, SequenceObservation};
use gatherlink_protocol::ids::ServiceId;

use crate::engine::ForwardOutcome;
use crate::runtime_config::CorePathConfig;

const DATA_TIMING_SAMPLE_SEQUENCE_INTERVAL: u64 = 1024;
const DATA_TIMING_SAMPLE_BUFFER_LIMIT: usize = 512;

/// Sparse real-data timing fact for Python-owned latency estimation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct DataTrafficTimingSample {
    pub path_id: u16,
    pub sequence: u64,
    pub packet_count: u32,
    pub observed_at_us: u64,
    pub peer_scope: Option<u32>,
}

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
    pub last_tx_at_us: u64,
    pub last_rx_at_us: u64,
    pub last_tx_gap_us: u64,
    pub last_rx_gap_us: u64,
    pub scheduler_in_flight_packets: u64,
    pub scheduler_in_flight_bytes: u64,
    pub scheduler_predicted_delivery_us: u64,
    pub reorder_buffer_packets: u64,
    pub reorder_buffer_oldest_age_us: u64,
    pub socket_receive_buffer_bytes: u64,
    pub socket_send_buffer_bytes: u64,
    pub socket_drain_quantum: u64,
}

/// Reserved-service frame counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ControlCounterSnapshot {
    pub frames: u64,
    pub bytes: u64,
    pub last_at_us: u64,
    pub last_gap_us: u64,
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
    pub service_paths: BTreeMap<String, BTreeMap<u16, CounterSnapshot>>,
    pub control_metadata: ControlMetadataSnapshot,
    pub security_drops: SecurityDropSnapshot,
}

/// Runtime counters owned by the Rust dataplane.
#[derive(Debug, Clone)]
pub struct DataplaneMetrics {
    started_at: Instant,
    services: BTreeMap<String, CounterSnapshot>,
    paths: BTreeMap<u16, CounterSnapshot>,
    service_paths: BTreeMap<String, BTreeMap<u16, CounterSnapshot>>,
    receive_sequences: BTreeMap<Option<u32>, GlobalSequenceTracker>,
    control_metadata: ControlMetadataSnapshot,
    security_drops: SecurityDropSnapshot,
    tx_timing_samples: Vec<DataTrafficTimingSample>,
    rx_timing_samples: Vec<DataTrafficTimingSample>,
}

impl Default for DataplaneMetrics {
    fn default() -> Self {
        Self {
            started_at: Instant::now(),
            services: BTreeMap::new(),
            paths: BTreeMap::new(),
            service_paths: BTreeMap::new(),
            receive_sequences: BTreeMap::new(),
            control_metadata: ControlMetadataSnapshot::default(),
            security_drops: SecurityDropSnapshot::default(),
            tx_timing_samples: Vec::new(),
            rx_timing_samples: Vec::new(),
        }
    }
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
        self.record_forward_parts(&outcome.service, outcome.path_id, outcome.payload_len);
    }

    /// Record one forwarded datagram without forcing the hot path to allocate an outcome object.
    pub fn record_forward_parts(&mut self, service_name: &str, path_id: u16, payload_len: usize) {
        let bytes = payload_len as u64;
        self.record_forward_batch(service_name, path_id, 1, bytes);
    }

    /// Record already-aggregated forwarded datagrams from the hot summary path.
    pub fn record_forward_batch(&mut self, service_name: &str, path_id: u16, packets: u64, bytes: u64) {
        let now_us = self.elapsed_us();
        let service = self.services.entry(service_name.to_owned()).or_default();
        service.packets += packets;
        service.bytes += bytes;
        service.tx_packets += packets;
        service.tx_bytes += bytes;
        record_tx_timing(service, now_us);

        let path = self.paths.entry(path_id).or_default();
        path.packets += packets;
        path.bytes += bytes;
        path.tx_packets += packets;
        path.tx_bytes += bytes;
        record_tx_timing(path, now_us);

        let service_path = self.service_path_entry(service_name, path_id);
        service_path.packets += packets;
        service_path.bytes += bytes;
        service_path.tx_packets += packets;
        service_path.tx_bytes += bytes;
        record_tx_timing(service_path, now_us);
    }

    /// Record a sparse real-data transmit sample keyed by existing frame metadata.
    pub fn record_transmit_timing_sample(
        &mut self,
        path_id: u16,
        sequence: u64,
        packet_count: usize,
        peer_scope: Option<u32>,
    ) {
        let Some(packet_count) = bounded_packet_count(packet_count) else {
            return;
        };
        let Some(sample_sequence) = sampled_sequence_in_batch(sequence, packet_count) else {
            return;
        };
        let sample = DataTrafficTimingSample {
            path_id,
            sequence: sample_sequence,
            packet_count: 1,
            observed_at_us: monotonic_clock_us(),
            peer_scope,
        };
        push_bounded_sample(&mut self.tx_timing_samples, sample);
    }

    /// Record one received Gatherlink data sequence before emitting the virtual UDP payload.
    ///
    /// This uses the protocol's global sequence tracker so normal traffic, not
    /// test payloads, drives missing and reorder counters.
    pub fn record_receive(
        &mut self,
        _service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload_len: usize,
        peer_scope: Option<u32>,
    ) -> SequenceObservation {
        let observation = self.observe_sequence(sequence, peer_scope);
        let now_us = self.elapsed_us();
        let path = self.paths.entry(path_id).or_default();
        path.packets += 1;
        path.bytes += payload_len as u64;
        path.rx_packets += 1;
        path.rx_bytes += payload_len as u64;
        record_rx_timing(path, now_us);
        if observation.out_of_order {
            path.reordered_packets += 1;
            path.packets_needing_reorder += 1;
        } else {
            // A forward sequence gap is not loss yet. In multipath receive, the
            // skipped sequence may still arrive on another path. Python should
            // treat this as reorder pressure until a later timeout/expiry
            // mechanism promotes it to confirmed loss.
            path.packets_needing_reorder += observation.missing_packets;
        }
        if should_sample_sequence(sequence) {
            let sample = DataTrafficTimingSample {
                path_id,
                sequence,
                packet_count: 1,
                observed_at_us: monotonic_clock_us(),
                peer_scope,
            };
            push_bounded_sample(&mut self.rx_timing_samples, sample);
        }
        observation
    }

    /// Record one received Gatherlink data sequence and the local service it was emitted to.
    pub fn record_receive_for_service(
        &mut self,
        service_name: &str,
        _service_id: ServiceId,
        path_id: u16,
        _sequence: u64,
        payload_len: usize,
        observation: SequenceObservation,
    ) {
        let now_us = self.elapsed_us();
        {
            let service = self.services.entry(service_name.to_owned()).or_default();
            service.packets += 1;
            service.bytes += payload_len as u64;
            service.rx_packets += 1;
            service.rx_bytes += payload_len as u64;
            record_rx_timing(service, now_us);
            if observation.out_of_order {
                service.reordered_packets += 1;
                service.packets_needing_reorder += 1;
            } else {
                // Keep service-level loss conservative for the same reason as path
                // loss: a sequence gap is only a reorder candidate until expiry.
                service.packets_needing_reorder += observation.missing_packets;
            }
        }
        let service_path = self.service_path_entry(service_name, path_id);
        service_path.packets += 1;
        service_path.bytes += payload_len as u64;
        service_path.rx_packets += 1;
        service_path.rx_bytes += payload_len as u64;
        record_rx_timing(service_path, now_us);
        if observation.out_of_order {
            service_path.reordered_packets += 1;
            service_path.packets_needing_reorder += 1;
        } else {
            service_path.packets_needing_reorder += observation.missing_packets;
        }
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
        let service = self.services.entry(service_key.clone()).or_default();
        if expected {
            service.expected_duplicate_packets += 1;
        } else {
            service.unexpected_duplicate_packets += 1;
            service.duplicate_packets += 1;
        }
        let service_path = self.service_path_entry(&service_key, path_id);
        service_path.rx_packets += 1;
        service_path.rx_bytes += payload_len as u64;
        if expected {
            service_path.expected_duplicate_packets += 1;
        } else {
            service_path.unexpected_duplicate_packets += 1;
            service_path.duplicate_packets += 1;
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
    pub fn record_reserved_received(
        &mut self,
        path_id: u16,
        frame_bytes: usize,
        sequence: u64,
        peer_scope: Option<u32>,
    ) {
        let _observation = self.observe_sequence(sequence, peer_scope);
        let now_us = self.elapsed_us();
        record_control_counter(&mut self.control_metadata.received, frame_bytes, now_us);
        let path = self.control_metadata.path_control.entry(path_id).or_default();
        record_control_counter(&mut path.rx, frame_bytes, now_us);
    }

    /// Record one sent reserved-service frame without decoding its payload.
    pub fn record_reserved_sent(&mut self, path_id: u16, frame_bytes: usize) {
        let now_us = self.elapsed_us();
        record_control_counter(&mut self.control_metadata.sent, frame_bytes, now_us);
        let path = self.control_metadata.path_control.entry(path_id).or_default();
        record_control_counter(&mut path.tx, frame_bytes, now_us);
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
            service_paths: self.service_paths.clone(),
            control_metadata: self.control_metadata.clone(),
            security_drops: self.security_drops,
        }
    }

    /// Drain sparse real-data timing samples so Python can advertise or match them once.
    pub fn drain_data_timing_samples(&mut self) -> (Vec<DataTrafficTimingSample>, Vec<DataTrafficTimingSample>) {
        let tx = std::mem::take(&mut self.tx_timing_samples);
        let rx = std::mem::take(&mut self.rx_timing_samples);
        (tx, rx)
    }

    fn observe_sequence(&mut self, sequence: u64, peer_scope: Option<u32>) -> SequenceObservation {
        self.receive_sequences.entry(peer_scope).or_default().observe(sequence)
    }

    fn elapsed_us(&self) -> u64 {
        self.started_at.elapsed().as_micros().min(u128::from(u64::MAX)) as u64
    }

    fn service_path_entry(&mut self, service_name: &str, path_id: u16) -> &mut CounterSnapshot {
        self.service_paths
            .entry(service_name.to_owned())
            .or_default()
            .entry(path_id)
            .or_default()
    }
}

fn should_sample_sequence(sequence: u64) -> bool {
    sequence == 1 || sequence % DATA_TIMING_SAMPLE_SEQUENCE_INTERVAL == 0
}

fn sampled_sequence_in_batch(first_sequence: u64, packet_count: u32) -> Option<u64> {
    if packet_count == 0 {
        return None;
    }
    if should_sample_sequence(first_sequence) {
        return Some(first_sequence);
    }
    let last_sequence = first_sequence.saturating_add(u64::from(packet_count).saturating_sub(1));
    let remainder = first_sequence % DATA_TIMING_SAMPLE_SEQUENCE_INTERVAL;
    let next_sample = first_sequence + (DATA_TIMING_SAMPLE_SEQUENCE_INTERVAL - remainder);
    if next_sample <= last_sequence {
        Some(next_sample)
    } else {
        None
    }
}

fn bounded_packet_count(packet_count: usize) -> Option<u32> {
    if packet_count == 0 {
        return None;
    }
    Some(packet_count.min(u32::MAX as usize) as u32)
}

fn monotonic_clock_us() -> u64 {
    let mut time_spec = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: `time_spec` is a valid mutable pointer and CLOCK_MONOTONIC does
    // not require additional invariants. If the syscall fails, returning zero
    // makes the Python parser ignore the sample rather than trusting bad time.
    let result = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut time_spec) };
    if result != 0 {
        return 0;
    }
    let seconds = u64::try_from(time_spec.tv_sec).unwrap_or(0);
    let nanos = u64::try_from(time_spec.tv_nsec).unwrap_or(0);
    seconds.saturating_mul(1_000_000).saturating_add(nanos / 1_000)
}

fn push_bounded_sample(samples: &mut Vec<DataTrafficTimingSample>, sample: DataTrafficTimingSample) {
    if samples.len() >= DATA_TIMING_SAMPLE_BUFFER_LIMIT {
        let overflow = samples.len() + 1 - DATA_TIMING_SAMPLE_BUFFER_LIMIT;
        samples.drain(0..overflow);
    }
    samples.push(sample);
}

fn record_tx_timing(counters: &mut CounterSnapshot, now_us: u64) {
    if counters.last_tx_at_us != 0 {
        counters.last_tx_gap_us = now_us.saturating_sub(counters.last_tx_at_us);
    }
    counters.last_tx_at_us = now_us;
}

fn record_rx_timing(counters: &mut CounterSnapshot, now_us: u64) {
    if counters.last_rx_at_us != 0 {
        counters.last_rx_gap_us = now_us.saturating_sub(counters.last_rx_at_us);
    }
    counters.last_rx_at_us = now_us;
}

fn record_control_counter(counters: &mut ControlCounterSnapshot, frame_bytes: usize, now_us: u64) {
    counters.frames += 1;
    counters.bytes += frame_bytes as u64;
    if counters.last_at_us > 0 {
        counters.last_gap_us = now_us.saturating_sub(counters.last_at_us);
    }
    counters.last_at_us = now_us;
}
