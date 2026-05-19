//! Dataplane telemetry counters.
//!
//! These structs deliberately stay boring: they count what Rust actually did
//! or observed, and leave interpretation to Python. That keeps monitoring,
//! scheduling decisions, labels, alerts, and policy out of the fast path while
//! still making real-world service behavior visible through one status shape.

use std::collections::BTreeMap;

use gatherlink_protocol::control::{ControlMessage, ControlPayload, GlobalSequenceTracker};

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
}

/// Control metaband frame and message counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ControlCounterSnapshot {
    pub frames: u64,
    pub messages: u64,
    pub bytes: u64,
}

/// Per-path control metaband counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PathControlSnapshot {
    pub tx: ControlCounterSnapshot,
    pub rx: ControlCounterSnapshot,
}

/// Directional capacity facts exactly as they appeared in control metadata.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PathCapacitySnapshot {
    pub tx_bps: Option<u64>,
    pub rx_bps: Option<u64>,
}

/// Directional path latency facts from control metadata, in microseconds.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PathLatencySnapshot {
    pub tx_current_us: Option<u32>,
    pub tx_mean_us: Option<u32>,
    pub rx_current_us: Option<u32>,
    pub rx_mean_us: Option<u32>,
}

/// Last internal clock sync frame decoded from control metadata.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InternalClockSyncSnapshot {
    pub exchange_id: u64,
    pub path_id: u16,
    pub mode: u8,
    pub origin_us: u64,
    pub receive_us: Option<u64>,
    pub transmit_us: Option<u64>,
}

/// Last sink-authoritative wall-clock frame decoded from control metadata.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SinkTimeSnapshot {
    pub path_id: u16,
    pub sink_unix_us: u64,
    pub sink_internal_us: u64,
    pub ntp_state: u8,
}

/// Control metaband facts observed by the Rust dataplane.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ControlMetadataSnapshot {
    pub sent: ControlCounterSnapshot,
    pub received: ControlCounterSnapshot,
    pub path_control: BTreeMap<u16, PathControlSnapshot>,
    pub path_metadata: BTreeMap<u16, String>,
    pub path_capacity: BTreeMap<u16, PathCapacitySnapshot>,
    pub path_latency: BTreeMap<u16, PathLatencySnapshot>,
    pub internal_clock_sync: Option<InternalClockSyncSnapshot>,
    pub sink_time: Option<SinkTimeSnapshot>,
}

/// Full dataplane metrics snapshot.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MetricsSnapshot {
    pub services: BTreeMap<String, CounterSnapshot>,
    pub paths: BTreeMap<u16, CounterSnapshot>,
    pub control_metadata: ControlMetadataSnapshot,
}

/// Runtime counters owned by the Rust dataplane.
#[derive(Debug, Clone, Default)]
pub struct DataplaneMetrics {
    services: BTreeMap<String, CounterSnapshot>,
    paths: BTreeMap<u16, CounterSnapshot>,
    receive_sequences: BTreeMap<u64, GlobalSequenceTracker>,
    control_metadata: ControlMetadataSnapshot,
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
    /// The current Rust loopback engine does not yet receive frames from a remote peer, but this is the hook the
    /// real path receiver should call. It uses the protocol's global sequence tracker so normal traffic, not test
    /// payloads, drives missing and reorder counters.
    pub fn record_receive(&mut self, service_id: u64, path_id: u16, sequence: u64, payload_len: usize) {
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

    /// Record one received control metaband payload and remember known message facts.
    pub fn record_control_received(&mut self, payload: &ControlPayload, frame_bytes: usize) {
        self.control_metadata.received.frames += 1;
        self.control_metadata.received.messages += payload.messages.len() as u64;
        self.control_metadata.received.bytes += frame_bytes as u64;
        self.merge_control_payload(payload);
    }

    /// Record one received control payload on the path that carried it.
    pub fn record_control_received_on_path(&mut self, path_id: u16, payload: &ControlPayload, frame_bytes: usize) {
        self.record_control_received(payload, frame_bytes);
        let path = self.control_metadata.path_control.entry(path_id).or_default();
        path.rx.frames += 1;
        path.rx.messages += payload.messages.len() as u64;
        path.rx.bytes += frame_bytes as u64;
    }

    /// Record one sent control metaband payload and remember the facts Rust emitted.
    pub fn record_control_sent(&mut self, payload: &ControlPayload, frame_bytes: usize) {
        self.control_metadata.sent.frames += 1;
        self.control_metadata.sent.messages += payload.messages.len() as u64;
        self.control_metadata.sent.bytes += frame_bytes as u64;
        self.merge_control_payload(payload);
    }

    /// Record one sent control payload on the path that carried it.
    pub fn record_control_sent_on_path(&mut self, path_id: u16, payload: &ControlPayload, frame_bytes: usize) {
        self.record_control_sent(payload, frame_bytes);
        let path = self.control_metadata.path_control.entry(path_id).or_default();
        path.tx.frames += 1;
        path.tx.messages += payload.messages.len() as u64;
        path.tx.bytes += frame_bytes as u64;
    }

    /// Return an immutable snapshot suitable for Python IPC/status conversion.
    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            services: self.services.clone(),
            paths: self.paths.clone(),
            control_metadata: self.control_metadata.clone(),
        }
    }

    fn merge_control_payload(&mut self, payload: &ControlPayload) {
        for message in &payload.messages {
            match message {
                ControlMessage::PathMetadata(metadata) => {
                    self.control_metadata
                        .path_metadata
                        .insert(metadata.path_id, metadata.name.clone());
                }
                ControlMessage::PathCapacity(capacity) => {
                    self.control_metadata.path_capacity.insert(
                        capacity.path_id,
                        PathCapacitySnapshot {
                            tx_bps: capacity.tx_bps,
                            rx_bps: capacity.rx_bps,
                        },
                    );
                }
                ControlMessage::PathLatency(latency) => {
                    self.control_metadata.path_latency.insert(
                        latency.path_id,
                        PathLatencySnapshot {
                            tx_current_us: latency.tx_current_us,
                            tx_mean_us: latency.tx_mean_us,
                            rx_current_us: latency.rx_current_us,
                            rx_mean_us: latency.rx_mean_us,
                        },
                    );
                }
                ControlMessage::InternalClockSync(sync) => {
                    self.control_metadata.internal_clock_sync = Some(InternalClockSyncSnapshot {
                        exchange_id: sync.exchange_id,
                        path_id: sync.path_id,
                        mode: sync.mode as u8,
                        origin_us: sync.origin_us,
                        receive_us: sync.receive_us,
                        transmit_us: sync.transmit_us,
                    });
                }
                ControlMessage::SinkTime(sink_time) => {
                    self.control_metadata.sink_time = Some(SinkTimeSnapshot {
                        path_id: sink_time.path_id,
                        sink_unix_us: sink_time.sink_unix_us,
                        sink_internal_us: sink_time.sink_internal_us,
                        ntp_state: sink_time.ntp_state as u8,
                    });
                }
                ControlMessage::PathAssignment(_) | ControlMessage::MissingRange(_) => {}
            }
        }
    }
}
