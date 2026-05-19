"""
Peer-relative internal clock offset tracking.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

DEFAULT_CLOCK_SYNC_INTERVAL_SECONDS = 2.0
DEFAULT_CLOCK_SYNC_MEAN_WINDOW_SECONDS = 30.0


@dataclass(frozen=True)
class InternalClockSyncMessage:
    """NTP-style internal clock sync message carried on Gatherlink control metadata."""

    exchange_id: int
    path_id: int
    mode: int
    origin_us: int
    receive_us: int | None = None
    transmit_us: int | None = None


@dataclass(frozen=True)
class SinkTimeMessage:
    """Authoritative sink wall-clock status carried on Gatherlink control metadata."""

    path_id: int
    sink_unix_us: int
    sink_internal_us: int
    ntp_state: int


class InternalClockSyncClient:
    """
    Estimate local monotonic offset to a peer's authoritative internal clock.

    This mirrors the NTP four-timestamp calculation but stays entirely inside Gatherlink process time. System NTP can
    discipline wall clocks separately; this offset is the value future Python policy can use for replay windows,
    sliding telemetry windows, and crypto timestamp decisions before compiling any fast-path requirements for Rust.
    """

    def __init__(
        self,
        path_names: list[str],
        *,
        interval_seconds: float = DEFAULT_CLOCK_SYNC_INTERVAL_SECONDS,
        mean_window_seconds: float = DEFAULT_CLOCK_SYNC_MEAN_WINDOW_SECONDS,
    ) -> None:
        self._path_names = path_names
        self._interval_seconds = interval_seconds
        self._mean_window_seconds = mean_window_seconds
        self._next_exchange_id = 1
        self._last_request_at = 0.0
        self._pending: dict[int, tuple[str, int]] = {}
        self._samples: deque[tuple[float, int, int]] = deque()

    def create_requests(
        self,
        path_names: list[str],
        path_ids: dict[str, int],
    ) -> dict[str, InternalClockSyncMessage]:
        """Create sparse sync requests when the configured interval has elapsed."""
        now = time.monotonic()
        if now - self._last_request_at < self._interval_seconds:
            return {}
        self._last_request_at = now
        requests = {}
        for path_name in path_names:
            exchange_id = self._next_exchange_id
            self._next_exchange_id += 1
            origin_us = internal_monotonic_us()
            self._pending[exchange_id] = (path_name, origin_us)
            requests[path_name] = InternalClockSyncMessage(
                exchange_id=exchange_id,
                path_id=path_ids[path_name],
                mode=1,
                origin_us=origin_us,
            )
        return requests

    def observe_control_frame(
        self,
        clock_sync_messages: list[InternalClockSyncMessage],
        *,
        path_names_by_id: dict[int, str],
    ) -> dict[str, int | str | None]:
        """Fold peer clock sync responses into current and rolling offset/RTT status."""
        update: dict[str, int | str | None] = {}
        for message in clock_sync_messages:
            if message.mode != 2 or message.receive_us is None or message.transmit_us is None:
                continue
            pending = self._pending.pop(message.exchange_id, None)
            if pending is None:
                continue
            path_name, origin_us = pending
            destination_us = internal_monotonic_us()
            offset_us = ((message.receive_us - origin_us) + (message.transmit_us - destination_us)) // 2
            rtt_us = max((destination_us - origin_us) - (message.transmit_us - message.receive_us), 0)
            now = time.monotonic()
            self._samples.append((now, offset_us, rtt_us))
            while self._samples and now - self._samples[0][0] > self._mean_window_seconds:
                self._samples.popleft()
            mean_offset_us = int(sum(sample[1] for sample in self._samples) / max(len(self._samples), 1))
            mean_rtt_us = int(sum(sample[2] for sample in self._samples) / max(len(self._samples), 1))
            update = {
                "role": "syncing-to-sink",
                "offset_us": offset_us,
                "mean_offset_us": mean_offset_us,
                "rtt_us": rtt_us,
                "mean_rtt_us": mean_rtt_us,
                "samples": len(self._samples),
                "path": path_names_by_id.get(message.path_id, path_name),
                "last_exchange_id": message.exchange_id,
                "last_at": datetime.now(UTC).isoformat(),
            }
        return update


def internal_monotonic_us() -> int:
    """Return Gatherlink process-internal monotonic time in microseconds."""
    return time.monotonic_ns() // 1000
