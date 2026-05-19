"""
Compile Python scheduler decisions into simple Rust runtime weights/rules.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.config.models import GatherlinkConfig, PathConfig, ServicePriority
from gatherlink.config.runtime import RuntimePathSchedulerConfig, RuntimeSchedulerConfig
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


def compile_path_scheduler(path: PathConfig, *, index: int) -> RuntimePathSchedulerConfig:
    """Compile one path's Python-owned scheduling hints into Rust-ready state."""
    state = "disabled" if not path.scheduler.enabled else path.scheduler.state
    return RuntimePathSchedulerConfig(
        path_id=index,
        route_id=0,
        enabled=path.scheduler.enabled and state != "disabled",
        state=state,
        weight=path.scheduler.weight,
        mtu=path.scheduler.mtu,
    )


def compile_scheduler(config: GatherlinkConfig) -> RuntimeSchedulerConfig:
    """Compile scheduler policy into a small runtime DTO for Rust execution."""
    # TODO: Add weighted/adaptive modes here once Python has trustworthy path
    # metrics. Rust should only receive the selected mode and precompiled path
    # execution state.
    return RuntimeSchedulerConfig(
        mode="round_robin",
        paths=[compile_path_scheduler(path, index=index) for index, path in enumerate(config.paths)],
    )
