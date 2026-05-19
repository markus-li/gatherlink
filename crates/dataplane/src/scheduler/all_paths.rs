//! All-path duplication scheduler for control metaband traffic.
//!
//! Python decides which traffic class needs duplication. Rust only executes the
//! compiled primitive: emit the same control payload, with the same sequence, on
//! every enabled path that can carry it.

use gatherlink_protocol::frame::V1_HEADER_LEN;

use crate::runtime_config::CorePathConfig;

/// Stateless selector for duplicated control packets.
#[derive(Debug, Clone, Copy, Default)]
pub struct AllPathSelector;

impl AllPathSelector {
    /// Return every enabled path index whose MTU can carry this payload.
    pub fn select_path_indices(paths: &[CorePathConfig], payload_len: usize) -> Vec<usize> {
        paths
            .iter()
            .enumerate()
            .filter(|(_index, path)| path.enabled() && V1_HEADER_LEN + payload_len <= path.mtu())
            .map(|(index, _path)| index)
            .collect()
    }
}
