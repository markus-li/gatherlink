"""Small scoring helpers for Python-owned path scheduling policy."""

from __future__ import annotations

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

MIN_PATH_WEIGHT = 1
MAX_PATH_WEIGHT = 65535
DEFAULT_CAPACITY_BPS = 1_000_000
DEFAULT_LATENCY_US = 50_000
LOSS_PPM_SCALE = 1_000_000
QUEUE_PACKET_PENALTY = 16
QUEUE_BYTE_PENALTY_STEP = 64 * 1024


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
) -> int:
    """
    Compute a conservative compiled weight from live path facts.

    Python may become much smarter here without touching Rust. Rust only gets
    the final integer weight plus primitive limits.
    """
    score = capacity_weight(tx_capacity_bps) * 16
    score -= latency_penalty(latency_us)
    score -= loss_penalty(loss_ppm)
    score -= queue_penalty(queue_depth_packets, queue_depth_bytes)
    return _clamp_weight(score)


def _clamp_weight(value: int) -> int:
    """Keep the compiled value within Rust's u16 scheduler contract."""
    return min(MAX_PATH_WEIGHT, max(MIN_PATH_WEIGHT, value))

