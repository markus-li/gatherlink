//! Deterministic weighted round-robin execution.
//!
//! The schedule is compiled outside the packet selection path. Rust still
//! applies MTU eligibility because it must not emit frames that cannot fit.

use crate::runtime_config::CorePathConfig;

/// Allocation-free packet-time weighted round-robin selector.
#[derive(Debug, Clone, Default)]
pub struct WeightedRoundRobin {
    slots: Vec<WeightedPathSlot>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
struct WeightedPathSlot {
    path_index: usize,
    weight: u16,
    current_weight: i64,
}

impl WeightedRoundRobin {
    /// Compile enabled paths into compact weighted slots.
    pub fn compile(paths: &[CorePathConfig]) -> Self {
        Self::compile_with_allowed_path_ids(paths, &[])
    }

    /// Compile enabled paths constrained by optional Python-selected path ids.
    pub fn compile_with_allowed_path_ids(paths: &[CorePathConfig], allowed_path_ids: &[u16]) -> Self {
        Self::compile_with_path_weights(paths, allowed_path_ids, &[])
    }

    /// Compile enabled paths constrained by optional ids and service-specific weights.
    ///
    /// When `path_weights` is non-empty, only the listed path ids receive slots.
    /// Python owns why a service gets these weights; Rust only executes the
    /// already-compiled per-service vector.
    pub fn compile_with_path_weights(
        paths: &[CorePathConfig],
        allowed_path_ids: &[u16],
        path_weights: &[(u16, u16)],
    ) -> Self {
        let mut slots = Vec::new();
        for (index, path) in paths.iter().enumerate() {
            if !path.enabled() {
                continue;
            }
            if !allowed_path_ids.is_empty() && !allowed_path_ids.contains(&path.path_id()) {
                continue;
            }
            let Some(weight) = service_weight_for_path(path.path_id(), path.weight(), path_weights) else {
                continue;
            };
            slots.push(WeightedPathSlot {
                path_index: index,
                weight,
                current_weight: 0,
            });
        }

        Self { slots }
    }

    /// Select a path index for a payload of `payload_len` bytes.
    pub fn select_path_index(&mut self, paths: &[CorePathConfig], payload_len: usize) -> Option<usize> {
        self.select_path_index_where(paths, payload_len, |_| true)
    }

    /// Select a path index while applying an extra caller-owned eligibility predicate.
    ///
    /// Service return traffic may need a peer/session-specific learned remote
    /// address in addition to the Python-compiled service weights. The
    /// predicate keeps that transport fact in Rust without adding policy
    /// meaning to the scheduler.
    pub fn select_path_index_where(
        &mut self,
        paths: &[CorePathConfig],
        payload_len: usize,
        allowed: impl Fn(&CorePathConfig) -> bool,
    ) -> Option<usize> {
        if self.slots.is_empty() {
            return None;
        }

        if let Some(index) = self.select_eligible(paths, payload_len, |path, payload_len| {
            allowed(path) && path.accepts_whole_packet() && payload_len <= path.max_data_payload()
        }) {
            return Some(index);
        }

        if let Some(index) = self.select_eligible(paths, payload_len, |path, _payload_len| {
            allowed(path) && path.accepts_fragmented_packet()
        }) {
            return Some(index);
        }

        if let Some(index) = self.select_eligible(paths, payload_len, |path, payload_len| {
            allowed(path) && path.enabled() && payload_len <= path.max_data_payload()
        }) {
            return Some(index);
        }

        paths
            .iter()
            .enumerate()
            .find(|(_index, path)| allowed(path) && path.enabled())
            .map(|(index, _path)| index)
    }

    fn select_eligible(
        &mut self,
        paths: &[CorePathConfig],
        payload_len: usize,
        eligible: impl Fn(&CorePathConfig, usize) -> bool,
    ) -> Option<usize> {
        let mut total_weight = 0_i64;
        let mut best_slot = None;
        let mut best_current_weight = i64::MIN;
        let mut best_path_index = usize::MAX;

        for (slot_index, slot) in self.slots.iter_mut().enumerate() {
            let path = &paths[slot.path_index];
            if !eligible(path, payload_len) {
                continue;
            }
            total_weight += i64::from(slot.weight);
            slot.current_weight += i64::from(slot.weight);
            if slot.current_weight > best_current_weight
                || (slot.current_weight == best_current_weight && slot.path_index < best_path_index)
            {
                best_slot = Some(slot_index);
                best_current_weight = slot.current_weight;
                best_path_index = slot.path_index;
            }
        }

        let slot_index = best_slot?;
        self.slots[slot_index].current_weight -= total_weight;
        Some(self.slots[slot_index].path_index)
    }
}

fn service_weight_for_path(path_id: u16, default_weight: u16, path_weights: &[(u16, u16)]) -> Option<u16> {
    if path_weights.is_empty() {
        return Some(default_weight);
    }
    path_weights
        .iter()
        .find(|(weighted_path_id, weight)| *weighted_path_id == path_id && *weight > 0)
        .map(|(_path_id, weight)| *weight)
}
