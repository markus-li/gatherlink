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
    latency_source: str | None = None
    latency_confidence: str | None = None
    latency_clock_error_us: int | None = Field(default=None, ge=0)
    latency_rtt_us: int | None = Field(default=None, ge=0)
    loss_ppm: int = Field(default=0, ge=0, le=1_000_000)
    tx_jitter_us: int | None = Field(default=None, ge=0)
    rx_jitter_us: int | None = Field(default=None, ge=0)
    tx_p95_us: int | None = Field(default=None, ge=0)
    rx_p95_us: int | None = Field(default=None, ge=0)
    queue_depth_packets: int = Field(default=0, ge=0)
    queue_depth_bytes: int = Field(default=0, ge=0)
    queue_oldest_age_us: int = Field(default=0, ge=0)
    send_failures: int = Field(default=0, ge=0)
    receive_gaps: int = Field(default=0, ge=0)
    reorder_depth_packets: int = Field(default=0, ge=0)
    local_drops: int = Field(default=0, ge=0)
    scheduler_in_flight_packets: int = Field(default=0, ge=0)
    scheduler_in_flight_bytes: int = Field(default=0, ge=0)
    scheduler_predicted_delivery_us: int = Field(default=0, ge=0)
    reorder_buffer_packets: int = Field(default=0, ge=0)
    reorder_buffer_oldest_age_us: int = Field(default=0, ge=0)
    socket_receive_buffer_bytes: int = Field(default=0, ge=0)
    socket_send_buffer_bytes: int = Field(default=0, ge=0)
    socket_drain_quantum: int = Field(default=0, ge=0)
    control_tx_frames: int = Field(default=0, ge=0)
    control_rx_frames: int = Field(default=0, ge=0)
    stale_control_age_us: int = Field(default=0, ge=0)
    last_tx_at_us: int = Field(default=0, ge=0)
    last_rx_at_us: int = Field(default=0, ge=0)
    last_tx_gap_us: int = Field(default=0, ge=0)
    last_rx_gap_us: int = Field(default=0, ge=0)
    observed_packets: int = Field(default=0, ge=0)

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

    @property
    def has_trusted_real_data_latency(self) -> bool:
        """Return whether matched real-payload latency is good enough for promotion policy."""
        return self.latency_source == "data-traffic-one-way" and self.latency_confidence == "good"

    def estimated_earliest_delivery_us(self, *, payload_bytes: int = 0) -> int | None:
        """
        Estimate earliest useful delivery from facts, not policy.

        Rust reports the compiled scheduler's current zero-payload prediction
        when it has one. Python can add payload transmit time and fall back to
        queue/in-flight facts for scheduler decisions and diagnostics.
        """
        if self.scheduler_predicted_delivery_us > 0:
            base = self.scheduler_predicted_delivery_us
        else:
            latency = self.scheduler_latency_us
            if latency is None:
                return None
            base = latency + self.queue_oldest_age_us
            capacity = self.tx_capacity_bps or 0
            if capacity > 0:
                pending_bytes = self.queue_depth_bytes + self.scheduler_in_flight_bytes
                base += pending_bytes * 8 * 1_000_000 // capacity
            base += self.queue_depth_packets + self.scheduler_in_flight_packets
        capacity = self.tx_capacity_bps or 0
        if payload_bytes > 0 and capacity > 0:
            base += payload_bytes * 8 * 1_000_000 // capacity
        return base


class SchedulerTelemetrySnapshot(GatherlinkBaseModel):
    """All path facts available when Python recompiles scheduler state."""

    paths: dict[str, PathSchedulerMetrics] = Field(default_factory=dict)


def path_pressure_from_path_stats(path_stats: dict[str, dict[str, int]]) -> dict[str, dict[str, int | str | None]]:
    """Convert path counters into generic control-plane pressure facts."""
    from datetime import UTC, datetime

    pressure: dict[str, dict[str, int | str | None]] = {}
    for path_name, stats in path_stats.items():
        packets = max(0, int(stats.get("packets", 0)))
        missed = max(0, int(stats.get("missed_packets", 0))) + max(0, int(stats.get("qdisc_dropped_packets", 0)))
        denominator = packets + missed
        loss_ppm = 0 if denominator <= 0 else min(1_000_000, missed * 1_000_000 // denominator)
        pressure[path_name] = {
            "loss_ppm": loss_ppm,
            "queue_depth_packets": max(0, int(stats.get("queue_depth_packets", 0))),
            "queue_depth_bytes": max(0, int(stats.get("queue_depth_bytes", 0))),
            "queue_oldest_age_us": max(0, int(stats.get("queue_oldest_age_us", 0))),
            "send_failures": max(0, int(stats.get("send_failed_packets", 0))),
            "receive_gaps": max(0, int(stats.get("packets_needing_reorder", 0))),
            "reorder_depth_packets": max(0, int(stats.get("reorder_depth_packets", stats.get("reordered_packets", 0)))),
            "local_drops": max(
                0,
                int(stats.get("qdisc_dropped_packets", 0)) + int(stats.get("security_drop_packets", 0)),
            ),
            "scheduler_in_flight_packets": max(0, int(stats.get("scheduler_in_flight_packets", 0))),
            "scheduler_in_flight_bytes": max(0, int(stats.get("scheduler_in_flight_bytes", 0))),
            "scheduler_predicted_delivery_us": max(0, int(stats.get("scheduler_predicted_delivery_us", 0))),
            "reorder_buffer_packets": max(0, int(stats.get("reorder_buffer_packets", 0))),
            "reorder_buffer_oldest_age_us": max(0, int(stats.get("reorder_buffer_oldest_age_us", 0))),
            "socket_receive_buffer_bytes": max(0, int(stats.get("socket_receive_buffer_bytes", 0))),
            "socket_send_buffer_bytes": max(0, int(stats.get("socket_send_buffer_bytes", 0))),
            "socket_drain_quantum": max(0, int(stats.get("socket_drain_quantum", 0))),
            "last_tx_at_us": max(0, int(stats.get("last_tx_at_us", 0))),
            "last_rx_at_us": max(0, int(stats.get("last_rx_at_us", 0))),
            "last_tx_gap_us": max(0, int(stats.get("last_tx_gap_us", 0))),
            "last_rx_gap_us": max(0, int(stats.get("last_rx_gap_us", 0))),
            "observed_packets": packets,
            "source": "local-path-stats",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    return pressure


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
    pressure = control_metadata.get("path_pressure")
    if not isinstance(capacity, dict):
        capacity = {}
    if not isinstance(latency, dict):
        latency = {}
    if not isinstance(pressure, dict):
        pressure = {}
    path_control = control_metadata.get("path_control")
    if not isinstance(path_control, dict):
        path_control = {}

    path_names = set(default_path_ids)
    path_names.update(str(name) for name in capacity.keys())
    path_names.update(str(name) for name in latency.keys())
    path_names.update(str(name) for name in pressure.keys())

    metrics: dict[str, PathSchedulerMetrics] = {}
    for path_name in sorted(path_names):
        capacity_record = capacity.get(path_name)
        latency_record = latency.get(path_name)
        pressure_record = pressure.get(path_name)
        if not isinstance(capacity_record, dict):
            capacity_record = {}
        if not isinstance(latency_record, dict):
            latency_record = {}
        if not isinstance(pressure_record, dict):
            pressure_record = {}
        path_control_record = path_control.get(path_name)
        if not isinstance(path_control_record, dict):
            path_control_record = {}
        control_tx = path_control_record.get("tx")
        control_rx = path_control_record.get("rx")
        if not isinstance(control_tx, dict):
            control_tx = {}
        if not isinstance(control_rx, dict):
            control_rx = {}

        metrics[path_name] = PathSchedulerMetrics(
            path_name=path_name,
            path_id=int(default_path_ids.get(path_name, len(metrics))),
            tx_capacity_bps=_optional_int(capacity_record.get("tx_bps")),
            rx_capacity_bps=_optional_int(capacity_record.get("rx_bps")),
            tx_latency_current_us=_optional_int(latency_record.get("tx_current_us")),
            tx_latency_mean_us=_optional_int(latency_record.get("tx_mean_us")),
            rx_latency_current_us=_optional_int(latency_record.get("rx_current_us")),
            rx_latency_mean_us=_optional_int(latency_record.get("rx_mean_us")),
            latency_source=_optional_str(latency_record.get("source")),
            latency_confidence=_optional_str(latency_record.get("confidence")),
            latency_clock_error_us=_optional_int(latency_record.get("clock_error_us")),
            latency_rtt_us=_optional_int(latency_record.get("rtt_us")),
            tx_jitter_us=_optional_int(latency_record.get("tx_jitter_us")),
            rx_jitter_us=_optional_int(latency_record.get("rx_jitter_us")),
            tx_p95_us=_optional_int(latency_record.get("tx_p95_us")),
            rx_p95_us=_optional_int(latency_record.get("rx_p95_us")),
            loss_ppm=min(_optional_int(pressure_record.get("loss_ppm")) or 0, 1_000_000),
            queue_depth_packets=_optional_int(pressure_record.get("queue_depth_packets")) or 0,
            queue_depth_bytes=_optional_int(pressure_record.get("queue_depth_bytes")) or 0,
            queue_oldest_age_us=_optional_int(pressure_record.get("queue_oldest_age_us")) or 0,
            send_failures=_optional_int(pressure_record.get("send_failures")) or 0,
            receive_gaps=_optional_int(pressure_record.get("receive_gaps")) or 0,
            reorder_depth_packets=_optional_int(pressure_record.get("reorder_depth_packets")) or 0,
            local_drops=_optional_int(pressure_record.get("local_drops")) or 0,
            scheduler_in_flight_packets=_optional_int(pressure_record.get("scheduler_in_flight_packets")) or 0,
            scheduler_in_flight_bytes=_optional_int(pressure_record.get("scheduler_in_flight_bytes")) or 0,
            scheduler_predicted_delivery_us=_optional_int(pressure_record.get("scheduler_predicted_delivery_us")) or 0,
            reorder_buffer_packets=_optional_int(pressure_record.get("reorder_buffer_packets")) or 0,
            reorder_buffer_oldest_age_us=_optional_int(pressure_record.get("reorder_buffer_oldest_age_us")) or 0,
            socket_receive_buffer_bytes=_optional_int(pressure_record.get("socket_receive_buffer_bytes")) or 0,
            socket_send_buffer_bytes=_optional_int(pressure_record.get("socket_send_buffer_bytes")) or 0,
            socket_drain_quantum=_optional_int(pressure_record.get("socket_drain_quantum")) or 0,
            control_tx_frames=_optional_int(control_tx.get("frames")) or 0,
            control_rx_frames=_optional_int(control_rx.get("frames")) or 0,
            stale_control_age_us=_optional_int(pressure_record.get("stale_control_age_us")) or 0,
            last_tx_at_us=_optional_int(pressure_record.get("last_tx_at_us")) or 0,
            last_rx_at_us=_optional_int(pressure_record.get("last_rx_at_us")) or 0,
            last_tx_gap_us=_optional_int(pressure_record.get("last_tx_gap_us")) or 0,
            last_rx_gap_us=_optional_int(pressure_record.get("last_rx_gap_us")) or 0,
            observed_packets=_optional_int(pressure_record.get("observed_packets")) or 0,
        )

    return SchedulerTelemetrySnapshot(paths=metrics)


def _optional_int(value: object) -> int | None:
    """Convert loose status metadata into an optional integer."""
    if value is None:
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        logger.warning("scheduler telemetry ignored non-integer value", extra={"value": repr(value)})
        return None
    if converted < 0:
        logger.warning("scheduler telemetry ignored negative value", extra={"value": converted})
        return None
    return converted


def _optional_str(value: object) -> str | None:
    """Convert loose status metadata into a non-empty optional string."""
    if value is None:
        return None
    converted = str(value).strip()
    return converted or None
