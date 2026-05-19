//! Compiled scheduler execution selected by the Python control plane.
//!
//! Rust only applies cheap packet-time primitives here: eligibility, MTU fit,
//! loss/capacity/latency ordering, and the already-compiled weights. Policy and
//! smoothing remain in Python so hot reapply can change behavior without moving
//! product logic into the dataplane.

use crate::runtime_config::{CorePathConfig, SchedulerMode};
use crate::scheduler::weighted_rr::WeightedRoundRobin;

/// Packet-time scheduler compiled from Python-selected mode and path state.
#[derive(Debug, Clone)]
pub struct CompiledScheduler {
    mode: SchedulerMode,
    weighted: WeightedRoundRobin,
}

impl CompiledScheduler {
    /// Build the executor for the current path table.
    pub fn compile(mode: SchedulerMode, paths: &[CorePathConfig]) -> Self {
        Self {
            mode,
            weighted: WeightedRoundRobin::compile(paths),
        }
    }

    /// Select a path index for the next payload.
    pub fn select_path_index(&mut self, paths: &[CorePathConfig], payload_len: usize) -> Option<usize> {
        match self.mode {
            SchedulerMode::RoundRobin | SchedulerMode::WeightedRoundRobin | SchedulerMode::Adaptive => {
                self.weighted.select_path_index(paths, payload_len)
            }
            SchedulerMode::LowestLatency => self.select_ranked(paths, payload_len, latency_score),
            SchedulerMode::LossAware => self.select_ranked(paths, payload_len, loss_score),
            SchedulerMode::CapacityAware => self.select_ranked(paths, payload_len, capacity_score),
            SchedulerMode::LeastQueue => self.select_ranked(paths, payload_len, least_queue_score),
            SchedulerMode::EarliestCompletionFirst => self.select_ranked(paths, payload_len, completion_score),
            SchedulerMode::BlockingEstimation => self.select_ranked(paths, payload_len, blocking_estimation_score),
            SchedulerMode::Balanced => self.select_ranked(paths, payload_len, balanced_score),
        }
    }

    fn select_ranked(
        &self,
        paths: &[CorePathConfig],
        payload_len: usize,
        score: fn(&CorePathConfig, usize) -> (u64, u64, u64),
    ) -> Option<usize> {
        let whole_packet = paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| path.accepts_whole_packet() && payload_len <= path.max_data_payload())
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
            .filter(|(_index, path)| path.accepts_fragmented_packet())
            .min_by_key(|(index, path)| {
                let (primary, secondary, tertiary) = score(path, payload_len);
                (primary, secondary, tertiary, *index)
            })
            .map(|(index, _path)| index)
    }
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
    // TODO(queue-stats): Replace this limit-derived placeholder with live queue
    // depth and oldest-age counters once path queues are wired into async sends.
    // Until then, Python can mark a path busy/drain or reduce weight to steer
    // away from pressure, while Rust keeps this mode deterministic.
    (
        path.primitives().max_in_flight_packets() as u64,
        path.primitives().max_in_flight_bytes() as u64,
        latency_score(path, 0).0,
    )
}

fn completion_score(path: &CorePathConfig, payload_len: usize) -> (u64, u64, u64) {
    let capacity_bps = path.primitives().tx_capacity_bps().unwrap_or(1);
    let transmit_us = (payload_len as u64)
        .saturating_mul(8)
        .saturating_mul(1_000_000)
        .saturating_div(capacity_bps.max(1));
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
