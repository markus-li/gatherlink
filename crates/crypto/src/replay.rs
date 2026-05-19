//! Sliding replay window for authenticated transport packet counters.

/// Default number of transport counters tracked behind the highest seen value.
pub const DEFAULT_REPLAY_WINDOW_BITS: u64 = 128;

/// Fixed-size replay window for one receive direction.
#[derive(Debug, Clone)]
pub struct ReplayWindow {
    highest: u64,
    bitmap: u128,
    initialized: bool,
    window_bits: u64,
}

impl Default for ReplayWindow {
    fn default() -> Self {
        Self::new(DEFAULT_REPLAY_WINDOW_BITS)
    }
}

impl ReplayWindow {
    /// Create a replay window. Values above 128 are clamped to the local bitmap size.
    #[must_use]
    pub fn new(window_bits: u64) -> Self {
        Self {
            highest: 0,
            bitmap: 0,
            initialized: false,
            window_bits: window_bits.clamp(1, 128),
        }
    }

    /// Return whether `counter` is fresh and mark it as seen.
    pub fn accept(&mut self, counter: u64) -> bool {
        if !self.initialized {
            self.initialized = true;
            self.highest = counter;
            self.bitmap = 1;
            return true;
        }
        if counter > self.highest {
            let shift = counter - self.highest;
            self.bitmap = if shift >= 128 { 0 } else { self.bitmap << shift };
            self.bitmap |= 1;
            self.highest = counter;
            return true;
        }

        let behind = self.highest - counter;
        if behind >= self.window_bits || behind >= 128 {
            return false;
        }
        let mask = 1u128 << behind;
        if self.bitmap & mask != 0 {
            return false;
        }
        self.bitmap |= mask;
        true
    }

    /// Highest accepted counter, if any packet has been accepted.
    #[must_use]
    pub fn highest(&self) -> Option<u64> {
        self.initialized.then_some(self.highest)
    }
}
