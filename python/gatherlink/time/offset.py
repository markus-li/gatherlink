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
from statistics import median

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

DEFAULT_CLOCK_SYNC_INTERVAL_SECONDS = 2.0
DEFAULT_CLOCK_SYNC_MEAN_WINDOW_SECONDS = 30.0
CLOCK_SYNC_GOOD_MIN_SAMPLES = 3
CLOCK_SYNC_MAX_OFFSET_JUMP_US = 250_000
CLOCK_SYNC_ERROR_FLOOR_US = 1_000
CLOCK_SYNC_MIN_DELAY_MARGIN_US = 2_000
CLOCK_SYNC_MIN_DELAY_MARGIN_NUMERATOR = 5
CLOCK_SYNC_MIN_DELAY_MARGIN_DENOMINATOR = 4
CLOCK_SYNC_MIN_WEIGHT_JITTER_US = 1_000


@dataclass(frozen=True)
class _ClockSyncSample:
    """One validated four-timestamp clock sample kept by the Python control plane."""

    observed_at: float
    path_name: str
    offset_us: int
    rtt_us: int


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
        self._samples: deque[_ClockSyncSample] = deque()

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
    ) -> dict[str, object]:
        """Fold peer clock sync responses into current and rolling offset/RTT status."""
        update: dict[str, object] = {}
        path_summaries: dict[str, dict[str, int | str]] = {}
        path_latency_observations: list[dict[str, int | str | None]] = []
        path_latency_rejections: list[dict[str, int | str | None]] = []
        for message in clock_sync_messages:
            if message.mode != 2 or message.receive_us is None or message.transmit_us is None:
                continue
            pending = self._pending.pop(message.exchange_id, None)
            if pending is None:
                continue
            path_name, origin_us = pending
            destination_us = internal_monotonic_us()
            local_elapsed_us = destination_us - origin_us
            peer_elapsed_us = message.transmit_us - message.receive_us
            raw_rtt_us = local_elapsed_us - peer_elapsed_us
            public_path_name = path_names_by_id.get(message.path_id, path_name)
            # Impossible four-timestamp exchanges are worse than missing telemetry:
            # they can make TCP-sensitive schedulers steer toward phantom low-latency paths.
            if local_elapsed_us < 0 or peer_elapsed_us < 0 or raw_rtt_us < 0:
                path_latency_rejections.append(
                    {
                        "path": public_path_name,
                        "reason": "impossible-clock-exchange",
                        "rtt_us": raw_rtt_us,
                        "clock_error_us": None,
                    }
                )
                logger.warning(
                    "clock sync sample rejected",
                    extra={
                        "exchange_id": message.exchange_id,
                        "path": path_name,
                        "local_elapsed_us": local_elapsed_us,
                        "peer_elapsed_us": peer_elapsed_us,
                        "raw_rtt_us": raw_rtt_us,
                    },
                )
                continue
            offset_us = ((message.receive_us - origin_us) + (message.transmit_us - destination_us)) // 2
            rtt_us = raw_rtt_us
            now = time.monotonic()
            if self._is_offset_jump_suspect(offset_us, rtt_us, now):
                path_latency_rejections.append(
                    {
                        "path": public_path_name,
                        "reason": "offset-outlier",
                        "rtt_us": rtt_us,
                        "clock_error_us": None,
                    }
                )
                logger.warning(
                    "clock sync sample rejected as offset outlier",
                    extra={"exchange_id": message.exchange_id, "path": path_name, "offset_us": offset_us},
                )
                continue
            self._samples.append(_ClockSyncSample(now, path_name, offset_us, rtt_us))
            while self._samples and now - self._samples[0].observed_at > self._mean_window_seconds:
                self._samples.popleft()
            aggregate = _sample_summary(list(self._samples))
            path_samples = [sample for sample in self._samples if sample.path_name == path_name]
            path_aggregate = _sample_summary(path_samples)
            tx_one_way_us, rx_one_way_us = _directional_latency_from_clock_offset(
                origin_us=origin_us,
                receive_us=message.receive_us,
                transmit_us=message.transmit_us,
                destination_us=destination_us,
                offset_us=int(path_aggregate["offset_us"]),
            )
            path_summaries = _path_sample_summaries(list(self._samples))
            path_latency_observations.append(
                {
                    "path": public_path_name,
                    "tx_one_way_us": tx_one_way_us,
                    "rx_one_way_us": rx_one_way_us,
                    "source": "clock-synced-one-way",
                    "confidence": str(path_aggregate["confidence"]),
                    "rtt_us": rtt_us,
                    "clock_error_us": int(path_aggregate["error_budget_us"]),
                    "offset_us": int(path_aggregate["offset_us"]),
                }
            )
            update = {
                "role": "syncing-to-sink",
                "offset_us": offset_us,
                "mean_offset_us": aggregate["offset_us"],
                "rtt_us": rtt_us,
                "mean_rtt_us": aggregate["rtt_us"],
                "base_rtt_us": aggregate["base_rtt_us"],
                "best_rtt_us": aggregate["best_rtt_us"],
                "error_budget_us": aggregate["error_budget_us"],
                "uncertainty_us": aggregate["uncertainty_us"],
                "drift_ppb": aggregate["drift_ppb"],
                "confidence": aggregate["confidence"],
                "samples": len(self._samples),
                "path": public_path_name,
                "path_samples": len(path_samples),
                "path_mean_offset_us": path_aggregate["offset_us"],
                "path_mean_rtt_us": path_aggregate["rtt_us"],
                "path_base_rtt_us": path_aggregate["base_rtt_us"],
                "path_error_budget_us": path_aggregate["error_budget_us"],
                "path_uncertainty_us": path_aggregate["uncertainty_us"],
                "path_drift_ppb": path_aggregate["drift_ppb"],
                "path_confidence": path_aggregate["confidence"],
                "path_summaries": dict(path_summaries),
                "path_latency_observations": list(path_latency_observations),
                "path_latency_rejections": list(path_latency_rejections),
                "last_exchange_id": message.exchange_id,
                "last_at": datetime.now(UTC).isoformat(),
            }
        if path_latency_rejections and not update:
            update = {"path_latency_rejections": list(path_latency_rejections)}
        return update

    def _is_offset_jump_suspect(self, offset_us: int, rtt_us: int, now: float) -> bool:
        """Reject abrupt offset jumps once there is a recent baseline to compare against."""
        recent = [sample for sample in self._samples if now - sample.observed_at <= self._mean_window_seconds]
        if len(recent) < CLOCK_SYNC_GOOD_MIN_SAMPLES:
            return False
        baseline_us = int(median(sample.offset_us for sample in recent))
        best_rtt_us = min(sample.rtt_us for sample in recent)
        allowed_jump_us = max(CLOCK_SYNC_MAX_OFFSET_JUMP_US, best_rtt_us * 4, rtt_us * 4)
        return abs(offset_us - baseline_us) > allowed_jump_us


def internal_monotonic_us() -> int:
    """Return Gatherlink process-internal monotonic time in microseconds."""
    return time.monotonic_ns() // 1000


def _directional_latency_from_clock_offset(
    *,
    origin_us: int,
    receive_us: int,
    transmit_us: int,
    destination_us: int,
    offset_us: int,
) -> tuple[int, int]:
    """
    Estimate directional one-way latency from an already-selected peer offset.

    The offset is selected from the rolling Python-owned clock estimator, not
    blindly from this exchange. That keeps one noisy or asymmetric sample from
    deciding both the clock and the path latency. The downstream latency tracker
    still sanity-checks the directional sum against measured RTT and error
    budget before scheduler policy can consume the sample.
    """
    tx_one_way_us = receive_us - (origin_us + offset_us)
    rx_one_way_us = destination_us - (transmit_us - offset_us)
    return tx_one_way_us, rx_one_way_us


def _sample_summary(samples: list[_ClockSyncSample]) -> dict[str, int | str]:
    """Summarize validated clock samples without letting one noisy sample dominate."""
    if not samples:
        return {
            "offset_us": 0,
            "rtt_us": 0,
            "base_rtt_us": 0,
            "best_rtt_us": 0,
            "error_budget_us": 0,
            "uncertainty_us": 0,
            "drift_ppb": 0,
            "confidence": "warming",
        }
    low_delay_samples = _low_delay_samples(samples)
    offsets = [sample.offset_us for sample in low_delay_samples]
    rtts = [sample.rtt_us for sample in samples]
    low_delay_rtts = [sample.rtt_us for sample in low_delay_samples]
    best_rtt_us = min(rtts)
    base_rtt_us = best_rtt_us
    mean_rtt_us = int(sum(rtts) / len(rtts))
    # The compatibility field named "mean_offset_us" is now a weighted median.
    # Lowest-delay samples and stable paths get the most influence, while the
    # historical key stays stable for callers that already consume it.
    offset_us = _weighted_median_offset_us(low_delay_samples)
    offset_jitter_us = _mean_absolute_offset_jitter_us(offsets, offset_us)
    path_spread_us = _path_offset_spread_us(low_delay_samples)
    uncertainty_us = max(
        CLOCK_SYNC_ERROR_FLOOR_US,
        min(low_delay_rtts) // 2,
        offset_jitter_us,
        path_spread_us // 2,
    )
    error_budget_us = uncertainty_us
    drift_ppb = _drift_ppb(low_delay_samples)
    confidence = "good" if len(samples) >= CLOCK_SYNC_GOOD_MIN_SAMPLES else "warming"
    return {
        "offset_us": offset_us,
        "rtt_us": mean_rtt_us,
        "base_rtt_us": base_rtt_us,
        "best_rtt_us": best_rtt_us,
        "error_budget_us": error_budget_us,
        "uncertainty_us": uncertainty_us,
        "drift_ppb": drift_ppb,
        "confidence": confidence,
    }


def _path_sample_summaries(samples: list[_ClockSyncSample]) -> dict[str, dict[str, int | str]]:
    """Summarize every path currently represented in the rolling clock window."""
    summaries: dict[str, dict[str, int | str]] = {}
    for path_name in sorted({sample.path_name for sample in samples}):
        path_samples = [sample for sample in samples if sample.path_name == path_name]
        path_aggregate = _sample_summary(path_samples)
        summaries[path_name] = {
            "mean_rtt_us": int(path_aggregate["rtt_us"]),
            "base_rtt_us": int(path_aggregate["base_rtt_us"]),
            "best_rtt_us": int(path_aggregate["best_rtt_us"]),
            "error_budget_us": int(path_aggregate["error_budget_us"]),
            "uncertainty_us": int(path_aggregate["uncertainty_us"]),
            "drift_ppb": int(path_aggregate["drift_ppb"]),
            "confidence": str(path_aggregate["confidence"]),
            "samples": len(path_samples),
        }
    return summaries


def _low_delay_samples(samples: list[_ClockSyncSample]) -> list[_ClockSyncSample]:
    """Return samples closest to each path's minimum RTT, where queueing is least likely."""
    base_by_path: dict[str, int] = {}
    for sample in samples:
        base_by_path[sample.path_name] = min(sample.rtt_us, base_by_path.get(sample.path_name, sample.rtt_us))
    selected = [
        sample
        for sample in samples
        if sample.rtt_us
        <= max(
            base_by_path[sample.path_name] + CLOCK_SYNC_MIN_DELAY_MARGIN_US,
            base_by_path[sample.path_name]
            * CLOCK_SYNC_MIN_DELAY_MARGIN_NUMERATOR
            // CLOCK_SYNC_MIN_DELAY_MARGIN_DENOMINATOR,
        )
    ]
    return selected or samples


def _weighted_median_offset_us(samples: list[_ClockSyncSample]) -> int:
    """Return a robust multi-path offset consensus weighted by delay and stability."""
    weighted = sorted((sample.offset_us, _sample_weight(sample, samples)) for sample in samples)
    total_weight = sum(weight for _offset, weight in weighted)
    midpoint = max(1, total_weight // 2)
    cumulative = 0
    for offset_us, weight in weighted:
        cumulative += weight
        if cumulative >= midpoint:
            return int(offset_us)
    return int(weighted[-1][0])


def _sample_weight(sample: _ClockSyncSample, samples: list[_ClockSyncSample]) -> int:
    """Return a positive integer weight for one low-delay sample."""
    path_samples = [item for item in samples if item.path_name == sample.path_name]
    global_base_rtt_us = min(item.rtt_us for item in samples)
    path_offsets = [item.offset_us for item in path_samples]
    path_median_us = int(median(path_offsets))
    path_jitter_us = max(CLOCK_SYNC_MIN_WEIGHT_JITTER_US, _mean_absolute_offset_jitter_us(path_offsets, path_median_us))
    return max(1, global_base_rtt_us * 1_000_000 // max(sample.rtt_us, 1) // path_jitter_us)


def _path_offset_spread_us(samples: list[_ClockSyncSample]) -> int:
    """Return spread between per-path offset medians as an asymmetry confidence signal."""
    medians = []
    for path_name in sorted({sample.path_name for sample in samples}):
        offsets = [sample.offset_us for sample in samples if sample.path_name == path_name]
        if offsets:
            medians.append(int(median(offsets)))
    if len(medians) < 2:
        return 0
    return max(medians) - min(medians)


def _drift_ppb(samples: list[_ClockSyncSample]) -> int:
    """Estimate slow peer-clock frequency drift without mixing path-asymmetry offsets."""
    path_drifts = []
    for path_name in sorted({sample.path_name for sample in samples}):
        path_samples = sorted(
            (sample for sample in samples if sample.path_name == path_name),
            key=lambda sample: sample.observed_at,
        )
        if len(path_samples) < 2:
            continue
        first = path_samples[0]
        last = path_samples[-1]
        elapsed_seconds = last.observed_at - first.observed_at
        if elapsed_seconds <= 0:
            continue
        path_drifts.append(int((last.offset_us - first.offset_us) * 1_000 / elapsed_seconds))
    if not path_drifts:
        return 0
    return int(median(path_drifts))


def _mean_absolute_offset_jitter_us(samples: list[int], median_us: int) -> int:
    """Return a small robust clock-offset spread estimate."""
    if not samples:
        return 0
    return int(sum(abs(sample - median_us) for sample in samples) / len(samples))
