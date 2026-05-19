"""
Compile Python scheduler decisions into simple Rust runtime weights/rules.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.config.models import GatherlinkConfig, PathConfig, ServicePriority
from gatherlink.config.runtime import RuntimePathSchedulerConfig, RuntimeSchedulerConfig
from gatherlink.scheduling.metrics import SchedulerTelemetrySnapshot
from gatherlink.scheduling.policies import compile_path_policy, rust_mode_for_policy
from gatherlink.shared.logging import get_logger

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
) -> RuntimePathSchedulerConfig:
    """Compile one path's Python-owned scheduling hints into Rust-ready state."""
    return compile_path_policy(path, index=index, mode=config.scheduler.mode, telemetry=telemetry)


def compile_scheduler(
    config: GatherlinkConfig,
    *,
    telemetry: SchedulerTelemetrySnapshot | None = None,
) -> RuntimeSchedulerConfig:
    """Compile scheduler policy into a small runtime DTO for Rust execution."""
    # TODO(scheduler-hot-reapply): Feed this function from the path manager's
    # live telemetry loop. The shape is ready now: Python can recompile mode,
    # path state, weights, and primitive limits without moving policy to Rust.
    return RuntimeSchedulerConfig(
        mode=rust_mode_for_policy(config.scheduler.mode),
        paths=[
            compile_path_scheduler(path, index=index, config=config, telemetry=telemetry)
            for index, path in enumerate(config.paths)
        ],
    )
