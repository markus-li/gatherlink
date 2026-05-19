"""
Foreground Rust-backed core service runner.

The runner is Python-owned orchestration around the Rust dataplane. Python loads
expanded runtime state, owns lifecycle and diagnostics, and calls the narrow
Rust handle to move packets. It does not inspect payloads or reimplement packet
scheduling.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from gatherlink.config.models import GatherlinkConfig
from gatherlink.config.runtime import RuntimeConfig
from gatherlink.dataplane.rust_backend import bind_core_dataplane
from gatherlink.dataplane.status import (
    merge_disabled_service_errors,
    named_rust_control_metadata,
    named_rust_path_stats,
)
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.runtime.plan import runtime_warnings
from gatherlink.runtime.reload import hot_reapply_scheduler_from_status
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)
COUNTER_SNAPSHOT_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class CoreRunnerResult:
    """Summary from a bounded foreground core runner invocation."""

    iterations: int
    forwarded_packets: int
    forwarded_bytes: int
    delivered_packets: int
    delivered_bytes: int


@dataclass
class CoreRunnerState:
    """Live counters and stop signal shared with the service IPC thread."""

    node: str
    security_mode: str
    service_names: list[str]
    stop_event: Event
    running: bool = False
    iterations: int = 0
    forwarded_packets: int = 0
    forwarded_bytes: int = 0
    delivered_packets: int = 0
    delivered_bytes: int = 0
    path_stats: dict[str, dict[str, int]] | None = None
    control_metadata: dict[str, object] | None = None

    def snapshot(self) -> dict[str, object]:
        """Return a service-monitor-friendly status payload."""
        payload: dict[str, object] = {
            "running": self.running and not self.stop_event.is_set(),
            "node": self.node,
            "security_mode": self.security_mode,
            "iterations": self.iterations,
            "services": self.service_names,
            "tx_packets": self.forwarded_packets,
            "tx_bytes": self.forwarded_bytes,
            "rx_packets": self.delivered_packets,
            "rx_bytes": self.delivered_bytes,
        }
        if self.path_stats is not None:
            payload["path_stats"] = self.path_stats
        if self.control_metadata is not None:
            payload["control_metadata"] = self.control_metadata
        return payload

    def stop(self) -> None:
        """Request a graceful runner stop from IPC or tests."""
        self.stop_event.set()


DataplaneFactory = Callable[[RuntimeConfig], Any]


def run_core_service(
    runtime_config: RuntimeConfig,
    *,
    dataplane_factory: DataplaneFactory = bind_core_dataplane,
    stop_event: Event | None = None,
    max_iterations: int | None = None,
    batch_size: int = 32,
    diagnostics_bus: DiagnosticsBus | None = None,
    runner_state: CoreRunnerState | None = None,
    source_config: GatherlinkConfig | None = None,
    scheduler_reapply_interval_seconds: float | None = None,
) -> CoreRunnerResult:
    """
    Run a Rust-backed core service loop until stopped.

    ``max_iterations`` is a test and smoke-run escape hatch. Normal services
    pass no limit and are stopped through the Python supervisor or process
    signal path.
    """
    service_names = [service.name for service in runtime_config.services if service.listen]
    if not service_names and not runtime_config.paths:
        raise ValueError("core runner requires at least one service listener or path transport")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    for warning in runtime_warnings(runtime_config):
        logger.warning(warning.removeprefix("WARNING: "))
        if diagnostics_bus is not None:
            diagnostics_bus.publish(DiagnosticEvent.warning(warning.removeprefix("WARNING: ")))

    dataplane = dataplane_factory(runtime_config)
    if diagnostics_bus is not None:
        for service in runtime_config.services:
            if service.listen:
                diagnostics_bus.publish(
                    DiagnosticEvent.service_bound(
                        node=runtime_config.node,
                        service=service.name,
                        listen=service.listen,
                        target=service.target,
                        details={"security_mode": runtime_config.security.mode},
                    )
                )
    stop_event = stop_event or Event()
    state = runner_state or CoreRunnerState(
        node=runtime_config.node,
        security_mode=runtime_config.security.mode,
        service_names=service_names,
        stop_event=stop_event,
    )
    state.running = True
    next_scheduler_reapply_at = (
        time.monotonic() + scheduler_reapply_interval_seconds
        if source_config is not None and scheduler_reapply_interval_seconds is not None
        else None
    )
    iterations = 0
    forwarded_packets = 0
    forwarded_bytes = 0
    delivered_packets = 0
    delivered_bytes = 0
    last_counter_snapshot: tuple[int, int, int, int] | None = None
    next_counter_snapshot_at = 0.0

    while not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        state.iterations = iterations
        for service_name in service_names:
            try:
                outcomes = _forward_available_for_service(dataplane, service_name, batch_size)
            except Exception as exc:
                if _is_idle_receive_timeout(exc):
                    continue
                raise
            forwarded_packets += len(outcomes)
            forwarded_bytes += sum(int(outcome.payload_len()) for outcome in outcomes)
            state.forwarded_packets = forwarded_packets
            state.forwarded_bytes = forwarded_bytes
        delivered = dataplane.receive_available_from_paths(batch_size)
        delivered_packets += len(delivered)
        delivered_bytes += sum(int(outcome.payload_len()) for outcome in delivered)
        state.delivered_packets = delivered_packets
        state.delivered_bytes = delivered_bytes
        _refresh_runner_state_from_dataplane(state, dataplane, runtime_config)
        if diagnostics_bus is not None:
            current_counter_snapshot = (forwarded_packets, forwarded_bytes, delivered_packets, delivered_bytes)
            if (
                current_counter_snapshot != last_counter_snapshot
                and time.monotonic() >= next_counter_snapshot_at
            ):
                diagnostics_bus.publish(
                    DiagnosticEvent.counter_snapshot(
                        node=runtime_config.node,
                        service=",".join(service_names) if service_names else None,
                        counters={
                            "tx_packets": forwarded_packets,
                            "tx_bytes": forwarded_bytes,
                            "rx_packets": delivered_packets,
                            "rx_bytes": delivered_bytes,
                        },
                    )
                )
                last_counter_snapshot = current_counter_snapshot
                next_counter_snapshot_at = time.monotonic() + COUNTER_SNAPSHOT_INTERVAL_SECONDS
        if (
            next_scheduler_reapply_at is not None
            and scheduler_reapply_interval_seconds is not None
            and time.monotonic() >= next_scheduler_reapply_at
        ):
            try:
                runtime_config = hot_reapply_scheduler_from_status(
                    dataplane,
                    source_config,
                    runtime_config,
                    state.snapshot(),
                )
                if diagnostics_bus is not None:
                    diagnostics_bus.publish(
                        DiagnosticEvent.config_reapplied(
                            node=runtime_config.node,
                            details={"source": "scheduler_status_loop"},
                        )
                    )
            except (TypeError, ValueError, RuntimeError) as exc:
                logger.warning("scheduler hot reapply skipped", extra={"error": str(exc)})
                if diagnostics_bus is not None:
                    diagnostics_bus.publish(
                        DiagnosticEvent.warning(
                            "scheduler hot reapply skipped",
                            details={"error": str(exc), "source": "scheduler_status_loop"},
                        )
                    )
            next_scheduler_reapply_at = time.monotonic() + scheduler_reapply_interval_seconds
        if diagnostics_bus is not None:
            diagnostics_bus.drain(limit=128)

    result = CoreRunnerResult(
        iterations=iterations,
        forwarded_packets=forwarded_packets,
        forwarded_bytes=forwarded_bytes,
        delivered_packets=delivered_packets,
        delivered_bytes=delivered_bytes,
    )
    if diagnostics_bus is not None:
        diagnostics_bus.publish(
            DiagnosticEvent.shutdown(
                node=runtime_config.node,
                reason="runner stopped",
                details=result.__dict__,
            )
        )
        diagnostics_bus.drain()
    state.running = False
    return result


def _refresh_runner_state_from_dataplane(
    state: CoreRunnerState,
    dataplane: Any,
    runtime_config: RuntimeConfig,
) -> None:
    """Copy Rust status counters into the Python-owned status shape when available."""
    status_snapshot = getattr(dataplane, "status_snapshot", None)
    if not callable(status_snapshot):
        return
    snapshot = status_snapshot()
    if not isinstance(snapshot, dict):
        return
    state.path_stats = named_rust_path_stats(snapshot, runtime_config)
    control_metadata = named_rust_control_metadata(snapshot.get("control_metadata", {}), runtime_config)
    disabled_services = snapshot.get("disabled_services")
    if isinstance(disabled_services, dict):
        merge_disabled_service_errors(control_metadata, disabled_services)
    state.control_metadata = control_metadata


def _forward_available_for_service(dataplane: Any, service_name: str, batch_size: int) -> list[Any]:
    """
    Drain one app-facing service without letting a quiet UDP socket stall supervision.

    The Rust dataplane keeps the blocking method for focused smoke tests, but the
    production runner needs the nonblocking shape so IPC status/stop, path receives,
    and scheduler reapply keep moving even when an application has no new datagram.
    """
    nonblocking = getattr(dataplane, "forward_available_for_service_nonblocking", None)
    if callable(nonblocking):
        return nonblocking(service_name, batch_size)
    return dataplane.forward_available_for_service(service_name, batch_size)


def _is_idle_receive_timeout(exc: Exception) -> bool:
    """Return whether Rust reported an idle UDP receive timeout."""
    text = str(exc).lower()
    # Rust maps socket idle timeouts through PyO3 as runtime errors. Treating
    # only receive-timeout wording as idle keeps real dataplane errors loud.
    return "failed to receive udp datagram" in text and (
        "timed out" in text or "would block" in text or "resource temporarily unavailable" in text
    )
