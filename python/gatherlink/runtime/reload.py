"""Hot runtime reapply helpers owned by the Python control plane."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gatherlink.config.models import GatherlinkConfig
from gatherlink.config.runtime import RuntimeConfig, RuntimePathSchedulerConfig
from gatherlink.dataplane.rust_backend import reapply_core_scheduler
from gatherlink.scheduling.compiler import compile_scheduler
from gatherlink.scheduling.metrics import (
    PathSchedulerMetrics,
    SchedulerTelemetrySnapshot,
    scheduler_metrics_from_control_metadata,
)
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

ReapplyFunction = Callable[[Any, RuntimeConfig], Any]


def recompile_runtime_from_status(
    config: GatherlinkConfig,
    runtime_config: RuntimeConfig,
    status: dict[str, object],
) -> RuntimeConfig:
    """
    Rebuild the Rust runtime scheduler from one structured service status snapshot.

    The important boundary is that status interpretation stays here in Python.
    Rust receives only refreshed primitive fields such as weights, capacities,
    latency, loss, MTU, state, and in-flight limits.
    """
    telemetry = scheduler_telemetry_from_status(status, runtime_config=runtime_config)
    compiled_scheduler = compile_scheduler(config, telemetry=telemetry)
    scheduler_paths = _runtime_scheduler_paths(
        compiled_scheduler.paths,
        runtime_config=runtime_config,
    )
    compiled_scheduler = compiled_scheduler.model_copy(update={"paths": scheduler_paths})
    runtime_paths = [
        path.model_copy(update={"scheduler": scheduler_paths[index]}) for index, path in enumerate(runtime_config.paths)
    ]
    return runtime_config.model_copy(
        update={
            "scheduler": compiled_scheduler,
            "paths": runtime_paths,
        }
    )


def hot_reapply_scheduler_from_status(
    dataplane: Any,
    config: GatherlinkConfig,
    runtime_config: RuntimeConfig,
    status: dict[str, object],
    *,
    reapply: ReapplyFunction = reapply_core_scheduler,
) -> RuntimeConfig:
    """
    Recompile scheduler primitives from live status and hot-apply them to Rust.

    The returned runtime config is the new Python-side source of truth for the
    next reapply pass. Callers should replace their stored runtime object only
    after this function succeeds.
    """
    updated = recompile_runtime_from_status(config, runtime_config, status)
    reapply(dataplane, updated)
    return updated


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
    """Merge cheap path loss facts into a scheduler metrics record."""
    packets = _int_or_zero(stats.get("packets"))
    missed = _int_or_zero(stats.get("missed_packets")) + _int_or_zero(stats.get("qdisc_dropped_packets"))
    denominator = packets + missed
    loss_ppm = 0 if denominator <= 0 else min(1_000_000, missed * 1_000_000 // denominator)
    if existing is None:
        return PathSchedulerMetrics(path_name=path_name, path_id=path_id, loss_ppm=loss_ppm)
    return existing.model_copy(update={"loss_ppm": loss_ppm})


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
