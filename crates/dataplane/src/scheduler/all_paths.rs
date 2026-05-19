//! Eligible-path selector for service fanout.
//!
//! Python decides which service needs fanout and how much. Rust only executes
//! the compiled primitive: choose enabled paths that can carry the payload.

use crate::runtime_config::CorePathConfig;

/// Stateless selector for fanout-eligible paths.
#[derive(Debug, Clone, Copy, Default)]
pub struct AllPathSelector;

impl AllPathSelector {
    /// Return every path index eligible for this payload.
    ///
    /// Whole-packet paths are preferred because they avoid fragment overhead.
    /// If none can carry the payload whole, Rust may use Python-enabled
    /// fragment-capable paths and let the fragmentation executor do its small,
    /// deterministic work.
    pub fn select_path_indices(paths: &[CorePathConfig], payload_len: usize) -> Vec<usize> {
        let whole_packet: Vec<usize> = paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| path.enabled() && payload_len <= path.max_data_payload())
            .map(|(index, _path)| index)
            .collect();
        if !whole_packet.is_empty() {
            return whole_packet;
        }

        paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| path.accepts_fragmented_packet())
            .map(|(index, _path)| index)
            .collect()
    }
}
