"""
Deterministic scheduler behavior simulation for reports and tests.

This module does not move packet scheduling policy into the lab. It gives the
Python control plane a repeatable way to explain how each policy should compile
and behave when the same three paths see different network conditions.
"""

from __future__ import annotations

from dataclasses import dataclass

from gatherlink.config.models import SchedulerPolicy
from gatherlink.config.runtime import SchedulerMode
from gatherlink.scheduling.policies import rust_mode_for_policy


@dataclass(frozen=True)
class SimulatedPath:
    """One scheduler-visible path in a deterministic scenario."""

    name: str
    tx_capacity_bps: int
    latency_us: int
    loss_ppm: int = 0
    queue_depth_packets: int = 0
    queue_depth_bytes: int = 0
    reorder_hold_us: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class SchedulerScenario:
    """A named set of path facts used to compare scheduler behavior."""

    name: str
    description: str
    paths: tuple[SimulatedPath, ...]
    payload_len: int = 1200


@dataclass(frozen=True)
class SchedulerDecision:
    """One policy's deterministic choice for one scenario."""

    policy: SchedulerPolicy
    rust_mode: SchedulerMode
    selected_path: str | None
    reason: str


POLICIES_TO_COMPARE: tuple[SchedulerPolicy, ...] = (
    "round_robin",
    "weighted_round_robin",
    "srtt",
    "lowest_latency",
    "loss_aware",
    "capacity_aware",
    "least_queue",
    "earliest_completion_first",
    "blocking_estimation",
    "ordered_multipath",
    "ordered_multipath_capacity_aware",
    "arrival_guarded_capacity",
    "flowlet_adaptive",
    "latency_guarded_capacity",
    "balanced",
    "adaptive",
)


THREE_PATH_SCENARIOS: tuple[SchedulerScenario, ...] = (
    SchedulerScenario(
        name="clean-balanced",
        description="All paths are clean; path-a is fastest, path-c has the most capacity.",
        paths=(
            SimulatedPath("path-a", tx_capacity_bps=3_200_000, latency_us=10_000),
            SimulatedPath("path-b", tx_capacity_bps=2_000_000, latency_us=35_000),
            SimulatedPath("path-c", tx_capacity_bps=5_000_000, latency_us=55_000),
        ),
    ),
    SchedulerScenario(
        name="loss-on-fast-path",
        description="The low-latency path starts dropping packets; loss-aware policies should avoid it.",
        paths=(
            SimulatedPath("path-a", tx_capacity_bps=3_200_000, latency_us=10_000, loss_ppm=120_000),
            SimulatedPath("path-b", tx_capacity_bps=2_000_000, latency_us=35_000),
            SimulatedPath("path-c", tx_capacity_bps=5_000_000, latency_us=55_000),
        ),
    ),
    SchedulerScenario(
        name="high-capacity-slow-path",
        description="A large packet makes capacity matter more than raw latency.",
        payload_len=1400,
        paths=(
            SimulatedPath("path-a", tx_capacity_bps=200_000, latency_us=5_000),
            SimulatedPath("path-b", tx_capacity_bps=2_000_000, latency_us=35_000),
            SimulatedPath("path-c", tx_capacity_bps=8_000_000, latency_us=60_000),
        ),
    ),
    SchedulerScenario(
        name="queue-pressure",
        description="The preferred low-latency path is backed up; queue-aware policies should move away.",
        paths=(
            SimulatedPath(
                "path-a",
                tx_capacity_bps=3_200_000,
                latency_us=10_000,
                queue_depth_packets=64,
                queue_depth_bytes=256_000,
            ),
            SimulatedPath("path-b", tx_capacity_bps=2_000_000, latency_us=35_000),
            SimulatedPath("path-c", tx_capacity_bps=5_000_000, latency_us=55_000),
        ),
    ),
    SchedulerScenario(
        name="jitter-reorder-risk",
        description="Path-c is high-capacity but has a large reorder hold penalty.",
        paths=(
            SimulatedPath("path-a", tx_capacity_bps=3_200_000, latency_us=15_000),
            SimulatedPath("path-b", tx_capacity_bps=2_000_000, latency_us=35_000),
            SimulatedPath("path-c", tx_capacity_bps=5_000_000, latency_us=30_000, reorder_hold_us=150_000),
        ),
    ),
)


def run_scheduler_matrix(
    scenarios: tuple[SchedulerScenario, ...] = THREE_PATH_SCENARIOS,
    policies: tuple[SchedulerPolicy, ...] = POLICIES_TO_COMPARE,
) -> dict[str, list[SchedulerDecision]]:
    """Return deterministic scheduler decisions for every scenario and policy."""
    return {scenario.name: [choose_path(policy, scenario) for policy in policies] for scenario in scenarios}


def choose_path(policy: SchedulerPolicy, scenario: SchedulerScenario) -> SchedulerDecision:
    """Choose the path a scheduler policy should prefer in this scenario."""
    rust_mode = rust_mode_for_policy(policy)
    enabled_paths = [path for path in scenario.paths if path.enabled]
    if not enabled_paths:
        return SchedulerDecision(policy=policy, rust_mode=rust_mode, selected_path=None, reason="no enabled paths")

    if policy == "flowlet_adaptive":
        selected = min(enabled_paths, key=_balanced_score(scenario.payload_len))
        return SchedulerDecision(policy, rust_mode, selected.name, "flowlet stickiness with adaptive fallback score")
    if policy == "arrival_guarded_capacity":
        selected = _arrival_guarded_capacity_choice(enabled_paths, scenario.payload_len)
        return SchedulerDecision(
            policy,
            rust_mode,
            selected.name,
            "capacity share after Python predicted-arrival guard",
        )
    if policy == "latency_guarded_capacity":
        return SchedulerDecision(
            policy,
            rust_mode,
            enabled_paths[0].name,
            "first slot after Python latency guard and capacity-weight compilation",
        )
    if policy == "capacity_aware":
        return SchedulerDecision(
            policy,
            rust_mode,
            enabled_paths[0].name,
            "first slot in Python-compiled capacity-weighted sequence",
        )
    if rust_mode in {"round_robin", "weighted_round_robin", "adaptive"}:
        if policy in {"balanced", "adaptive"}:
            selected = min(enabled_paths, key=_balanced_score(scenario.payload_len))
            return SchedulerDecision(policy, rust_mode, selected.name, "best hybrid capacity/latency/loss/queue score")
        return SchedulerDecision(policy, rust_mode, enabled_paths[0].name, "first eligible path in weighted sequence")
    if rust_mode == "lowest_latency":
        selected = min(enabled_paths, key=lambda path: (path.latency_us, path.loss_ppm, -path.tx_capacity_bps))
        return SchedulerDecision(policy, rust_mode, selected.name, "lowest latency")
    if rust_mode == "loss_aware":
        selected = min(enabled_paths, key=lambda path: (path.loss_ppm, path.latency_us, -path.tx_capacity_bps))
        return SchedulerDecision(policy, rust_mode, selected.name, "lowest loss with latency tie-breaker")
    if rust_mode == "capacity_aware":
        selected = max(enabled_paths, key=lambda path: (path.tx_capacity_bps, -path.latency_us, -path.loss_ppm))
        return SchedulerDecision(policy, rust_mode, selected.name, "highest TX capacity")
    if rust_mode == "least_queue":
        selected = min(
            enabled_paths, key=lambda path: (path.queue_depth_packets, path.queue_depth_bytes, path.latency_us)
        )
        return SchedulerDecision(policy, rust_mode, selected.name, "lowest queue pressure")
    if rust_mode == "earliest_completion_first":
        selected = min(enabled_paths, key=_completion_score(scenario.payload_len))
        return SchedulerDecision(policy, rust_mode, selected.name, "lowest latency plus transmit time")
    if rust_mode == "blocking_estimation":
        selected = min(enabled_paths, key=_blocking_score(scenario.payload_len))
        return SchedulerDecision(policy, rust_mode, selected.name, "lowest completion time plus reorder hold")
    if rust_mode == "ordered_multipath":
        selected = min(enabled_paths, key=_ordered_multipath_score(scenario.payload_len))
        return SchedulerDecision(policy, rust_mode, selected.name, "earliest safe service-flow arrival")
    if rust_mode == "balanced":
        selected = min(enabled_paths, key=_balanced_score(scenario.payload_len))
        return SchedulerDecision(policy, rust_mode, selected.name, "best hybrid capacity/latency/loss/queue score")
    raise ValueError(f"unsupported rust scheduler mode: {rust_mode}")


def _completion_score(payload_len: int):
    """Return a score function matching Rust's ECF-like target."""

    def score(path: SimulatedPath) -> tuple[int, int, int]:
        transmit_us = payload_len * 8 * 1_000_000 // max(path.tx_capacity_bps, 1)
        return (path.latency_us + transmit_us, path.loss_ppm, -path.tx_capacity_bps)

    return score


def _blocking_score(payload_len: int):
    """Return a score function matching Rust's blocking-estimation target."""

    def score(path: SimulatedPath) -> tuple[int, int, int]:
        completion_us, loss_ppm, inverse_capacity = _completion_score(payload_len)(path)
        return (completion_us + path.reorder_hold_us, loss_ppm, inverse_capacity)

    return score


def _balanced_score(payload_len: int):
    """Return the hybrid policy score used by balanced/adaptive reports."""

    def score(path: SimulatedPath) -> tuple[int, int, int]:
        completion_us, loss_ppm, inverse_capacity = _completion_score(payload_len)(path)
        queue_penalty = path.queue_depth_packets * 16 + path.queue_depth_bytes // (64 * 1024)
        return (completion_us + loss_ppm * 100 + path.latency_us // 4 + queue_penalty, inverse_capacity, loss_ppm)

    return score


def _arrival_guarded_capacity_choice(paths: list[SimulatedPath], payload_len: int) -> SimulatedPath:
    """Choose a capacity path after removing predicted receiver-blocking outliers."""
    predictions = {path.name: _arrival_prediction_us(path, payload_len) for path in paths}
    fastest = min(predictions.values())
    eligible = [path for path in paths if predictions[path.name] <= fastest + (path.reorder_hold_us or 150_000)]
    if not eligible:
        eligible = paths
    return max(eligible, key=lambda path: (path.tx_capacity_bps, -path.latency_us, -path.loss_ppm))


def _arrival_prediction_us(path: SimulatedPath, payload_len: int) -> int:
    """Return the same coarse arrival estimate the Python compiler uses."""
    transmit_us = payload_len * 8 * 1_000_000 // max(1, path.tx_capacity_bps)
    queue_us = path.queue_depth_packets * 50 + path.queue_depth_bytes * 8 * 1_000_000 // max(1, path.tx_capacity_bps)
    return path.latency_us + transmit_us + queue_us


def _ordered_multipath_score(payload_len: int):
    """Return the first-packet score used by the ordered multipath executor."""

    def score(path: SimulatedPath) -> tuple[int, int, int]:
        completion_us, loss_ppm, inverse_capacity = _completion_score(payload_len)(path)
        queue_penalty = path.queue_depth_packets + payload_len * 8 * 1_000_000 // max(path.tx_capacity_bps, 1)
        return (completion_us + path.reorder_hold_us + loss_ppm * 100, queue_penalty, inverse_capacity)

    return score
