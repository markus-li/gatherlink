//! Compiled scheduler execution selected by the Python control plane.
//!
//! Rust only applies cheap packet-time primitives here: eligibility, MTU fit,
//! loss/capacity/latency ordering, and the already-compiled weights. Policy and
//! smoothing remain in Python so hot reapply can change behavior without moving
//! product logic into the dataplane.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use crate::runtime_config::{CorePathConfig, PathSchedulerPrimitives, SchedulerMode};
use crate::scheduler::weighted_rr::WeightedRoundRobin;

const UNKNOWN_ORDERED_LATENCY_US: u64 = 50_000;
const ORDERED_PACING_MAX_SLEEP_US: u64 = 250;
const ORDERED_EARLY_ARRIVAL_SOFT_PENALTY_MULTIPLIER: u64 = 16;

/// Packet-time scheduler compiled from Python-selected mode and path state.
#[derive(Debug, Clone)]
pub struct CompiledScheduler {
    mode: SchedulerMode,
    weighted: WeightedRoundRobin,
    ordered_multipath: OrderedMultipathScheduler,
}

/// Cheap per-path runtime facts from scheduler execution.
///
/// This intentionally reports only what the compiled executor currently has
/// queued or predicted. Python turns these facts into pressure, policy, and
/// operator explanations.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SchedulerPathRuntimeSnapshot {
    pub in_flight_packets: u64,
    pub in_flight_bytes: u64,
    pub predicted_delivery_us: u64,
}

impl CompiledScheduler {
    /// Build the executor for the current path table.
    pub fn compile(mode: SchedulerMode, paths: &[CorePathConfig]) -> Self {
        Self {
            mode,
            weighted: WeightedRoundRobin::compile(paths),
            ordered_multipath: OrderedMultipathScheduler::compile(paths.len()),
        }
    }

    /// Select a path index for the next payload.
    pub fn select_path_index(&mut self, paths: &[CorePathConfig], payload_len: usize) -> Option<usize> {
        self.select_path_index_where(paths, payload_len, |_| true)
    }

    /// Select a path index while applying a caller-owned eligibility predicate.
    ///
    /// Python may constrain a service to a proven path subset while still
    /// asking Rust to execute the node scheduler. The predicate is only a
    /// primitive eligibility check; service meaning and policy remain in
    /// Python.
    pub fn select_path_index_where(
        &mut self,
        paths: &[CorePathConfig],
        payload_len: usize,
        allowed: impl Fn(&CorePathConfig) -> bool,
    ) -> Option<usize> {
        match self.mode {
            SchedulerMode::RoundRobin | SchedulerMode::WeightedRoundRobin | SchedulerMode::Adaptive => {
                self.weighted.select_path_index_where(paths, payload_len, allowed)
            }
            SchedulerMode::LowestLatency => self.select_ranked(paths, payload_len, latency_score, allowed),
            SchedulerMode::LossAware => self.select_ranked(paths, payload_len, loss_score, allowed),
            SchedulerMode::CapacityAware => self.select_ranked(paths, payload_len, capacity_score, allowed),
            SchedulerMode::LeastQueue => self.select_ranked(paths, payload_len, least_queue_score, allowed),
            SchedulerMode::EarliestCompletionFirst => self.select_ranked(paths, payload_len, completion_score, allowed),
            SchedulerMode::BlockingEstimation => {
                self.select_ranked(paths, payload_len, blocking_estimation_score, allowed)
            }
            SchedulerMode::OrderedMultipath => {
                self.ordered_multipath
                    .select_path_index_where(paths, payload_len, allowed)
            }
            SchedulerMode::Balanced => self.select_ranked(paths, payload_len, balanced_score, allowed),
        }
    }

    /// Return whether this scheduler needs per-datagram path decisions.
    pub fn requires_per_datagram_decisions(&self) -> bool {
        matches!(self.mode, SchedulerMode::OrderedMultipath)
    }

    /// Return whether burst execution may keep a path even when frames cannot coalesce.
    ///
    /// Plain round robin can use this as a cheap syscall-reduction primitive.
    /// Weighted/capacity policies need fresh packet-time decisions so high-MTU
    /// bursts do not sit on one path long enough to overflow shaped links.
    pub fn allows_noncoalesced_burst_reuse(&self) -> bool {
        matches!(self.mode, SchedulerMode::RoundRobin | SchedulerMode::Adaptive)
    }

    /// Return current path-local scheduler execution facts.
    pub fn path_runtime_snapshots(&self, paths: &[CorePathConfig]) -> Vec<(u16, SchedulerPathRuntimeSnapshot)> {
        match self.mode {
            SchedulerMode::OrderedMultipath => self.ordered_multipath.path_runtime_snapshots(paths),
            _ => paths
                .iter()
                .map(|path| (path.path_id(), SchedulerPathRuntimeSnapshot::default()))
                .collect(),
        }
    }

    fn select_ranked(
        &self,
        paths: &[CorePathConfig],
        payload_len: usize,
        score: fn(&CorePathConfig, usize) -> (u64, u64, u64),
        allowed: impl Fn(&CorePathConfig) -> bool,
    ) -> Option<usize> {
        let whole_packet = paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| {
                allowed(path) && path.accepts_whole_packet() && payload_len <= path.max_data_payload()
            })
            .min_by_key(|(index, path)| {
                let (primary, secondary, tertiary) = score(path, payload_len);
                (primary, secondary, tertiary, *index)
            })
            .map(|(index, _path)| index);
        if whole_packet.is_some() {
            return whole_packet;
        }

        paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| allowed(path) && path.accepts_fragmented_packet())
            .min_by_key(|(index, path)| {
                let (primary, secondary, tertiary) = score(path, payload_len);
                (primary, secondary, tertiary, *index)
            })
            .map(|(index, _path)| index)
    }
}

/// MPTCP-inspired ordered service-flow scheduler.
///
/// Python owns the name, policy, and smoothed path facts. Rust keeps only this
/// tiny virtual send timeline so consecutive service-flow sequence numbers are
/// assigned to paths by predicted arrival instead of blind round robin.
#[derive(Debug, Clone)]
struct OrderedMultipathScheduler {
    started_at: Instant,
    path_available_at_us: Vec<u64>,
    in_flight: Vec<VecDeque<OrderedInFlightPacket>>,
    last_ordered_arrival_us: u64,
    next_tie_start_index: usize,
}

impl OrderedMultipathScheduler {
    fn compile(path_count: usize) -> Self {
        Self {
            started_at: Instant::now(),
            path_available_at_us: vec![0; path_count],
            in_flight: vec![VecDeque::new(); path_count],
            last_ordered_arrival_us: 0,
            next_tie_start_index: 0,
        }
    }

    fn select_path_index_where(
        &mut self,
        paths: &[CorePathConfig],
        payload_len: usize,
        allowed: impl Fn(&CorePathConfig) -> bool,
    ) -> Option<usize> {
        if self.path_available_at_us.len() != paths.len() {
            self.path_available_at_us.resize(paths.len(), 0);
        }
        if self.in_flight.len() != paths.len() {
            self.in_flight.resize_with(paths.len(), VecDeque::new);
        }

        let now_us = self.now_us();
        self.drain_completed(now_us);
        self.normalize_idle_timeline(now_us);
        let selected = self
            .select_ordered_whole_packet(paths, payload_len, now_us, &allowed)
            .or_else(|| self.select_ordered_fragment_path(paths, payload_len, now_us, &allowed))?;
        let prediction = self.predict(paths[selected].primitives(), payload_len, selected, now_us);
        self.apply_bounded_pacing_delay(paths[selected].primitives(), prediction.path_ready_us, now_us);
        self.path_available_at_us[selected] = prediction.path_available_after_send_us;
        self.in_flight[selected].push_back(OrderedInFlightPacket {
            predicted_arrival_us: prediction.predicted_arrival_us,
            bytes: payload_len,
        });
        self.next_tie_start_index = if paths.is_empty() {
            0
        } else {
            (selected + 1) % paths.len()
        };
        self.last_ordered_arrival_us = self
            .last_ordered_arrival_us
            .saturating_add(1)
            .max(prediction.predicted_arrival_us);
        Some(selected)
    }

    fn select_ordered_whole_packet(
        &self,
        paths: &[CorePathConfig],
        payload_len: usize,
        now_us: u64,
        allowed: &impl Fn(&CorePathConfig) -> bool,
    ) -> Option<usize> {
        let has_available_credit = paths.iter().enumerate().any(|(index, path)| {
            allowed(path)
                && path.accepts_whole_packet()
                && payload_len <= path.max_data_payload()
                && self.in_flight_within_limits(path.primitives(), payload_len, index)
        });
        paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| {
                allowed(path) && path.accepts_whole_packet() && payload_len <= path.max_data_payload()
            })
            .filter(|(index, path)| {
                !has_available_credit || self.in_flight_within_limits(path.primitives(), payload_len, *index)
            })
            .min_by_key(|(index, path)| self.ordered_score(path, payload_len, *index, now_us))
            .map(|(index, _path)| index)
    }

    fn select_ordered_fragment_path(
        &self,
        paths: &[CorePathConfig],
        payload_len: usize,
        now_us: u64,
        allowed: &impl Fn(&CorePathConfig) -> bool,
    ) -> Option<usize> {
        let has_available_credit = paths.iter().enumerate().any(|(index, path)| {
            allowed(path)
                && path.accepts_fragmented_packet()
                && self.in_flight_within_limits(path.primitives(), payload_len, index)
        });
        paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| allowed(path) && path.accepts_fragmented_packet())
            .filter(|(index, path)| {
                !has_available_credit || self.in_flight_within_limits(path.primitives(), payload_len, *index)
            })
            .min_by_key(|(index, path)| self.ordered_score(path, payload_len, *index, now_us))
            .map(|(index, _path)| index)
    }

    fn ordered_score(
        &self,
        path: &CorePathConfig,
        payload_len: usize,
        index: usize,
        now_us: u64,
    ) -> (u64, u64, u64, u64, usize) {
        let prediction = self.predict(path.primitives(), payload_len, index, now_us);
        let reorder_hold_us = path.primitives().reorder_hold_us() as u64;
        let early_inside_hold_us = if self.last_ordered_arrival_us == 0 {
            0
        } else {
            self.last_ordered_arrival_us
                .saturating_sub(prediction.predicted_arrival_us)
        };
        let early_arrival_us = if self.last_ordered_arrival_us == 0 || reorder_hold_us == 0 {
            0
        } else {
            self.last_ordered_arrival_us
                .saturating_sub(prediction.predicted_arrival_us)
                .saturating_sub(reorder_hold_us)
        };
        let late_arrival_us = if self.last_ordered_arrival_us == 0 || reorder_hold_us == 0 {
            0
        } else {
            prediction
                .predicted_arrival_us
                .saturating_sub(self.last_ordered_arrival_us.saturating_add(reorder_hold_us))
        };
        let head_of_line_penalty_us = early_arrival_us.saturating_mul(1_000_000);
        let early_smoothing_penalty_us =
            early_inside_hold_us.saturating_mul(ORDERED_EARLY_ARRIVAL_SOFT_PENALTY_MULTIPLIER);
        let late_reorder_penalty_us = late_arrival_us.saturating_mul(1_000_000);
        let in_flight_penalty_us = self.in_flight_penalty_us(path.primitives(), payload_len, index);
        (
            prediction
                .predicted_arrival_us
                .saturating_add(early_smoothing_penalty_us)
                .saturating_add(head_of_line_penalty_us)
                .saturating_add(late_reorder_penalty_us)
                .saturating_add(in_flight_penalty_us)
                .saturating_add(path.primitives().loss_ppm() as u64 * 100),
            prediction.path_available_after_send_us,
            self.path_available_at_us.get(index).copied().unwrap_or(now_us),
            inverse_capacity(path),
            self.tie_rotation_distance(index),
        )
    }

    fn tie_rotation_distance(&self, index: usize) -> usize {
        if self.path_available_at_us.is_empty() {
            return index;
        }
        (index + self.path_available_at_us.len() - self.next_tie_start_index) % self.path_available_at_us.len()
    }

    fn predict(
        &self,
        primitives: PathSchedulerPrimitives,
        payload_len: usize,
        index: usize,
        now_us: u64,
    ) -> OrderedPathPrediction {
        let capacity_bps = primitives.tx_capacity_bps().unwrap_or(1).max(1);
        let pacing_budget_bps = primitives.pacing_budget_bps();
        let transmit_us = transmit_time_us(payload_len, capacity_bps);
        let paced_transmit_us = if pacing_budget_bps == 0 {
            transmit_us
        } else {
            transmit_us.max(transmit_time_us(payload_len, pacing_budget_bps.max(1)))
        };
        let queued_transmit_us = transmit_time_us(primitives.queue_depth_bytes() as usize, capacity_bps);
        let queue_delay_us = primitives
            .queue_oldest_age_us()
            .saturating_add(queued_transmit_us)
            .saturating_add(primitives.queue_depth_packets() as u64);
        let latency_us = primitives
            .latency_us()
            .map(u64::from)
            .unwrap_or(UNKNOWN_ORDERED_LATENCY_US);
        let readiness_floor_us = self
            .last_ordered_arrival_us
            .saturating_sub(latency_us)
            .saturating_sub(u64::from(primitives.reorder_hold_us()));
        let path_ready_us = self
            .path_available_at_us
            .get(index)
            .copied()
            .unwrap_or(now_us)
            .max(readiness_floor_us)
            .saturating_add(queue_delay_us);
        OrderedPathPrediction {
            path_ready_us,
            predicted_arrival_us: path_ready_us
                .saturating_add(paced_transmit_us)
                .saturating_add(latency_us),
            path_available_after_send_us: path_ready_us.saturating_add(paced_transmit_us),
        }
    }

    fn now_us(&self) -> u64 {
        self.started_at.elapsed().as_micros().min(u128::from(u64::MAX)) as u64
    }

    fn drain_completed(&mut self, now_us: u64) {
        for path_queue in &mut self.in_flight {
            while path_queue
                .front()
                .is_some_and(|packet| packet.predicted_arrival_us <= now_us)
            {
                path_queue.pop_front();
            }
        }
    }

    fn normalize_idle_timeline(&mut self, now_us: u64) {
        if self.in_flight.iter().any(|path_queue| !path_queue.is_empty()) {
            return;
        }
        let Some(max_available_us) = self.path_available_at_us.iter().copied().max() else {
            return;
        };
        if max_available_us >= now_us {
            return;
        }
        let shift_us = now_us - max_available_us;
        for available_at_us in &mut self.path_available_at_us {
            *available_at_us = available_at_us.saturating_add(shift_us);
        }
        // With no packets in flight, the receiver cannot still be blocked by
        // the previous ordered horizon. Reset it so sparse TCP-like bursts do
        // not pin every later packet to the first equal path.
        self.last_ordered_arrival_us = 0;
    }

    fn apply_bounded_pacing_delay(&self, primitives: PathSchedulerPrimitives, path_ready_us: u64, now_us: u64) {
        if primitives.pacing_budget_bps() == 0 {
            return;
        }
        let delay_us = path_ready_us.saturating_sub(now_us).min(ORDERED_PACING_MAX_SLEEP_US);
        if delay_us > 0 {
            std::thread::sleep(Duration::from_micros(delay_us));
        }
    }

    fn in_flight_penalty_us(&self, primitives: PathSchedulerPrimitives, payload_len: usize, index: usize) -> u64 {
        let (packet_overflow, byte_overflow) = self.in_flight_overflow(primitives, payload_len, index);
        packet_overflow
            .saturating_mul(10_000_000)
            .saturating_add(byte_overflow.saturating_mul(1_000))
    }

    fn in_flight_within_limits(&self, primitives: PathSchedulerPrimitives, payload_len: usize, index: usize) -> bool {
        self.in_flight_overflow(primitives, payload_len, index) == (0, 0)
    }

    fn in_flight_overflow(&self, primitives: PathSchedulerPrimitives, payload_len: usize, index: usize) -> (u64, u64) {
        let Some(path_queue) = self.in_flight.get(index) else {
            return (0, 0);
        };
        let packet_limit = primitives.max_in_flight_packets();
        let byte_limit = primitives.max_in_flight_bytes();
        let pending_packets = path_queue.len().saturating_add(1);
        let pending_bytes = path_queue.iter().fold(payload_len as u64, |total, packet| {
            total.saturating_add(packet.bytes as u64)
        });

        let packet_overflow = if packet_limit == 0 {
            0
        } else {
            pending_packets.saturating_sub(usize::from(packet_limit)) as u64
        };
        let byte_overflow = if byte_limit == 0 {
            0
        } else {
            pending_bytes.saturating_sub(u64::from(byte_limit))
        };
        (packet_overflow, byte_overflow)
    }

    fn path_runtime_snapshots(&self, paths: &[CorePathConfig]) -> Vec<(u16, SchedulerPathRuntimeSnapshot)> {
        let now_us = self.now_us();
        paths
            .iter()
            .enumerate()
            .map(|(index, path)| {
                let in_flight = self.in_flight.get(index);
                let in_flight_packets = in_flight.map_or(0, VecDeque::len) as u64;
                let in_flight_bytes = in_flight
                    .map(|queue| {
                        queue
                            .iter()
                            .fold(0_u64, |total, packet| total.saturating_add(packet.bytes as u64))
                    })
                    .unwrap_or(0);
                let predicted_delivery_us = self
                    .predict(path.primitives(), 0, index, now_us)
                    .predicted_arrival_us
                    .saturating_sub(now_us);
                (
                    path.path_id(),
                    SchedulerPathRuntimeSnapshot {
                        in_flight_packets,
                        in_flight_bytes,
                        predicted_delivery_us,
                    },
                )
            })
            .collect()
    }
}

#[derive(Debug, Copy, Clone)]
struct OrderedPathPrediction {
    path_ready_us: u64,
    predicted_arrival_us: u64,
    path_available_after_send_us: u64,
}

#[derive(Debug, Copy, Clone)]
struct OrderedInFlightPacket {
    predicted_arrival_us: u64,
    bytes: usize,
}

fn latency_score(path: &CorePathConfig, _payload_len: usize) -> (u64, u64, u64) {
    (
        path.primitives().latency_us().unwrap_or(u32::MAX) as u64,
        path.primitives().loss_ppm() as u64,
        inverse_capacity(path),
    )
}

fn loss_score(path: &CorePathConfig, _payload_len: usize) -> (u64, u64, u64) {
    (
        path.primitives().loss_ppm() as u64,
        path.primitives().latency_us().unwrap_or(u32::MAX) as u64,
        inverse_capacity(path),
    )
}

fn capacity_score(path: &CorePathConfig, _payload_len: usize) -> (u64, u64, u64) {
    (
        u64::MAX - path.primitives().tx_capacity_bps().unwrap_or(0),
        path.primitives().latency_us().unwrap_or(u32::MAX) as u64,
        path.primitives().loss_ppm() as u64,
    )
}

fn least_queue_score(path: &CorePathConfig, _payload_len: usize) -> (u64, u64, u64) {
    (
        path.primitives().queue_depth_packets() as u64,
        path.primitives().queue_depth_bytes() as u64,
        path.primitives()
            .queue_oldest_age_us()
            .saturating_add(latency_score(path, _payload_len).0),
    )
}

fn completion_score(path: &CorePathConfig, payload_len: usize) -> (u64, u64, u64) {
    let capacity_bps = path.primitives().tx_capacity_bps().unwrap_or(1);
    let transmit_us = transmit_time_us(payload_len, capacity_bps.max(1));
    let latency_us = path.primitives().latency_us().unwrap_or(u32::MAX) as u64;
    (
        latency_us.saturating_add(transmit_us),
        path.primitives().loss_ppm() as u64,
        inverse_capacity(path),
    )
}

fn blocking_estimation_score(path: &CorePathConfig, payload_len: usize) -> (u64, u64, u64) {
    let (completion_us, loss_ppm, inverse_capacity) = completion_score(path, payload_len);
    let reorder_hold_us = path.primitives().reorder_hold_us() as u64;
    (
        completion_us.saturating_add(reorder_hold_us),
        loss_ppm,
        inverse_capacity,
    )
}

fn balanced_score(path: &CorePathConfig, payload_len: usize) -> (u64, u64, u64) {
    let (completion_us, loss_ppm, inverse_capacity) = completion_score(path, payload_len);
    let latency_us = path.primitives().latency_us().unwrap_or(u32::MAX) as u64;
    (
        completion_us
            .saturating_add(loss_ppm.saturating_mul(100))
            .saturating_add(latency_us / 4),
        inverse_capacity,
        loss_ppm,
    )
}

fn inverse_capacity(path: &CorePathConfig) -> u64 {
    u64::MAX - path.primitives().tx_capacity_bps().unwrap_or(0)
}

fn transmit_time_us(payload_len: usize, capacity_bps: u64) -> u64 {
    (payload_len as u64)
        .saturating_mul(8)
        .saturating_mul(1_000_000)
        .saturating_div(capacity_bps.max(1))
}
