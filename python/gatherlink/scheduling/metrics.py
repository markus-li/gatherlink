"""
Scheduler telemetry models consumed by Python policy.

Rust reports cheap counters and peer metadata. Python converts those facts into
stable path telemetry records, then policy/scoring code compiles them into the
small primitives Rust can execute quickly.
"""

from __future__ import annotations

from pydantic import Field

from gatherlink.shared.logging import get_logger
from gatherlink.shared.models import GatherlinkBaseModel

logger = get_logger(__name__)


class PathSchedulerMetrics(GatherlinkBaseModel):
    """One path's current scheduler-visible facts from the local point of view."""

    path_name: str
    path_id: int
    tx_capacity_bps: int | None = None
    rx_capacity_bps: int | None = None
    tx_latency_current_us: int | None = None
    tx_latency_mean_us: int | None = None
    rx_latency_current_us: int | None = None
    rx_latency_mean_us: int | None = None
    loss_ppm: int = Field(default=0, ge=0, le=1_000_000)
    queue_depth_packets: int = Field(default=0, ge=0)
    queue_depth_bytes: int = Field(default=0, ge=0)
    queue_oldest_age_us: int = Field(default=0, ge=0)

    @property
    def scheduler_latency_us(self) -> int | None:
        """Return the best single latency estimate for Rust's compact primitive."""
        for value in (
            self.tx_latency_mean_us,
            self.tx_latency_current_us,
            self.rx_latency_mean_us,
            self.rx_latency_current_us,
        ):
            if value is not None:
                return value
        return None


class SchedulerTelemetrySnapshot(GatherlinkBaseModel):
    """All path facts available when Python recompiles scheduler state."""

    paths: dict[str, PathSchedulerMetrics] = Field(default_factory=dict)


def scheduler_metrics_from_control_metadata(
    control_metadata: dict[str, object],
    *,
    default_path_ids: dict[str, int] | None = None,
) -> SchedulerTelemetrySnapshot:
    """
    Build scheduler telemetry from the generic service status control shape.

    The service monitor and scheduler intentionally consume the same metadata
    vocabulary. That keeps lab, future production services, and diagnostics from
    growing separate interpretations of capacity or latency.
    """
    default_path_ids = default_path_ids or {}
    capacity = control_metadata.get("path_capacity")
    latency = control_metadata.get("path_latency")
    if not isinstance(capacity, dict):
        capacity = {}
    if not isinstance(latency, dict):
        latency = {}

    path_names = set(default_path_ids)
    path_names.update(str(name) for name in capacity.keys())
    path_names.update(str(name) for name in latency.keys())

    metrics: dict[str, PathSchedulerMetrics] = {}
    for path_name in sorted(path_names):
        capacity_record = capacity.get(path_name)
        latency_record = latency.get(path_name)
        if not isinstance(capacity_record, dict):
            capacity_record = {}
        if not isinstance(latency_record, dict):
            latency_record = {}

        metrics[path_name] = PathSchedulerMetrics(
            path_name=path_name,
            path_id=int(default_path_ids.get(path_name, len(metrics))),
            tx_capacity_bps=_optional_int(capacity_record.get("tx_bps")),
            rx_capacity_bps=_optional_int(capacity_record.get("rx_bps")),
            tx_latency_current_us=_optional_int(latency_record.get("tx_current_us")),
            tx_latency_mean_us=_optional_int(latency_record.get("tx_mean_us")),
            rx_latency_current_us=_optional_int(latency_record.get("rx_current_us")),
            rx_latency_mean_us=_optional_int(latency_record.get("rx_mean_us")),
        )

    return SchedulerTelemetrySnapshot(paths=metrics)


def _optional_int(value: object) -> int | None:
    """Convert loose status metadata into an optional integer."""
    if value is None:
        return None
    return int(value)

