"""Hot runtime reapply helpers owned by the Python control plane."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any

from gatherlink.config.expansion import compile_service_scheduler_primitives
from gatherlink.config.models import GatherlinkConfig, SchedulerPolicy
from gatherlink.config.runtime import RuntimeConfig, RuntimePathSchedulerConfig, RuntimeServiceConfig
from gatherlink.dataplane.rust_backend import reapply_core_scheduler
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.scheduling.compiler import compile_scheduler
from gatherlink.scheduling.congestion import CongestionFairnessController, compile_congestion_fairness
from gatherlink.scheduling.coordinator import SchedulerPolicyCoordinator
from gatherlink.scheduling.metrics import (
    PathSchedulerMetrics,
    SchedulerTelemetrySnapshot,
    scheduler_metrics_from_control_metadata,
)
from gatherlink.scheduling.scoring import PathScore, score_snapshot
from gatherlink.scheduling.service_intent import service_traffic_summary_from_status
from gatherlink.scheduling.service_paths import ServicePathAllocator
from gatherlink.scheduling.smoothing import SchedulerTelemetrySmoother
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

ReapplyFunction = Callable[[Any, RuntimeConfig], Any]


def recompile_runtime_from_status(
    config: GatherlinkConfig,
    runtime_config: RuntimeConfig,
    status: dict[str, object],
    *,
    telemetry_smoother: SchedulerTelemetrySmoother | None = None,
    scheduler_coordinator: SchedulerPolicyCoordinator | None = None,
    service_path_allocator: ServicePathAllocator | None = None,
    congestion_controller: CongestionFairnessController | None = None,
) -> RuntimeConfig:
    """
    Rebuild the Rust runtime scheduler from one structured service status snapshot.

    The important boundary is that status interpretation stays here in Python.
    Rust receives only refreshed primitive fields such as weights, capacities,
    latency, loss, MTU, state, and in-flight limits.
    """
    telemetry = scheduler_telemetry_from_status(status, runtime_config=runtime_config)
    if telemetry_smoother is not None:
        telemetry = telemetry_smoother.smooth(telemetry)
    service_traffic = service_traffic_summary_from_status(runtime_config.services, status)
    effective_mode = (
        scheduler_coordinator.choose_effective_mode(config, telemetry, service_traffic=service_traffic)
        if scheduler_coordinator is not None
        else None
    )
    compiled_scheduler = compile_scheduler(
        config,
        telemetry=telemetry,
        effective_mode=effective_mode,
        congestion_controller=congestion_controller,
    )
    scheduler_paths = _runtime_scheduler_paths(
        compiled_scheduler.paths,
        runtime_config=runtime_config,
    )
    compiled_scheduler = compiled_scheduler.model_copy(update={"paths": scheduler_paths})
    runtime_paths = [
        path.model_copy(update={"scheduler": scheduler_paths[index]}) for index, path in enumerate(runtime_config.paths)
    ]
    runtime_services = _runtime_services_with_scheduler_primitives(
        config,
        runtime_config=runtime_config,
        effective_mode=effective_mode,
    )
    if service_path_allocator is not None:
        interim_runtime = runtime_config.model_copy(
            update={
                "scheduler": compiled_scheduler,
                "paths": runtime_paths,
                "services": runtime_services,
            }
        )
        service_path_decision = service_path_allocator.update(
            config,
            interim_runtime,
            telemetry,
            status,
            now=monotonic(),
        )
        runtime_services = service_path_decision.services
    return runtime_config.model_copy(
        update={
            "scheduler": compiled_scheduler,
            "paths": runtime_paths,
            "services": runtime_services,
        }
    )


def hot_reapply_scheduler_from_status(
    dataplane: Any,
    config: GatherlinkConfig,
    runtime_config: RuntimeConfig,
    status: dict[str, object],
    *,
    reapply: ReapplyFunction = reapply_core_scheduler,
    telemetry_smoother: SchedulerTelemetrySmoother | None = None,
    scheduler_coordinator: SchedulerPolicyCoordinator | None = None,
    service_path_allocator: ServicePathAllocator | None = None,
    congestion_controller: CongestionFairnessController | None = None,
) -> RuntimeConfig:
    """
    Recompile scheduler primitives from live status and hot-apply them to Rust.

    The returned runtime config is the new Python-side source of truth for the
    next reapply pass. Callers should replace their stored runtime object only
    after this function succeeds.
    """
    updated = recompile_runtime_from_status(
        config,
        runtime_config,
        status,
        telemetry_smoother=telemetry_smoother,
        scheduler_coordinator=scheduler_coordinator,
        service_path_allocator=service_path_allocator,
        congestion_controller=congestion_controller,
    )
    if _scheduler_runtime_equivalent(runtime_config, updated):
        # Scheduler loops can run frequently during benchmarks and production
        # monitoring. If Python's policy decision compiles to the same Rust
        # primitives, avoid disturbing the dataplane with a no-op hot reapply.
        return updated
    reapply(dataplane, updated)
    _apply_service_schedulers(dataplane, updated.services)
    return updated


def scheduler_decision_event_from_status(
    config: GatherlinkConfig,
    runtime_config: RuntimeConfig,
    status: dict[str, object],
    *,
    scheduler_coordinator: SchedulerPolicyCoordinator | None = None,
    service_path_allocator: ServicePathAllocator | None = None,
) -> DiagnosticEvent:
    """
    Explain the current Python scheduler view as a structured diagnostic event.

    Rust executes only the compact primitives after reapply. This event keeps
    the operator-facing meaning, health labels, and path score reasons in the
    Python control plane where policy belongs.
    """
    telemetry = scheduler_telemetry_from_status(status, runtime_config=runtime_config)
    scores = score_snapshot(telemetry.paths)
    ranked_scores = sorted(scores.values(), key=lambda score: (-score.score, score.path_name))
    selected = _selected_path(ranked_scores)
    runtime_paths_by_name = {path.name: path.scheduler for path in runtime_config.paths}
    details = {
        "mode": runtime_config.scheduler.mode,
        "configured_mode": config.scheduler.mode,
        "congestion_policy": config.scheduler.congestion_policy,
        "selected_path": selected,
        "paths": [_path_decision_details(score, runtime_paths_by_name.get(score.path_name)) for score in ranked_scores],
        "service_traffic": service_traffic_summary_from_status(runtime_config.services, status).export_dict(),
    }
    congestion = _congestion_decision_details(config, runtime_config, telemetry)
    if congestion:
        details["congestion_fairness"] = congestion
    if scheduler_coordinator is not None and scheduler_coordinator.last_decision is not None:
        details["coordinator"] = scheduler_coordinator.last_decision.export_dict()
        details["coordinator_recent"] = scheduler_coordinator.recent_decisions()[-4:]
    if service_path_allocator is not None and service_path_allocator.last_decision is not None:
        details["service_path_allocator"] = service_path_allocator.last_decision.export_dict()
    return DiagnosticEvent.scheduler_decision(node=runtime_config.node, details=details)


def scheduler_telemetry_from_status(
    status: dict[str, object],
    *,
    runtime_config: RuntimeConfig,
) -> SchedulerTelemetrySnapshot:
    """Build scheduler telemetry from the shared status shape used by monitor and services."""
    default_path_ids = {path.name: path.scheduler.path_id for path in runtime_config.paths}
    control_metadata = status.get("control_metadata")
    if not isinstance(control_metadata, dict):
        control_metadata = {}
    snapshot = scheduler_metrics_from_control_metadata(control_metadata, default_path_ids=default_path_ids)
    path_stats = status.get("path_stats")
    if not isinstance(path_stats, dict):
        return snapshot

    paths = dict(snapshot.paths)
    for path_name, raw_stats in path_stats.items():
        if not isinstance(path_name, str) or not isinstance(raw_stats, dict):
            continue
        existing = paths.get(path_name)
        paths[path_name] = _merge_loss_metrics(
            existing,
            path_name=path_name,
            path_id=default_path_ids.get(path_name, len(paths)),
            stats=raw_stats,
        )
    return SchedulerTelemetrySnapshot(paths=paths)


def _merge_loss_metrics(
    existing: PathSchedulerMetrics | None,
    *,
    path_name: str,
    path_id: int,
    stats: dict[str, object],
) -> PathSchedulerMetrics:
    """Merge cheap local path facts without hiding peer receiver pressure."""
    packets = _int_or_zero(stats.get("packets"))
    missed = _int_or_zero(stats.get("missed_packets")) + _int_or_zero(stats.get("qdisc_dropped_packets"))
    denominator = packets + missed
    loss_ppm = 0 if denominator <= 0 else min(1_000_000, missed * 1_000_000 // denominator)
    queue_depth_packets = _int_or_zero(stats.get("queue_depth_packets"))
    queue_depth_bytes = _int_or_zero(stats.get("queue_depth_bytes"))
    queue_oldest_age_us = _int_or_zero(stats.get("queue_oldest_age_us"))
    send_failures = _int_or_zero(stats.get("send_failed_packets"))
    receive_gaps = _int_or_zero(stats.get("packets_needing_reorder"))
    reorder_depth_packets = _int_or_zero(stats.get("reorder_depth_packets"))
    if reorder_depth_packets == 0:
        reorder_depth_packets = _int_or_zero(stats.get("reordered_packets"))
    local_drops = _int_or_zero(stats.get("qdisc_dropped_packets")) + _int_or_zero(stats.get("security_drop_packets"))
    last_tx_at_us = _int_or_zero(stats.get("last_tx_at_us"))
    last_rx_at_us = _int_or_zero(stats.get("last_rx_at_us"))
    last_tx_gap_us = _int_or_zero(stats.get("last_tx_gap_us"))
    last_rx_gap_us = _int_or_zero(stats.get("last_rx_gap_us"))
    scheduler_in_flight_packets = _int_or_zero(stats.get("scheduler_in_flight_packets"))
    scheduler_in_flight_bytes = _int_or_zero(stats.get("scheduler_in_flight_bytes"))
    scheduler_predicted_delivery_us = _int_or_zero(stats.get("scheduler_predicted_delivery_us"))
    reorder_buffer_packets = _int_or_zero(stats.get("reorder_buffer_packets"))
    reorder_buffer_oldest_age_us = _int_or_zero(stats.get("reorder_buffer_oldest_age_us"))
    socket_receive_buffer_bytes = _int_or_zero(stats.get("socket_receive_buffer_bytes"))
    socket_send_buffer_bytes = _int_or_zero(stats.get("socket_send_buffer_bytes"))
    socket_drain_quantum = _int_or_zero(stats.get("socket_drain_quantum"))
    updates = {
        "loss_ppm": loss_ppm,
        "queue_depth_packets": queue_depth_packets,
        "queue_depth_bytes": queue_depth_bytes,
        "queue_oldest_age_us": queue_oldest_age_us,
        "send_failures": send_failures,
        "receive_gaps": receive_gaps,
        "reorder_depth_packets": reorder_depth_packets,
        "local_drops": local_drops,
        "last_tx_at_us": last_tx_at_us,
        "last_rx_at_us": last_rx_at_us,
        "last_tx_gap_us": last_tx_gap_us,
        "last_rx_gap_us": last_rx_gap_us,
        "scheduler_in_flight_packets": scheduler_in_flight_packets,
        "scheduler_in_flight_bytes": scheduler_in_flight_bytes,
        "scheduler_predicted_delivery_us": scheduler_predicted_delivery_us,
        "reorder_buffer_packets": reorder_buffer_packets,
        "reorder_buffer_oldest_age_us": reorder_buffer_oldest_age_us,
        "socket_receive_buffer_bytes": socket_receive_buffer_bytes,
        "socket_send_buffer_bytes": socket_send_buffer_bytes,
        "socket_drain_quantum": socket_drain_quantum,
        "observed_packets": packets,
    }
    if existing is None:
        return PathSchedulerMetrics(path_name=path_name, path_id=path_id, **updates)

    # Peer control metadata is the receiver's view of this path. Local path
    # stats are the sender/runtime view. The scheduler needs the pessimistic
    # union of both; otherwise a quiet local queue can erase remote receiver
    # drops, gaps, or queue pressure just before Python recompiles policy.
    updates = {
        "loss_ppm": max(existing.loss_ppm, loss_ppm),
        "queue_depth_packets": max(existing.queue_depth_packets, queue_depth_packets),
        "queue_depth_bytes": max(existing.queue_depth_bytes, queue_depth_bytes),
        "queue_oldest_age_us": max(existing.queue_oldest_age_us, queue_oldest_age_us),
        "send_failures": existing.send_failures + send_failures,
        "receive_gaps": max(existing.receive_gaps, receive_gaps),
        "reorder_depth_packets": max(existing.reorder_depth_packets, reorder_depth_packets),
        "local_drops": existing.local_drops + local_drops,
        "last_tx_at_us": max(existing.last_tx_at_us, last_tx_at_us),
        "last_rx_at_us": max(existing.last_rx_at_us, last_rx_at_us),
        "last_tx_gap_us": max(existing.last_tx_gap_us, last_tx_gap_us),
        "last_rx_gap_us": max(existing.last_rx_gap_us, last_rx_gap_us),
        "scheduler_in_flight_packets": max(existing.scheduler_in_flight_packets, scheduler_in_flight_packets),
        "scheduler_in_flight_bytes": max(existing.scheduler_in_flight_bytes, scheduler_in_flight_bytes),
        "scheduler_predicted_delivery_us": max(
            existing.scheduler_predicted_delivery_us,
            scheduler_predicted_delivery_us,
        ),
        "reorder_buffer_packets": max(existing.reorder_buffer_packets, reorder_buffer_packets),
        "reorder_buffer_oldest_age_us": max(existing.reorder_buffer_oldest_age_us, reorder_buffer_oldest_age_us),
        "socket_receive_buffer_bytes": max(existing.socket_receive_buffer_bytes, socket_receive_buffer_bytes),
        "socket_send_buffer_bytes": max(existing.socket_send_buffer_bytes, socket_send_buffer_bytes),
        "socket_drain_quantum": max(existing.socket_drain_quantum, socket_drain_quantum),
        "observed_packets": max(existing.observed_packets, packets),
    }
    return existing.model_copy(update=updates)


def _runtime_scheduler_paths(
    compiled_paths: list[RuntimePathSchedulerConfig],
    *,
    runtime_config: RuntimeConfig,
) -> list[RuntimePathSchedulerConfig]:
    """Preserve runtime-only path facts while refreshing Python-compiled primitives."""
    output: list[RuntimePathSchedulerConfig] = []
    for index, compiled in enumerate(compiled_paths):
        current = runtime_config.paths[index].scheduler
        mtu = compiled.mtu - runtime_config.security.packet_overhead
        output.append(
            compiled.model_copy(
                update={
                    # The expansion layer has already selected the compact
                    # runtime path id. Preserve it here so a hot
                    # scheduler reapply cannot accidentally renumber live paths.
                    "path_id": current.path_id,
                    "mtu": mtu,
                }
            )
        )
    return output


def _runtime_services_with_scheduler_primitives(
    config: GatherlinkConfig,
    *,
    runtime_config: RuntimeConfig,
    effective_mode: SchedulerPolicy | None,
) -> list[RuntimeServiceConfig]:
    """
    Refresh service-level Rust primitives after Python chooses a policy.

    Path scheduler hot reapply is not enough for flowlet-style policies. Rust
    executes service primitives cheaply, but Python owns when those primitives
    are active and how user-facing policy names map to them.
    """
    output: list[RuntimeServiceConfig] = []
    for service_config, runtime_service in zip(config.services, runtime_config.services, strict=True):
        primitives = compile_service_scheduler_primitives(
            config,
            service_config,
            effective_scheduler_mode=effective_mode,
        )
        output.append(runtime_service.model_copy(update=primitives))
    return output


def _apply_service_schedulers(dataplane: Any, services: list[RuntimeServiceConfig]) -> None:
    """Hot-apply Python-compiled service scheduler primitives when Rust exposes the hook."""
    setter = getattr(dataplane, "set_service_scheduler", None)
    if not callable(setter):
        return
    for service in services:
        setter(
            service.service_id,
            service.scheduler_fanout,
            service.scheduler_fanout_below_bytes,
            service.scheduler_flowlet_idle_us,
            service.scheduler_flowlet_max_hold_us,
            service.scheduler_path_run_datagrams,
            service.scheduler_path_policy,
            service.scheduler_allowed_path_ids,
            service.scheduler_path_weights,
        )


def _scheduler_runtime_equivalent(current: RuntimeConfig, updated: RuntimeConfig) -> bool:
    """Return whether hot reapply would send identical scheduler primitives."""
    if current.scheduler.model_dump() != updated.scheduler.model_dump():
        return False
    if len(current.paths) != len(updated.paths) or len(current.services) != len(updated.services):
        return False
    for current_path, updated_path in zip(current.paths, updated.paths, strict=True):
        if current_path.scheduler.model_dump() != updated_path.scheduler.model_dump():
            return False
    for current_service, updated_service in zip(current.services, updated.services, strict=True):
        if _service_scheduler_tuple(current_service) != _service_scheduler_tuple(updated_service):
            return False
    return True


def _service_scheduler_tuple(
    service: RuntimeServiceConfig,
) -> tuple[int, int, int, int, int, str, tuple[int, ...], tuple[tuple[int, int], ...]]:
    """Return only the service primitives that Rust can hot-reapply."""
    return (
        service.scheduler_fanout,
        service.scheduler_fanout_below_bytes,
        service.scheduler_flowlet_idle_us,
        service.scheduler_flowlet_max_hold_us,
        service.scheduler_path_run_datagrams,
        service.scheduler_path_policy,
        tuple(service.scheduler_allowed_path_ids),
        tuple(service.scheduler_path_weights),
    )


def _selected_path(scores: list[PathScore]) -> str | None:
    """Return the highest-scored non-down path, if Python has one."""
    for score in scores:
        if score.health != "down":
            return score.path_name
    return scores[0].path_name if scores else None


def _path_decision_details(
    score: PathScore,
    runtime_scheduler: RuntimePathSchedulerConfig | None,
) -> dict[str, object]:
    """Merge Python score reasons with the Rust primitive values currently in force."""
    details = score.export_dict()
    if runtime_scheduler is not None:
        details.update(
            {
                "state": runtime_scheduler.state,
                "enabled": runtime_scheduler.enabled,
                "compiled_weight": runtime_scheduler.weight,
                "tx_capacity_bps": runtime_scheduler.tx_capacity_bps,
                "rx_capacity_bps": runtime_scheduler.rx_capacity_bps,
                "latency_us": runtime_scheduler.latency_us,
                "loss_ppm": runtime_scheduler.loss_ppm,
                "queue_depth_packets": runtime_scheduler.queue_depth_packets,
                "queue_depth_bytes": runtime_scheduler.queue_depth_bytes,
                "queue_oldest_age_us": runtime_scheduler.queue_oldest_age_us,
                "max_in_flight_packets": runtime_scheduler.max_in_flight_packets,
                "max_in_flight_bytes": runtime_scheduler.max_in_flight_bytes,
                "pacing_budget_bps": runtime_scheduler.pacing_budget_bps,
            }
        )
    return details


def _congestion_decision_details(
    config: GatherlinkConfig,
    runtime_config: RuntimeConfig,
    telemetry: SchedulerTelemetrySnapshot,
) -> list[dict[str, object]]:
    """Return operator-facing self-limiting facts for scheduler diagnostics."""
    if config.scheduler.congestion_policy == "off":
        return []
    details: list[dict[str, object]] = []
    runtime_paths_by_name = {path.name: path for path in runtime_config.paths}
    configured_paths_by_name = {path.name: path for path in config.paths}
    for path_name, metrics in sorted(telemetry.paths.items()):
        runtime_path = runtime_paths_by_name.get(path_name)
        configured_path = configured_paths_by_name.get(path_name)
        if runtime_path is None or configured_path is None:
            continue
        if configured_path.scheduler.pacing_budget_bps:
            details.append(
                {
                    "path": path_name,
                    "policy": config.scheduler.congestion_policy,
                    "pressure_level": "explicit",
                    "reason": "explicit_pacing_budget",
                    "pacing_budget_bps": runtime_path.scheduler.pacing_budget_bps,
                    "capacity_bps": runtime_path.scheduler.tx_capacity_bps,
                }
            )
            continue
        decision = compile_congestion_fairness(
            metrics,
            policy=config.scheduler.congestion_policy,
            capacity_bps=runtime_path.scheduler.tx_capacity_bps,
        )
        if decision.pressure_level <= 0 and runtime_path.scheduler.pacing_budget_bps <= 0:
            continue
        reason = decision.reason
        if decision.pressure_level <= 0 and runtime_path.scheduler.pacing_budget_bps > 0:
            reason = "held_for_recovery_hysteresis"
        elif (
            runtime_path.scheduler.pacing_budget_bps
            and runtime_path.scheduler.pacing_budget_bps != decision.pacing_budget_bps
        ):
            reason = f"stable_{reason}"
        details.append(
            {
                "path": path_name,
                "policy": config.scheduler.congestion_policy,
                "pressure_level": decision.pressure_level,
                "reason": reason,
                "pacing_budget_bps": runtime_path.scheduler.pacing_budget_bps or decision.pacing_budget_bps,
                "capacity_bps": runtime_path.scheduler.tx_capacity_bps,
                "loss_ppm": metrics.loss_ppm,
                "queue_depth_packets": metrics.queue_depth_packets,
                "queue_oldest_age_us": metrics.queue_oldest_age_us,
                "send_failures": metrics.send_failures,
                "local_drops": metrics.local_drops,
            }
        )
    return details


def _int_or_zero(value: object) -> int:
    """Convert loose status counters to an integer without trusting their source."""
    if value is None:
        return 0
    try:
        converted = int(value)
    except (TypeError, ValueError):
        logger.warning("scheduler status counter ignored non-integer value", extra={"value": repr(value)})
        return 0
    return max(0, converted)
