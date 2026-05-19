//! Receiver-side packet dedupe.
//!
//! Rust keeps this deliberately mechanical: remember recent service/sequence
//! pairs and report whether a payload has already been emitted. Python decides
//! which services use fanout; this module only prevents duplicated user-service
//! frames from being delivered twice.

use std::collections::{HashSet, VecDeque};

use gatherlink_protocol::ids::ServiceId;

#[derive(Debug, Clone)]
pub struct DedupeWindow {
    capacity: usize,
    seen: HashSet<(ServiceId, Option<u32>, u64)>,
    order: VecDeque<(ServiceId, Option<u32>, u64)>,
}

impl Default for DedupeWindow {
    fn default() -> Self {
        Self::new(4096)
    }
}

impl DedupeWindow {
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity: capacity.max(1),
            seen: HashSet::new(),
            order: VecDeque::new(),
        }
    }

    pub fn observe(&mut self, service_id: ServiceId, sequence: u64) -> DedupeObservation {
        self.observe_in_scope(service_id, None, sequence)
    }

    pub fn observe_in_scope(
        &mut self,
        service_id: ServiceId,
        peer_scope: Option<u32>,
        sequence: u64,
    ) -> DedupeObservation {
        let key = (service_id, peer_scope, sequence);
        if self.seen.contains(&key) {
            return DedupeObservation::Duplicate;
        }
        self.seen.insert(key);
        self.order.push_back(key);
        while self.order.len() > self.capacity {
            if let Some(expired) = self.order.pop_front() {
                self.seen.remove(&expired);
            }
        }
        DedupeObservation::FirstSeen
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DedupeObservation {
    FirstSeen,
    Duplicate,
}
