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
PATH_LATENCY_PERCENTILE = 95
PATH_LATENCY_MAX_ACCEPTED_US = 10_000_000
DATA_TRAFFIC_SAMPLE_MAX_AGE_SECONDS = 30.0
DATA_TRAFFIC_CONTROL_BATCH_SIZE = 128

DataTransmitSample = tuple[int, int, int, int]


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
        self._samples: dict[str, dict[str, deque[tuple[float, int]]]] = {
            path_name: {"tx": deque(), "rx": deque()} for path_name in path_names
        }
        self._latency: dict[str, dict[str, int | str | None]] = {}
        self._dirty: set[str] = set()

    def observe(self, path_name: str, one_way_us: int) -> dict[str, dict[str, int | str | None]]:
        """Record one path latency observation and return the changed status row."""
        return self.observe_directional(
            path_name,
            tx_one_way_us=one_way_us,
            rx_one_way_us=one_way_us,
            source="reply-rtt-half",
            confidence="coarse",
        )

    def observe_directional(
        self,
        path_name: str,
        *,
        tx_one_way_us: int | None = None,
        rx_one_way_us: int | None = None,
        source: str = "clock-synced-one-way",
        confidence: str = "warming",
        rtt_us: int | None = None,
        clock_error_us: int | None = None,
    ) -> dict[str, dict[str, int | str | None]]:
        """Record directional path latency if the sample is sane enough for scheduler use."""
        if tx_one_way_us is None and rx_one_way_us is None:
            return {}
        rejection_reason = _latency_rejection_reason(tx_one_way_us, rx_one_way_us, rtt_us, clock_error_us)
        if rejection_reason:
            return self.reject(
                path_name,
                rejection_reason,
                source="rejected",
                confidence="rejected",
                rtt_us=rtt_us,
                clock_error_us=clock_error_us,
            )
        now = time.monotonic()
        path_samples = self._samples.setdefault(path_name, {"tx": deque(), "rx": deque()})
        existing = dict(self._latency.get(path_name, {}))
        if tx_one_way_us is not None:
            existing.update(_observe_direction(path_samples["tx"], "tx", tx_one_way_us, now))
        if rx_one_way_us is not None:
            existing.update(_observe_direction(path_samples["rx"], "rx", rx_one_way_us, now))
        source, confidence = _preserve_stronger_latency_provenance(
            existing,
            source=source,
            confidence=confidence,
        )
        existing.update(
            {
                "source": source,
                "confidence": confidence,
                "clock_error_us": clock_error_us,
                "rtt_us": rtt_us,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        self._latency[path_name] = existing
        self._dirty.add(path_name)
        return {path_name: dict(existing)}

    def reject(
        self,
        path_name: str,
        reason: str,
        *,
        source: str = "rejected",
        confidence: str = "rejected",
        rtt_us: int | None = None,
        clock_error_us: int | None = None,
    ) -> dict[str, dict[str, int | str | None]]:
        """
        Record that a latency candidate was deliberately rejected.

        Keeping the previous accepted values while marking the latest rejection
        lets operators and schedulers distinguish "no data" from "bad data was
        seen and ignored" without letting impossible samples steer path policy.
        """
        now = time.monotonic()
        path_samples = self._samples.setdefault(path_name, {"tx": deque(), "rx": deque()})
        _prune_direction(path_samples["tx"], now)
        _prune_direction(path_samples["rx"], now)
        existing = dict(self._latency.get(path_name, {}))
        has_fresh_latency = bool(path_samples["tx"] or path_samples["rx"])
        if has_fresh_latency and existing.get("source") not in {None, "rejected"}:
            existing.update(
                {
                    "rejection_reason": reason,
                    "rejection_rtt_us": rtt_us,
                    "rejection_clock_error_us": clock_error_us,
                    "rejection_updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._latency[path_name] = existing
            self._dirty.add(path_name)
            return {path_name: dict(existing)}
        existing.update(
            {
                "source": source,
                "confidence": confidence,
                "rejection_reason": reason,
                "clock_error_us": clock_error_us,
                "rtt_us": rtt_us,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        self._latency[path_name] = existing
        self._dirty.add(path_name)
        return {path_name: dict(existing)}

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


class DataTrafficLatencyTracker:
    """
    Match sparse real-data tx/rx samples into scheduler-safe latency observations.

    Rust only records cheap timing facts keyed by existing frame path/sequence
    fields. Python converts local process times into the current shared
    Gatherlink clock domain, advertises transmit samples over the control
    metaband, and accepts matched peer samples only when the timing is sane.
    """

    def __init__(self, path_names_by_id: dict[int, str]) -> None:
        self._path_names_by_id = dict(path_names_by_id)
        self._rx_samples: dict[tuple[int | None, int, int], tuple[int, float]] = {}
        self._pending_peer_tx: dict[tuple[int | None, int, int], tuple[int, float]] = {}

    def observe_local_samples(
        self,
        samples: dict[str, object],
        *,
        local_clock_offset_us: int | None,
    ) -> list[DataTransmitSample]:
        """
        Store local receive samples and return local transmit samples for control metadata.

        The transmit timestamp is converted to the shared sink clock domain when
        this node has an offset. Servers already use the sink clock domain, so
        `None` means "local is authoritative enough for this sample".
        """
        now = time.monotonic()
        self._prune(now)
        rx_samples = samples.get("rx")
        if isinstance(rx_samples, list):
            for sample in rx_samples:
                parsed = _parse_timing_sample(sample)
                if parsed is None:
                    continue
                path_id, sequence, _packet_count, observed_at_us, peer_scope = parsed
                self._rx_samples[(peer_scope, path_id, sequence)] = (
                    _shared_clock_us(observed_at_us, local_clock_offset_us),
                    now,
                )
        tx_control_samples: list[DataTransmitSample] = []
        tx_samples = samples.get("tx")
        if isinstance(tx_samples, list):
            for sample in tx_samples:
                parsed = _parse_timing_sample(sample)
                if parsed is None:
                    continue
                path_id, sequence, packet_count, observed_at_us, _peer_scope = parsed
                tx_control_samples.append(
                    (path_id, sequence, packet_count, _shared_clock_us(observed_at_us, local_clock_offset_us))
                )
        return tx_control_samples

    def observe_peer_transmit_samples(
        self,
        peer_samples: list[DataTransmitSample],
        *,
        peer_scope: int | None,
        local_clock_offset_us: int | None,
        local_clock_is_authoritative: bool = False,
        latency_tracker: PathLatencyTracker,
        rtt_us: int | None,
        clock_error_us: int | None,
    ) -> dict[str, dict[str, int | str | None]]:
        """Match peer-advertised transmit samples with local receive samples."""
        now = time.monotonic()
        self._prune(now)
        changed: dict[str, dict[str, int | str | None]] = {}
        for path_id, first_sequence, _packet_count, peer_tx_us in peer_samples:
            key = (peer_scope, path_id, first_sequence)
            rx_sample = self._rx_samples.get(key)
            if rx_sample is None:
                self._pending_peer_tx[key] = (peer_tx_us, now)
                continue
            changed.update(
                self._promote_match(
                    key,
                    peer_tx_us=peer_tx_us,
                    latency_tracker=latency_tracker,
                    local_clock_offset_us=local_clock_offset_us,
                    local_clock_is_authoritative=local_clock_is_authoritative,
                    rtt_us=rtt_us,
                    clock_error_us=clock_error_us,
                )
            )
        return changed

    def promote_pending_peer_transmit_samples(
        self,
        *,
        local_clock_offset_us: int | None,
        local_clock_is_authoritative: bool = False,
        latency_tracker: PathLatencyTracker,
        rtt_us: int | None,
        clock_error_us: int | None,
    ) -> dict[str, dict[str, int | str | None]]:
        """Promote peer samples that arrived before their matching real data packet."""
        changed: dict[str, dict[str, int | str | None]] = {}
        for key, (peer_tx_us, _observed_at) in list(self._pending_peer_tx.items()):
            if key not in self._rx_samples:
                continue
            changed.update(
                self._promote_match(
                    key,
                    peer_tx_us=peer_tx_us,
                    latency_tracker=latency_tracker,
                    local_clock_offset_us=local_clock_offset_us,
                    local_clock_is_authoritative=local_clock_is_authoritative,
                    rtt_us=rtt_us,
                    clock_error_us=clock_error_us,
                )
            )
            self._pending_peer_tx.pop(key, None)
        return changed

    def _prune(self, now: float) -> None:
        oldest = now - DATA_TRAFFIC_SAMPLE_MAX_AGE_SECONDS
        self._rx_samples = {key: value for key, value in self._rx_samples.items() if value[1] >= oldest}
        self._pending_peer_tx = {key: value for key, value in self._pending_peer_tx.items() if value[1] >= oldest}

    def _promote_match(
        self,
        key: tuple[int | None, int, int],
        *,
        peer_tx_us: int,
        latency_tracker: PathLatencyTracker,
        local_clock_offset_us: int | None,
        local_clock_is_authoritative: bool,
        rtt_us: int | None,
        clock_error_us: int | None,
    ) -> dict[str, dict[str, int | str | None]]:
        _peer_scope, path_id, _first_sequence = key
        local_rx_us, _sampled_at = self._rx_samples[key]
        one_way_us = local_rx_us - peer_tx_us
        path_name = self._path_names_by_id.get(path_id, f"path-id:{path_id}")
        confidence = "good" if local_clock_offset_us is not None or local_clock_is_authoritative else "warming"
        return latency_tracker.observe_directional(
            path_name,
            tx_one_way_us=one_way_us,
            source="data-traffic-one-way",
            confidence=confidence,
            rtt_us=rtt_us,
            clock_error_us=clock_error_us,
        )


def take_data_transmit_sample_batch(samples: list[DataTransmitSample]) -> list[DataTransmitSample]:
    """
    Pop a bounded real-data timing batch for control metadata.

    The buffer lives in Python so policy controls control-band pressure. Rust
    only exposes the sparse facts it already gathered while moving packets.
    """
    batch = samples[:DATA_TRAFFIC_CONTROL_BATCH_SIZE]
    del samples[:DATA_TRAFFIC_CONTROL_BATCH_SIZE]
    return batch


def _observe_direction(
    samples: deque[tuple[float, int]],
    direction: str,
    one_way_us: int,
    now: float,
) -> dict[str, int]:
    """Update one directional latency window and return status fields."""
    samples.append((now, one_way_us))
    _prune_direction(samples, now)
    observed = [sample for _sample_at, sample in samples]
    mean_us = int(sum(observed) / max(len(observed), 1))
    jitter_us = _mean_absolute_jitter_us(observed, mean_us)
    p95_us = _nearest_rank_percentile(observed, PATH_LATENCY_PERCENTILE)
    return {
        f"{direction}_current_us": one_way_us,
        f"{direction}_mean_us": mean_us,
        f"{direction}_jitter_us": jitter_us,
        f"{direction}_p95_us": p95_us,
    }


def _prune_direction(samples: deque[tuple[float, int]], now: float) -> None:
    """Drop stale directional latency samples from a rolling window."""
    while samples and now - samples[0][0] > PATH_LATENCY_MEAN_WINDOW_SECONDS:
        samples.popleft()


def _preserve_stronger_latency_provenance(
    existing: dict[str, int | str | None],
    *,
    source: str,
    confidence: str,
) -> tuple[str, str]:
    """
    Keep stronger real-data provenance when weaker probes arrive later.

    Clock-sync probes are useful for bootstrap and stats, but matched payload
    timing is the proof Python wants for TCP-like ordered promotion. Do not let
    a later control probe silently relabel a fresh real-data row as merely
    clock-derived.
    """
    if existing.get("source") == "data-traffic-one-way" and existing.get("confidence") == "good":
        if source != "data-traffic-one-way":
            existing["latest_probe_source"] = source
            existing["latest_probe_confidence"] = confidence
            return "data-traffic-one-way", "good"
    return source, confidence


def _parse_timing_sample(sample: object) -> tuple[int, int, int, int, int | None] | None:
    """Normalize one Rust timing sample dict while keeping bad samples harmless."""
    if not isinstance(sample, dict):
        return None
    try:
        path_id = int(sample["path_id"])
        sequence = int(sample["sequence"])
        packet_count = int(sample["packet_count"])
        observed_at_us = int(sample["observed_at_us"])
    except (KeyError, TypeError, ValueError):
        return None
    peer_scope_raw = sample.get("peer_scope")
    try:
        peer_scope = None if peer_scope_raw is None else int(peer_scope_raw)
    except (TypeError, ValueError):
        peer_scope = None
    if path_id < 0 or sequence < 0 or packet_count <= 0 or observed_at_us <= 0:
        return None
    return path_id, sequence, packet_count, observed_at_us, peer_scope


def _shared_clock_us(local_us: int, local_clock_offset_us: int | None) -> int:
    """Convert local process monotonic time into the current shared Gatherlink clock domain."""
    return local_us + (local_clock_offset_us or 0)


def _latency_rejection_reason(
    tx_one_way_us: int | None,
    rx_one_way_us: int | None,
    rtt_us: int | None,
    clock_error_us: int | None,
) -> str | None:
    """Return why a latency fact is unusable before it can affect scheduler policy."""
    values = [value for value in [tx_one_way_us, rx_one_way_us, rtt_us, clock_error_us] if value is not None]
    if any(value < 0 for value in values):
        return "negative-sample"
    if any(value > PATH_LATENCY_MAX_ACCEPTED_US for value in [tx_one_way_us, rx_one_way_us] if value is not None):
        return "unreasonable-sample"
    if rtt_us is None:
        return None
    error_budget_us = max(clock_error_us or 0, 2_000)
    for one_way_us in [tx_one_way_us, rx_one_way_us]:
        if one_way_us is not None and one_way_us > rtt_us + error_budget_us:
            return "impossible-rtt"
    if tx_one_way_us is None or rx_one_way_us is None:
        return None
    if tx_one_way_us + rx_one_way_us > rtt_us + (error_budget_us * 2):
        return "impossible-rtt"
    return None


def prune_outstanding_latency_packets(outstanding_packets: dict[int, tuple[str, float]]) -> None:
    """Drop unmatched reply probes so lossy tests cannot grow the latency tracker forever."""
    oldest_allowed = time.monotonic() - PATH_LATENCY_OUTSTANDING_MAX_AGE_SECONDS
    expired_sequences = [
        sequence for sequence, (_path_name, sent_at) in outstanding_packets.items() if sent_at < oldest_allowed
    ]
    for sequence in expired_sequences:
        outstanding_packets.pop(sequence, None)


def _mean_absolute_jitter_us(samples: list[int], mean_us: int) -> int:
    """Return a cheap bounded jitter summary for Python scheduler decisions."""
    if not samples:
        return 0
    return int(sum(abs(sample - mean_us) for sample in samples) / len(samples))


def _nearest_rank_percentile(samples: list[int], percentile: int) -> int:
    """Return a small-window nearest-rank percentile without external dependencies."""
    if not samples:
        return 0
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), (len(ordered) * percentile + 99) // 100))
    return ordered[rank - 1]
