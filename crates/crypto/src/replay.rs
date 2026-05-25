//! Sliding replay window for authenticated transport packet counters.

/// Default number of transport counters tracked behind the highest seen value.
///
/// Multipath traffic can legitimately arrive hundreds of thousands of packets
/// behind the fastest path during saturation. This is still bounded replay
/// defense: old counters outside the window and already-seen counters inside it
/// are rejected, but normal multipath reorder does not collapse into crypto
/// drops.
pub const DEFAULT_REPLAY_WINDOW_BITS: usize = 1_048_576;
const MAX_REPLAY_WINDOW_BITS: usize = 4_194_304;

/// Fixed-size replay window for one receive direction.
#[derive(Debug, Clone)]
pub struct ReplayWindow {
    highest: u64,
    seen: Vec<Option<u64>>,
    initialized: bool,
    window_bits: usize,
}

impl Default for ReplayWindow {
    fn default() -> Self {
        Self::new(DEFAULT_REPLAY_WINDOW_BITS)
    }
}

impl ReplayWindow {
    /// Create a replay window. Oversized values are clamped to the local bounded storage limit.
    #[must_use]
    pub fn new(window_bits: usize) -> Self {
        let window_bits = window_bits.clamp(1, MAX_REPLAY_WINDOW_BITS);
        Self {
            highest: 0,
            seen: vec![None; window_bits],
            initialized: false,
            window_bits,
        }
    }

    /// Return whether `counter` is fresh and mark it as seen.
    pub fn accept(&mut self, counter: u64) -> bool {
        if !self.initialized {
            self.initialized = true;
            self.highest = counter;
            self.mark_seen(counter);
            return true;
        }
        if counter > self.highest {
            self.highest = counter;
            self.mark_seen(counter);
            return true;
        }

        let behind = self.highest - counter;
        if behind >= self.window_bits as u64 {
            return false;
        }
        if self.is_seen(counter) {
            return false;
        }
        self.mark_seen(counter);
        true
    }

    /// Highest accepted counter, if any packet has been accepted.
    #[must_use]
    pub fn highest(&self) -> Option<u64> {
        self.initialized.then_some(self.highest)
    }

    fn slot(&self, counter: u64) -> usize {
        counter as usize % self.window_bits
    }

    fn is_seen(&self, counter: u64) -> bool {
        self.seen[self.slot(counter)] == Some(counter)
    }

    fn mark_seen(&mut self, counter: u64) {
        let slot = self.slot(counter);
        self.seen[slot] = Some(counter);
    }
}
