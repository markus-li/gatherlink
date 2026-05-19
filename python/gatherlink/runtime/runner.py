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

from gatherlink.carriers import CarrierSupervisor
from gatherlink.config.models import GatherlinkConfig
from gatherlink.config.runtime import RuntimeConfig
from gatherlink.control import ControlCadenceState
from gatherlink.control.announcements import announce_control_metadata
from gatherlink.control.metadata import empty_control_metadata
from gatherlink.control.policy import apply_control_policy_to_dataplane
from gatherlink.control.remote_status import RemoteStatusState, handle_event, send_request_if_due
from gatherlink.control.reserved import drain_reserved_service_events
from gatherlink.dataplane.rust_backend import bind_core_dataplane
from gatherlink.dataplane.status import (
    merge_control_metadata,
    merge_disabled_service_errors,
    named_rust_control_metadata,
    named_rust_path_stats,
)
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.protocol import SERVICE_ID_CONTROL_METADATA, SERVICE_ID_REMOTE_STATUS
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
    security_drops: dict[str, int] | None = None
    control_cadence: ControlCadenceState | None = None
    remote_status: RemoteStatusState | None = None

    def snapshot(self) -> dict[str, object]:
        """Return a service-monitor-friendly status payload."""
        remote_status = self.remote_status.status() if self.remote_status is not None else None
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
        if self.security_drops is not None:
            payload["security_drops"] = self.security_drops
        if self.control_cadence is not None:
            payload["control_cadence"] = self.control_cadence.status()
        if remote_status is not None:
            payload["remote_status"] = remote_status["cache"]
            payload["remote_status_state"] = {key: value for key, value in remote_status.items() if key != "cache"}
        return payload

    def stop(self) -> None:
        """Request a graceful runner stop from IPC or tests."""
        self.stop_event.set()

    def request_control_cadence(self, request: dict[str, object]) -> dict[str, object]:
        """IPC command for temporary monitor-grade control metadata cadence."""
        profile = str(request.get("profile") or "monitor")
        ttl_seconds = float(request.get("ttl_seconds") or 120.0)
        if profile != "monitor":
            raise RuntimeError(f"unsupported control cadence profile: {profile}")
        if self.control_cadence is None:
            self.control_cadence = ControlCadenceState()
        return self.control_cadence.request_monitor_profile(ttl_seconds=ttl_seconds)

    def request_remote_status(self, request: dict[str, object]) -> dict[str, object]:
        """IPC command for temporary read-only remote status over service id 8."""
        ttl_seconds = float(request.get("ttl_seconds") or 120.0)
        if self.remote_status is None:
            self.remote_status = RemoteStatusState()
        return self.remote_status.request(ttl_seconds=ttl_seconds)


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

    carrier_supervisor = CarrierSupervisor(runtime_config)
    runtime_config = carrier_supervisor.start()
    try:
        dataplane = dataplane_factory(runtime_config)
    except BaseException:
        carrier_supervisor.close()
        raise
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
    state.control_cadence = state.control_cadence or ControlCadenceState()
    state.remote_status = state.remote_status or RemoteStatusState()
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
    last_security_drop_packets = 0
    next_counter_snapshot_at = 0.0
    decoded_control_metadata = empty_control_metadata()
    applied_disabled_services: set[str] = set()
    path_names_by_id = {path.scheduler.path_id: path.name for path in runtime_config.paths}
    local_targets_by_service_id = {service.service_id: service.target for service in runtime_config.services}
    peer_names_by_scope = _peer_names_by_scope(runtime_config)
    _apply_reserved_service_schedulers(dataplane)
    next_control_metadata_at = 0.0

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
        if time.monotonic() >= next_control_metadata_at:
            _try_announce_control_metadata(
                dataplane,
                runtime_config,
                decoded_control_metadata,
                include_sink_time=runtime_config.role == "server",
            )
            next_control_metadata_at = time.monotonic() + state.control_cadence.next_interval(
                forwarded_packets + delivered_packets
            )
        try:
            send_request_if_due(dataplane, state.remote_status, peer_scopes=peer_names_by_scope)
        except RuntimeError as exc:
            logger.warning("remote status request skipped", extra={"error": str(exc)})
        if _drain_python_reserved_services(
            dataplane,
            decoded_control_metadata,
            path_names_by_id=path_names_by_id,
            local_targets_by_service_id=local_targets_by_service_id,
            remote_status=state.remote_status,
            peer_names_by_scope=peer_names_by_scope,
            fallback_peer_name=runtime_config.peer or "peer",
            status_provider=state.snapshot,
        ):
            apply_control_policy_to_dataplane(
                dataplane,
                decoded_control_metadata,
                applied_disabled_services=applied_disabled_services,
                logger=logger.warning,
            )
        _refresh_runner_state_from_dataplane(state, dataplane, runtime_config, decoded_control_metadata)
        if diagnostics_bus is not None:
            last_security_drop_packets = _publish_security_drop_diagnostics(
                diagnostics_bus,
                state,
                node=runtime_config.node,
                peer=runtime_config.peer,
                last_packets=last_security_drop_packets,
            )
            current_counter_snapshot = (forwarded_packets, forwarded_bytes, delivered_packets, delivered_bytes)
            if current_counter_snapshot != last_counter_snapshot and time.monotonic() >= next_counter_snapshot_at:
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
                _apply_reserved_service_schedulers(dataplane)
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
    carrier_supervisor.close()
    return result


def _refresh_runner_state_from_dataplane(
    state: CoreRunnerState,
    dataplane: Any,
    runtime_config: RuntimeConfig,
    decoded_control_metadata: dict[str, object] | None = None,
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
    if decoded_control_metadata is not None and _has_decoded_control_metadata(decoded_control_metadata):
        merge_control_metadata(control_metadata, decoded_control_metadata)
    state.control_metadata = control_metadata
    security_drops = snapshot.get("security_drops")
    if isinstance(security_drops, dict):
        state.security_drops = {
            "packets": int(security_drops.get("packets", 0) or 0),
            "bytes": int(security_drops.get("bytes", 0) or 0),
        }


def _publish_security_drop_diagnostics(
    diagnostics_bus: DiagnosticsBus,
    state: CoreRunnerState,
    *,
    node: str,
    peer: str | None,
    last_packets: int,
) -> int:
    """
    Emit a structured sample when Rust reports new silent security drops.

    Rust only exposes aggregate counters so malformed/auth/replay failures stay
    indistinguishable on the wire. Python adds operator meaning here without
    changing network behavior or pushing policy into the dataplane.
    """
    security_drops = state.security_drops
    if not security_drops:
        return last_packets
    packets = int(security_drops.get("packets", 0) or 0)
    if packets <= last_packets:
        return last_packets
    diagnostics_bus.publish(
        DiagnosticEvent.drop_event(
            code="crypto.auth_failed",
            node=node,
            peer=peer,
            message="transport security silently dropped invalid packets",
            details={
                "packets": packets,
                "bytes": int(security_drops.get("bytes", 0) or 0),
                "delta_packets": packets - last_packets,
                "drop_family": "transport_security",
            },
        )
    )
    return packets


def _has_decoded_control_metadata(control_metadata: dict[str, object]) -> bool:
    """Return whether Python decoded any peer control facts worth overlaying."""
    received = control_metadata.get("received")
    if isinstance(received, dict) and int(received.get("frames") or 0) > 0:
        return True
    for count_name in [
        "path_metadata_count",
        "service_metadata_count",
        "service_endpoint_assertion_count",
        "service_disable_count",
        "service_scheduler_policy_count",
        "path_capacity_count",
        "path_latency_count",
        "path_mtu_count",
        "service_endpoint_mismatch_count",
    ]:
        if int(control_metadata.get(count_name) or 0) > 0:
            return True
    return False


def _drain_python_reserved_services(
    dataplane: Any,
    control_metadata: dict[str, object],
    *,
    path_names_by_id: dict[int, str],
    local_targets_by_service_id: dict[int, str],
    remote_status: RemoteStatusState | None = None,
    peer_names_by_scope: dict[int, str] | None = None,
    fallback_peer_name: str = "peer",
    status_provider: Callable[[], dict[str, object]] | None = None,
) -> int:
    """
    Drain reserved service payloads that Rust intentionally forwards to Python.

    This keeps the production runner on the same boundary as the lab runner:
    Rust recognizes reserved ids and preserves bytes; Python decodes control,
    auth, diagnostics, remote-status, and future reserved services.
    """
    drain = getattr(dataplane, "drain_reserved_service_events", None)
    if not callable(drain):
        return 0
    extra_handlers = {}
    if remote_status is not None and status_provider is not None:
        extra_handlers[SERVICE_ID_REMOTE_STATUS] = lambda event: handle_event(
            event,
            dataplane=dataplane,
            state=remote_status,
            peer_name=_peer_name_for_event(event, peer_names_by_scope or {}, fallback_peer_name),
            status_provider=status_provider,
            logger=logger.warning,
        )
    return drain_reserved_service_events(
        dataplane,
        control_metadata,
        path_names_by_id=path_names_by_id,
        local_targets_by_service_id=local_targets_by_service_id,
        extra_handlers=extra_handlers,
        logger=logger.warning,
    )


def _peer_names_by_scope(runtime_config: RuntimeConfig) -> dict[int, str]:
    """Return authenticated peer/session labels keyed by Rust peer scope."""
    if runtime_config.security.sessions:
        return {
            int(session.local_receiver_index): session.name or f"receiver:{session.local_receiver_index}"
            for session in runtime_config.security.sessions
        }
    peer = runtime_config.peer
    local_receiver_index = runtime_config.security.local_receiver_index
    if peer and local_receiver_index is not None:
        return {int(local_receiver_index): peer}
    return {}


def _peer_name_for_event(event: Any, peer_names_by_scope: dict[int, str], fallback_peer_name: str) -> str:
    """Resolve a monitor-friendly peer name for a reserved service event."""
    peer_scope = getattr(event, "peer_scope", None)
    if callable(peer_scope):
        peer_scope = peer_scope()
    if peer_scope is None:
        return fallback_peer_name
    return peer_names_by_scope.get(int(peer_scope), f"peer:{peer_scope}")


def _apply_reserved_service_schedulers(dataplane: Any) -> None:
    """Compile Python-owned reserved-service scheduling into Rust primitives."""
    setter = getattr(dataplane, "set_service_scheduler", None)
    if not callable(setter):
        return
    setter(SERVICE_ID_CONTROL_METADATA, 0, 0)
    setter(SERVICE_ID_REMOTE_STATUS, 1, 0)


def _try_announce_control_metadata(
    dataplane: Any,
    runtime_config: RuntimeConfig,
    control_metadata: dict[str, object],
    *,
    include_sink_time: bool,
) -> None:
    """
    Send sparse discovery if the current transport state can carry it.

    Shared sink configs may not have a fixed outbound carrier tuple before the
    first authenticated source packet arrives. That is not a fatal service
    failure; Python retries at the normal control cadence after Rust learns
    enough peer/session state to transmit.
    """
    try:
        announce_control_metadata(
            dataplane,
            runtime_config,
            control_metadata,
            include_sink_time=include_sink_time,
        )
    except RuntimeError as exc:
        logger.warning("control metadata announcement skipped", extra={"error": str(exc)})


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
