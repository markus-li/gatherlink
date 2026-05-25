"""Small scoring helpers for Python-owned path scheduling policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gatherlink.scheduling.metrics import PathSchedulerMetrics
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

MIN_PATH_WEIGHT = 1
MAX_PATH_WEIGHT = 65535
DEFAULT_CAPACITY_BPS = 1_000_000
DEFAULT_LATENCY_US = 50_000
LOSS_PPM_SCALE = 1_000_000
QUEUE_PACKET_PENALTY = 16
QUEUE_BYTE_PENALTY_STEP = 64 * 1024
JITTER_PENALTY_STEP_US = 10_000


PathHealth = Literal["alive", "degraded", "down"]
STALE_CONTROL_DOWN_US = 120_000_000
STALE_CONTROL_DEGRADED_US = 30_000_000
HIGH_LOSS_PPM = 50_000
HIGH_QUEUE_PACKETS = 256
HIGH_QUEUE_AGE_US = 5_000_000
HIGH_REORDER_DEPTH_PACKETS = 128
HIGH_IN_FLIGHT_PACKETS = 512
HIGH_REORDER_BUFFER_PACKETS = 128
CAPACITY_CONFIDENCE_HIGH_PPM = 850_000


@dataclass(frozen=True)
class PathScore:
    """Python-owned explanation of one path's scheduler health."""

    path_name: str
    health: PathHealth
    score: int
    weight: int
    capacity_confidence_ppm: int
    reasons: tuple[str, ...]

    def export_dict(self) -> dict[str, object]:
        """Return stable facts for diagnostics, monitor output, and tests."""
        return {
            "path": self.path_name,
            "health": self.health,
            "score": self.score,
            "weight": self.weight,
            "capacity_confidence_ppm": self.capacity_confidence_ppm,
            "reasons": list(self.reasons),
        }


def score_path(metrics: PathSchedulerMetrics) -> PathScore:
    """
    Score a path using Python-owned telemetry interpretation.

    Rust should never infer these meanings from raw counters. Python turns noisy
    operational facts into a health label, an explanation, and small execution
    primitives such as weights or drain/disabled state.
    """
    reasons: list[str] = []
    score = capacity_weight(metrics.tx_capacity_bps) * 100
    latency = metrics.scheduler_latency_us
    if latency is not None:
        latency_cost = min(80, latency_penalty(latency) * 10)
        score -= latency_cost
        if latency_cost:
            reasons.append("latency_penalty")
    loss_cost = min(100, loss_penalty(metrics.loss_ppm) * 4)
    if loss_cost:
        score -= loss_cost
        reasons.append("loss_penalty")
    queue_cost = min(120, queue_penalty(metrics.queue_depth_packets, metrics.queue_depth_bytes))
    if queue_cost:
        score -= queue_cost
        reasons.append("queue_pressure")
    if metrics.send_failures:
        score -= min(120, metrics.send_failures * 20)
        reasons.append("send_failures")
    if metrics.receive_gaps:
        score -= min(80, metrics.receive_gaps * 10)
        reasons.append("receive_gaps")
    if metrics.local_drops:
        score -= min(120, metrics.local_drops * 20)
        reasons.append("local_drops")
    if metrics.reorder_depth_packets >= HIGH_REORDER_DEPTH_PACKETS:
        score -= 80
        reasons.append("reorder_pressure")
    if metrics.scheduler_in_flight_packets >= HIGH_IN_FLIGHT_PACKETS:
        score -= 40
        reasons.append("in_flight_pressure")
    if metrics.reorder_buffer_packets >= HIGH_REORDER_BUFFER_PACKETS:
        score -= 80
        reasons.append("receiver_blocking")

    health: PathHealth = "alive"
    if metrics.stale_control_age_us >= STALE_CONTROL_DOWN_US:
        health = "down"
        reasons.append("control_stale")
    elif (
        metrics.stale_control_age_us >= STALE_CONTROL_DEGRADED_US
        or metrics.loss_ppm >= HIGH_LOSS_PPM
        or metrics.queue_depth_packets >= HIGH_QUEUE_PACKETS
        or metrics.queue_oldest_age_us >= HIGH_QUEUE_AGE_US
        or metrics.local_drops > 0
        or metrics.send_failures > 0
        or metrics.scheduler_in_flight_packets >= HIGH_IN_FLIGHT_PACKETS
        or metrics.reorder_buffer_packets >= HIGH_REORDER_BUFFER_PACKETS
    ):
        health = "degraded"
    weight = adaptive_weight(
        tx_capacity_bps=metrics.tx_capacity_bps,
        latency_us=latency,
        loss_ppm=metrics.loss_ppm,
        queue_depth_packets=metrics.queue_depth_packets,
        queue_depth_bytes=metrics.queue_depth_bytes,
    )
    capacity_confidence = capacity_confidence_ppm(metrics)
    if health == "down":
        weight = 1
        score = min(score, 0)
    elif health == "degraded":
        weight = max(1, weight // 2)
    if not reasons:
        reasons.append("healthy")
    return PathScore(
        path_name=metrics.path_name,
        health=health,
        score=max(0, score),
        weight=weight,
        capacity_confidence_ppm=capacity_confidence,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def score_snapshot(paths: dict[str, PathSchedulerMetrics]) -> dict[str, PathScore]:
    """Score all paths in a telemetry snapshot with stable ordering."""
    return {name: score_path(paths[name]) for name in sorted(paths)}


def capacity_weight(capacity_bps: int | None, *, baseline_bps: int = DEFAULT_CAPACITY_BPS) -> int:
    """Turn a bandwidth estimate into a bounded Rust weight."""
    if capacity_bps is None:
        return MIN_PATH_WEIGHT
    return _clamp_weight(round(capacity_bps / max(1, baseline_bps)))


def latency_penalty(latency_us: int | None, *, baseline_us: int = DEFAULT_LATENCY_US) -> int:
    """Return an integer penalty for paths slower than the baseline latency."""
    if latency_us is None:
        return 0
    return max(0, latency_us - baseline_us) // max(1, baseline_us)


def loss_penalty(loss_ppm: int) -> int:
    """Return an integer penalty from smoothed loss parts-per-million."""
    return max(0, min(loss_ppm, LOSS_PPM_SCALE)) // 10_000


def queue_penalty(queue_depth_packets: int, queue_depth_bytes: int) -> int:
    """Return an integer penalty for already-buffered path work."""
    return max(0, queue_depth_packets) * QUEUE_PACKET_PENALTY + max(0, queue_depth_bytes) // QUEUE_BYTE_PENALTY_STEP


def adaptive_weight(
    *,
    tx_capacity_bps: int | None,
    latency_us: int | None,
    loss_ppm: int,
    queue_depth_packets: int = 0,
    queue_depth_bytes: int = 0,
    jitter_us: int | None = None,
) -> int:
    """
    Compute a conservative compiled weight from live path facts.

    Python may become much smarter here without touching Rust. Rust only gets
    the final integer weight plus primitive limits.
    """
    score = capacity_weight(tx_capacity_bps) * 16
    score -= latency_penalty(latency_us)
    score -= jitter_penalty(jitter_us)
    score -= loss_penalty(loss_ppm)
    score -= queue_penalty(queue_depth_packets, queue_depth_bytes)
    return _clamp_weight(score)


def jitter_penalty(jitter_us: int | None) -> int:
    """Return a small penalty for paths whose latency varies enough to reorder traffic."""
    if jitter_us is None:
        return 0
    return max(0, jitter_us) // JITTER_PENALTY_STEP_US


def capacity_confidence_ppm(metrics: PathSchedulerMetrics) -> int:
    """
    Return confidence in the current path-capacity estimate.

    This deliberately does not change `capacity_aware` weights by itself.
    Saturated lab links and real WAN facsimiles can produce drops while the
    configured capacity ratio remains the correct split. The confidence value
    is an explanation and coordinator input, not hidden Rust policy.
    """
    confidence = 1_000_000
    if metrics.tx_capacity_bps is None:
        confidence -= 250_000
    if metrics.stale_control_age_us >= STALE_CONTROL_DEGRADED_US:
        confidence -= 350_000
    confidence -= min(250_000, metrics.loss_ppm * 2)
    confidence -= min(150_000, metrics.queue_depth_packets * 250)
    confidence -= min(150_000, metrics.local_drops)
    confidence -= min(100_000, metrics.send_failures * 10_000)
    return max(0, min(1_000_000, confidence))


def _clamp_weight(value: int) -> int:
    """Keep the compiled value within Rust's u16 scheduler contract."""
    return min(MAX_PATH_WEIGHT, max(MIN_PATH_WEIGHT, value))
