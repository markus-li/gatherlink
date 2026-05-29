"""
Compile Python scheduler decisions into simple Rust runtime weights/rules.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.config.models import GatherlinkConfig, PathConfig, SchedulerPolicy, ServicePriority
from gatherlink.config.runtime import RuntimePathSchedulerConfig, RuntimeSchedulerConfig
from gatherlink.scheduling.congestion import CongestionFairnessController, compile_congestion_fairness
from gatherlink.scheduling.metrics import PathSchedulerMetrics, SchedulerTelemetrySnapshot
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
ORDERED_RECEIVER_MIN_OBSERVED_PACKETS = 1_000
ORDERED_RECEIVER_GAP_DRAIN_PPM = 5_000
ORDERED_RECEIVER_REORDER_DEPTH_DRAIN_PACKETS = 4_096
ORDERED_RECEIVER_BUFFER_DRAIN_PACKETS = 4_096
ORDERED_RECEIVER_BUFFER_AGE_MULTIPLIER = 2
ORDERED_RECEIVER_IN_FLIGHT_DRAIN_PACKETS = 8_192
ORDERED_RECEIVER_IN_FLIGHT_DRAIN_BYTES = 8 * 1024 * 1024
ORDERED_RECEIVER_PREDICTED_DELIVERY_MULTIPLIER = 3
ORDERED_RECEIVER_AGE_SCORE_UNIT_US = 1_000

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
    congestion_controller: CongestionFairnessController | None = None,
) -> RuntimeSchedulerConfig:
    """Compile scheduler policy into a small runtime DTO for Rust execution."""
    # Scheduler hot-reapply calls this compiler from Python-owned runtime
    # telemetry. Keep this function as the narrow translation point from policy
    # decisions into Rust-executed mode, path state, weights, and primitive
    # limits.
    selected_mode = effective_mode or _compilable_mode(config)
    paths = [
        compile_path_scheduler(path, index=index, config=config, telemetry=telemetry, effective_mode=effective_mode)
        for index, path in enumerate(config.paths)
    ]
    if telemetry is not None:
        paths = _apply_congestion_fairness(
            paths,
            config=config,
            telemetry=telemetry,
            congestion_controller=congestion_controller,
        )
        paths = _apply_health_guard(paths, config=config, telemetry=telemetry, effective_mode=selected_mode)
    if selected_mode == "latency_guarded_capacity":
        paths = _apply_latency_guard(paths)
    if selected_mode == "arrival_guarded_capacity":
        paths = _apply_arrival_guard(paths)
    if selected_mode == "ordered_multipath_capacity_aware":
        paths = _apply_ordered_arrival_guard(paths)
    if selected_mode in {"ordered_multipath", "ordered_multipath_capacity_aware"} and telemetry is not None:
        paths = _apply_ordered_receiver_feedback_guard(paths, config=config, telemetry=telemetry)
    if selected_mode in SINGLE_PATH_POLICIES:
        paths = _apply_single_best_path(paths)
    return RuntimeSchedulerConfig(
        mode=rust_mode_for_policy(selected_mode),
        paths=paths,
    )


def _apply_congestion_fairness(
    paths: list[RuntimePathSchedulerConfig],
    *,
    config: GatherlinkConfig,
    telemetry: SchedulerTelemetrySnapshot,
    congestion_controller: CongestionFairnessController | None = None,
) -> list[RuntimePathSchedulerConfig]:
    """
    Compile path pressure into narrow self-limiting primitives.

    Python owns the fairness model because "be a good network citizen" is
    operator policy. Rust only sees a `pacing_budget_bps` value, and the fast
    path bypasses this primitive when it is zero. Explicit per-path pacing from
    config always wins because it is an operator override, not inference.
    """
    policy = config.scheduler.congestion_policy
    if policy == "off":
        return paths
    guarded: list[RuntimePathSchedulerConfig] = []
    for path, configured_path in zip(paths, config.paths, strict=True):
        if configured_path.scheduler.pacing_budget_bps or path.state != "active":
            guarded.append(path)
            continue
        metrics = telemetry.paths.get(configured_path.name)
        capacity_bps = path.tx_capacity_bps or configured_path.scheduler.tx_capacity_bps
        if metrics is None:
            guarded.append(path)
            continue
        decision = compile_congestion_fairness(metrics, policy=policy, capacity_bps=capacity_bps)
        if congestion_controller is not None:
            decision = congestion_controller.stabilize(configured_path.name, decision)
        if decision.pacing_budget_bps <= 0:
            guarded.append(path)
            continue
        guarded.append(path.model_copy(update={"pacing_budget_bps": decision.pacing_budget_bps}))
    return guarded


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


def _apply_ordered_receiver_feedback_guard(
    paths: list[RuntimePathSchedulerConfig],
    *,
    config: GatherlinkConfig,
    telemetry: SchedulerTelemetrySnapshot,
) -> list[RuntimePathSchedulerConfig]:
    """
    Drain paths that are creating receiver-side ordered-flow pressure.

    Ordered multipath is only useful for TCP-like traffic when the receiver can
    release packets without head-of-line stalls. Rust reports cheap facts such
    as receive gaps and reorder-buffer age; Python decides when that crosses
    from "tighten credit" into "stop scheduling normal ordered traffic here".
    The compiled result is still ordinary path state and weight primitives.
    """
    active_indexes = [index for index, path in enumerate(paths) if path.enabled and path.state == "active"]
    if len(active_indexes) < 2:
        return paths

    pressure_scores: dict[int, int] = {}
    severe_indexes: set[int] = set()
    for index, (path, configured_path) in enumerate(zip(paths, config.paths, strict=True)):
        if index not in active_indexes:
            continue
        metrics = telemetry.paths.get(configured_path.name)
        if metrics is None:
            continue
        score = _ordered_receiver_pressure_score(path, metrics)
        pressure_scores[index] = score
        if _ordered_receiver_pressure_is_severe(path, metrics):
            severe_indexes.add(index)

    if not severe_indexes:
        return paths

    # If every active path is pressured, keep the least-bad path alive. The
    # scheduler must fail soft into one usable path, not compile an empty set.
    if severe_indexes == set(active_indexes):
        keep_index = min(active_indexes, key=lambda index: (pressure_scores.get(index, 0), index))
        severe_indexes.remove(keep_index)

    guarded: list[RuntimePathSchedulerConfig] = []
    for index, path in enumerate(paths):
        if index in severe_indexes:
            guarded.append(path.model_copy(update={"state": "drain", "weight": ARRIVAL_GUARD_DRAIN_WEIGHT}))
        else:
            guarded.append(path)
    return guarded


def _ordered_receiver_pressure_is_severe(
    path: RuntimePathSchedulerConfig,
    metrics: PathSchedulerMetrics,
) -> bool:
    """Return whether receiver feedback is bad enough to drain this path."""
    observed_packets = metrics.observed_packets
    if observed_packets >= ORDERED_RECEIVER_MIN_OBSERVED_PACKETS:
        receive_gap_ppm = metrics.receive_gaps * 1_000_000 // observed_packets
        if receive_gap_ppm >= ORDERED_RECEIVER_GAP_DRAIN_PPM:
            return True
    if metrics.reorder_depth_packets >= ORDERED_RECEIVER_REORDER_DEPTH_DRAIN_PACKETS:
        return True
    if metrics.reorder_buffer_packets >= ORDERED_RECEIVER_BUFFER_DRAIN_PACKETS:
        return True
    reorder_hold_us = path.reorder_hold_us or ARRIVAL_GUARD_DEFAULT_BUDGET_US
    if metrics.scheduler_in_flight_packets >= ORDERED_RECEIVER_IN_FLIGHT_DRAIN_PACKETS:
        return True
    if metrics.scheduler_in_flight_bytes >= ORDERED_RECEIVER_IN_FLIGHT_DRAIN_BYTES:
        return True
    if metrics.scheduler_predicted_delivery_us > reorder_hold_us * ORDERED_RECEIVER_PREDICTED_DELIVERY_MULTIPLIER:
        return True
    return metrics.reorder_buffer_oldest_age_us > reorder_hold_us * ORDERED_RECEIVER_BUFFER_AGE_MULTIPLIER


def _ordered_receiver_pressure_score(
    path: RuntimePathSchedulerConfig,
    metrics: PathSchedulerMetrics,
) -> int:
    """Score ordered receiver pressure so an all-pressured group keeps the least-bad path."""
    observed_packets = metrics.observed_packets
    receive_gap_ppm = 0
    if observed_packets > 0:
        receive_gap_ppm = metrics.receive_gaps * 1_000_000 // observed_packets
    return (
        receive_gap_ppm
        + metrics.reorder_depth_packets
        + metrics.reorder_buffer_packets
        + metrics.scheduler_in_flight_packets
        + metrics.scheduler_in_flight_bytes // 1024
        + metrics.scheduler_predicted_delivery_us // ORDERED_RECEIVER_AGE_SCORE_UNIT_US
        + metrics.reorder_buffer_oldest_age_us // ORDERED_RECEIVER_AGE_SCORE_UNIT_US
    )


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
