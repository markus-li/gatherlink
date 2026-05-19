"""Replay-window helpers for authenticated transport packet counters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReplayWindow:
    """Track recently accepted transport counters for one receive direction."""

    window_bits: int = 128
    highest: int | None = None
    bitmap: int = 0

    def __post_init__(self) -> None:
        """Clamp the tracked bitmap to the implemented local size."""
        self.window_bits = max(1, min(self.window_bits, 128))

    def accept(self, counter: int) -> bool:
        """Return whether a counter is fresh and mark it as accepted."""
        if counter < 0 or counter >= 2**64:
            return False
        if self.highest is None:
            self.highest = counter
            self.bitmap = 1
            return True
        if counter > self.highest:
            shift = counter - self.highest
            self.bitmap = 0 if shift >= 128 else self.bitmap << shift
            self.bitmap |= 1
            self.highest = counter
            return True

        behind = self.highest - counter
        if behind >= self.window_bits or behind >= 128:
            return False
        mask = 1 << behind
        if self.bitmap & mask:
            return False
        self.bitmap |= mask
        return True

