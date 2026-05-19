//! Deterministic weighted round-robin execution.
//!
//! The schedule is compiled outside the packet selection path. Rust still
//! applies MTU eligibility because it must not emit frames that cannot fit.

use crate::runtime_config::CorePathConfig;

/// Allocation-free packet-time weighted round-robin selector.
#[derive(Debug, Clone, Default)]
pub struct WeightedRoundRobin {
    slots: Vec<WeightedPathSlot>,
    next_slot: usize,
    current_remaining: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct WeightedPathSlot {
    path_index: usize,
    weight: u16,
}

impl WeightedRoundRobin {
    /// Compile enabled paths into compact weighted slots.
    pub fn compile(paths: &[CorePathConfig]) -> Self {
        let mut slots = Vec::new();
        for (index, path) in paths.iter().enumerate() {
            if !path.enabled() {
                continue;
            }
            slots.push(WeightedPathSlot {
                path_index: index,
                weight: path.weight(),
            });
        }

        Self {
            slots,
            next_slot: 0,
            current_remaining: 0,
        }
    }

    /// Select a path index for a payload of `payload_len` bytes.
    pub fn select_path_index(&mut self, paths: &[CorePathConfig], payload_len: usize) -> Option<usize> {
        if self.slots.is_empty() {
            return None;
        }

        let start_slot = self.advance_and_return_start_slot();

        for offset in 0..self.slots.len() {
            let index = self.slots[(start_slot + offset) % self.slots.len()].path_index;
            let path = &paths[index];
            if path.accepts_whole_packet() && payload_len <= path.max_data_payload() {
                return Some(index);
            }
        }

        for offset in 0..self.slots.len() {
            let index = self.slots[(start_slot + offset) % self.slots.len()].path_index;
            if paths[index].accepts_fragmented_packet() {
                return Some(index);
            }
        }

        for offset in 0..self.slots.len() {
            let index = self.slots[(start_slot + offset) % self.slots.len()].path_index;
            let path = &paths[index];
            if path.enabled() && payload_len <= path.max_data_payload() {
                return Some(index);
            }
        }

        paths
            .iter()
            .enumerate()
            .find(|(_index, path)| path.enabled())
            .map(|(index, _path)| index)
    }

    fn advance_and_return_start_slot(&mut self) -> usize {
        let start_slot = self.next_slot % self.slots.len();
        if self.current_remaining == 0 {
            self.current_remaining = self.slots[start_slot].weight.saturating_sub(1);
        } else {
            self.current_remaining -= 1;
        }
        if self.current_remaining == 0 {
            self.next_slot = (start_slot + 1) % self.slots.len();
        }
        start_slot
    }
}
