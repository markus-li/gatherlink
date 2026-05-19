"""
Path telemetry helpers for scheduler-visible control metadata.

This module is production control-plane code. Lab scenarios use it because labs
exercise the same per-path facts the Python scheduler will eventually consume:
current latency, rolling latency, and bounded outstanding packet tracking.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime

PATH_LATENCY_MEAN_WINDOW_SECONDS = 30.0
PATH_LATENCY_OUTSTANDING_MAX_AGE_SECONDS = 120.0


class PathLatencyTracker:
    """
    Track per-path latency from live reply packets for scheduler-visible control metadata.

    The first production-safe estimator uses reply RTT for the same Gatherlink
    sequence and reports half of that as an early one-way estimate. That keeps
    the mechanism traffic-derived while leaving room for richer peer timestamp
    metadata and confidence scoring once both directional measurements mature.
    """

    def __init__(self, path_names: list[str]) -> None:
        self._path_names = path_names
        self._samples: dict[str, deque[tuple[float, int]]] = {path_name: deque() for path_name in path_names}
        self._latency: dict[str, dict[str, int | str | None]] = {}
        self._dirty: set[str] = set()

    def observe(self, path_name: str, one_way_us: int) -> dict[str, dict[str, int | str | None]]:
        """Record one path latency observation and return the changed status row."""
        now = time.monotonic()
        samples = self._samples.setdefault(path_name, deque())
        samples.append((now, one_way_us))
        while samples and now - samples[0][0] > PATH_LATENCY_MEAN_WINDOW_SECONDS:
            samples.popleft()
        mean_us = int(sum(sample for _sample_at, sample in samples) / max(len(samples), 1))
        # In the local reply lab, the same path carries both request and reply. Report both directions so the monitor
        # and future Python scheduler can consume one stable shape before production peer telemetry gets richer.
        self._latency[path_name] = {
            "tx_current_us": one_way_us,
            "tx_mean_us": mean_us,
            "rx_current_us": one_way_us,
            "rx_mean_us": mean_us,
            "source": "reply-rtt-half",
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._dirty.add(path_name)
        return {path_name: dict(self._latency[path_name])}

    def dirty_snapshot(self) -> dict[str, dict[str, int | str | None]]:
        """Return changed latency records that have not yet been sent over control metadata."""
        return {
            path_name: dict(self._latency[path_name])
            for path_name in self._path_names
            if path_name in self._dirty and path_name in self._latency
        }

    def mark_sent(self) -> None:
        """Mark the current dirty latency records as advertised."""
        self._dirty.clear()


def prune_outstanding_latency_packets(outstanding_packets: dict[int, tuple[str, float]]) -> None:
    """Drop unmatched reply probes so lossy tests cannot grow the latency tracker forever."""
    oldest_allowed = time.monotonic() - PATH_LATENCY_OUTSTANDING_MAX_AGE_SECONDS
    expired_sequences = [
        sequence for sequence, (_path_name, sent_at) in outstanding_packets.items() if sent_at < oldest_allowed
    ]
    for sequence in expired_sequences:
        outstanding_packets.pop(sequence, None)
