"""
Compile Python scheduler decisions into simple Rust runtime weights/rules.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.config.models import GatherlinkConfig, PathConfig, SchedulerPolicy, ServicePriority
from gatherlink.config.runtime import RuntimePathSchedulerConfig, RuntimeSchedulerConfig
from gatherlink.scheduling.metrics import SchedulerTelemetrySnapshot
from gatherlink.scheduling.policies import compile_path_policy, rust_mode_for_policy
from gatherlink.scheduling.scoring import score_snapshot
from gatherlink.scheduling.service_intent import service_traffic_summary
from gatherlink.shared.logging import get_logger

LATENCY_GUARD_MIN_MARGIN_US = 10_000
LATENCY_GUARD_MARGIN_DIVISOR = 2
CAPACITY_SHARE_POLICIES = {
    "capacity_aware",
    "latency_guarded_capacity",
    "ordered_multipath_capacity_aware",
    "arrival_guarded_capacity",
}
SINGLE_PATH_POLICIES = {"single_best_path"}
COORDINATED_ADAPTIVE_DEFAULT_POLICY: SchedulerPolicy = "capacity_aware"
ARRIVAL_GUARD_DEFAULT_BUDGET_US = 150_000
ARRIVAL_GUARD_QUEUE_PACKET_US = 50
ARRIVAL_GUARD_DRAIN_WEIGHT = 1

logger = get_logger(__name__)

SERVICE_PRIORITY_VALUES: dict[ServicePriority, int] = {
    "bulk": 50,
    "normal": 100,
    "high": 200,
    "critical": 400,
}


def compile_service_priority(priority: ServicePriority) -> int:
    """Compile service priority labels into stable numeric runtime values."""
    return SERVICE_PRIORITY_VALUES[priority]


def compile_path_scheduler(
    path: PathConfig,
    *,
    index: int,
    config: GatherlinkConfig,
    telemetry: SchedulerTelemetrySnapshot | None = None,
    effective_mode: SchedulerPolicy | None = None,
) -> RuntimePathSchedulerConfig:
    """Compile one path's Python-owned scheduling hints into Rust-ready state."""
    return compile_path_policy(path, index=index, mode=effective_mode or _compilable_mode(config), telemetry=telemetry)


def compile_scheduler(
    config: GatherlinkConfig,
    *,
    telemetry: SchedulerTelemetrySnapshot | None = None,
    effective_mode: SchedulerPolicy | None = None,
) -> RuntimeSchedulerConfig:
    """Compile scheduler policy into a small runtime DTO for Rust execution."""
    # TODO(scheduler-hot-reapply): Feed this function from the path manager's
    # live telemetry loop. The shape is ready now: Python can recompile mode,
    # path state, weights, and primitive limits without moving policy to Rust.
    selected_mode = effective_mode or _compilable_mode(config)
    paths = [
        compile_path_scheduler(path, index=index, config=config, telemetry=telemetry, effective_mode=effective_mode)
        for index, path in enumerate(config.paths)
    ]
    if telemetry is not None:
        paths = _apply_health_guard(paths, config=config, telemetry=telemetry, effective_mode=selected_mode)
    if selected_mode == "latency_guarded_capacity":
        paths = _apply_latency_guard(paths)
    if selected_mode == "arrival_guarded_capacity":
        paths = _apply_arrival_guard(paths)
    if selected_mode == "ordered_multipath_capacity_aware":
        paths = _apply_ordered_arrival_guard(paths)
    if selected_mode in SINGLE_PATH_POLICIES:
        paths = _apply_single_best_path(paths)
    return RuntimeSchedulerConfig(
        mode=rust_mode_for_policy(selected_mode),
        paths=paths,
    )


def default_effective_scheduler_policy(config: GatherlinkConfig) -> SchedulerPolicy:
    """Return the Python policy used before live coordinator decisions exist."""
    return _compilable_mode(config)


def _compilable_mode(config: GatherlinkConfig) -> SchedulerPolicy:
    """Return a concrete policy when the configured mode is a Python coordinator."""
    mode = config.scheduler.mode
    if mode == "coordinated_adaptive":
        service_traffic = service_traffic_summary(config.services)
        if config.scheduler.traffic_bias == "tcp" or service_traffic.is_tcp_like_only:
            return "single_best_path"
        if config.scheduler.traffic_bias == "udp" or service_traffic.is_udp_bulk_only:
            return "capacity_aware"
        if service_traffic.has_mixed_known_classes:
            return "capacity_aware"
        return COORDINATED_ADAPTIVE_DEFAULT_POLICY
    return mode


def _apply_health_guard(
    paths: list[RuntimePathSchedulerConfig],
    *,
    config: GatherlinkConfig,
    telemetry: SchedulerTelemetrySnapshot,
    effective_mode: SchedulerPolicy,
) -> list[RuntimePathSchedulerConfig]:
    """
    Suppress clearly unhealthy paths before Rust receives execution primitives.

    Python owns the health model. Rust still receives only ordinary primitive
    state and weights: degraded paths lose weight, while down paths move to
    drain so normal scheduling avoids them without pretending the interface no
    longer exists.
    """
    scores = score_snapshot(telemetry.paths)
    guarded: list[RuntimePathSchedulerConfig] = []
    for path, configured_path in zip(paths, config.paths, strict=True):
        score = scores.get(configured_path.name)
        if score is None:
            guarded.append(path)
            continue
        if score.health == "down" and path.state == "active":
            guarded.append(path.model_copy(update={"state": "drain", "weight": 1}))
        elif score.health == "degraded" and path.state == "active" and effective_mode not in CAPACITY_SHARE_POLICIES:
            guarded.append(path.model_copy(update={"weight": max(1, min(path.weight, score.weight))}))
        else:
            guarded.append(path)
    return guarded


def _apply_latency_guard(paths: list[RuntimePathSchedulerConfig]) -> list[RuntimePathSchedulerConfig]:
    """Drain latency outliers before Rust executes capacity-oriented scheduling."""
    known_latencies = [path.latency_us for path in paths if path.enabled and path.latency_us is not None]
    if len(known_latencies) < 2:
        return paths

    fastest_latency_us = min(known_latencies)
    allowed_margin_us = max(LATENCY_GUARD_MIN_MARGIN_US, fastest_latency_us // LATENCY_GUARD_MARGIN_DIVISOR)
    latency_limit_us = fastest_latency_us + allowed_margin_us
    guarded: list[RuntimePathSchedulerConfig] = []
    for path in paths:
        if path.latency_us is not None and path.latency_us > latency_limit_us and path.state == "active":
            guarded.append(path.model_copy(update={"state": "drain"}))
        else:
            guarded.append(path)
    return guarded


def _apply_arrival_guard(paths: list[RuntimePathSchedulerConfig]) -> list[RuntimePathSchedulerConfig]:
    """
    Demote paths predicted to arrive outside the receiver reorder budget.

    This is a Python-owned, MPTCP-inspired guard for single-flow traffic. Rust
    still receives only ordinary weighted scheduling primitives; Python makes
    the semantic decision that a slower path should become drain/probe traffic
    when it would probably create head-of-line blocking at the receiver.
    """
    predictions = [_arrival_prediction_us(path) for path in paths]
    known_predictions = [
        prediction
        for path, prediction in zip(paths, predictions, strict=True)
        if path.enabled and prediction is not None
    ]
    if len(known_predictions) < 2:
        return paths

    fastest_arrival_us = min(known_predictions)
    guarded: list[RuntimePathSchedulerConfig] = []
    for path, prediction in zip(paths, predictions, strict=True):
        if prediction is None or path.state != "active":
            guarded.append(path)
            continue
        budget_us = path.reorder_hold_us or ARRIVAL_GUARD_DEFAULT_BUDGET_US
        if prediction > fastest_arrival_us + budget_us:
            guarded.append(path.model_copy(update={"state": "drain", "weight": ARRIVAL_GUARD_DRAIN_WEIGHT}))
        else:
            guarded.append(path)
    return guarded


def _apply_ordered_arrival_guard(paths: list[RuntimePathSchedulerConfig]) -> list[RuntimePathSchedulerConfig]:
    """
    Drain ordered paths that cannot fit the fastest path's reorder budget.

    This is the TCP-safe version of the arrival guard. The normal arrival guard
    asks whether each path's own receive hold can tolerate its prediction. For
    ordered service flows, the limiting factor is the earliest arriving path:
    if a slower path lands outside that shared window it can stall TCP badly.
    Python makes that semantic decision and Rust still sees only drain/active
    path primitives.
    """
    predictions = [_arrival_prediction_us(path) for path in paths]
    eligible = [
        (index, path, prediction)
        for index, (path, prediction) in enumerate(zip(paths, predictions, strict=True))
        if path.enabled and path.state == "active" and prediction is not None
    ]
    if len(eligible) < 2:
        return paths

    fastest_index, fastest_path, fastest_arrival_us = min(
        eligible,
        key=lambda item: (item[2], item[1].loss_ppm, item[0]),
    )
    budget_us = fastest_path.reorder_hold_us or ARRIVAL_GUARD_DEFAULT_BUDGET_US
    guarded: list[RuntimePathSchedulerConfig] = []
    for index, path, prediction in zip(range(len(paths)), paths, predictions, strict=True):
        if index == fastest_index or prediction is None or path.state != "active":
            guarded.append(path)
            continue
        if prediction > fastest_arrival_us + budget_us:
            guarded.append(path.model_copy(update={"state": "drain", "weight": ARRIVAL_GUARD_DRAIN_WEIGHT}))
        else:
            guarded.append(path)
    return guarded


def _arrival_prediction_us(path: RuntimePathSchedulerConfig) -> int | None:
    """Return a compact estimated arrival time for one path, or None if unknown."""
    if path.latency_us is None:
        return None
    capacity_bps = path.tx_capacity_bps or 0
    if capacity_bps <= 0:
        transmit_us = 0
        queued_transmit_us = 0
    else:
        transmit_us = path.mtu * 8 * 1_000_000 // capacity_bps
        queued_transmit_us = path.queue_depth_bytes * 8 * 1_000_000 // capacity_bps
    queue_us = path.queue_oldest_age_us + queued_transmit_us + path.queue_depth_packets * ARRIVAL_GUARD_QUEUE_PACKET_US
    return path.latency_us + transmit_us + queue_us


def _apply_single_best_path(paths: list[RuntimePathSchedulerConfig]) -> list[RuntimePathSchedulerConfig]:
    """
    Compile a TCP-stable one-path policy without adding Rust policy.

    The chosen path is active with its normal capacity-derived weight. Other
    paths move to drain with weight 1 so control/probe logic can still see them,
    while ordinary service traffic avoids intentional multi-path reordering.
    """
    eligible = [(index, path) for index, path in enumerate(paths) if path.enabled and path.state == "active"]
    if len(eligible) < 2:
        return paths

    best_index, _best_path = min(
        eligible,
        key=lambda item: (
            -(item[1].tx_capacity_bps or 0),
            item[1].latency_us if item[1].latency_us is not None else 2**32 - 1,
            item[1].loss_ppm,
            item[0],
        ),
    )
    guarded: list[RuntimePathSchedulerConfig] = []
    for index, path in enumerate(paths):
        if index == best_index or path.state != "active":
            guarded.append(path)
        else:
            guarded.append(path.model_copy(update={"state": "drain", "weight": 1}))
    return guarded
