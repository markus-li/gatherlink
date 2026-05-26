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
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from gatherlink.carriers import CarrierSupervisor
from gatherlink.config.models import GatherlinkConfig
from gatherlink.config.runtime import RuntimeConfig
from gatherlink.control import ControlCadenceState
from gatherlink.control.announcements import announce_control_metadata, announce_path_pinned_clock_sync
from gatherlink.control.auth import decode_auth_crypto_event
from gatherlink.control.metadata import empty_control_metadata, merge_control_path_latency
from gatherlink.control.policy import apply_control_policy_to_dataplane
from gatherlink.control.remote_status import RemoteStatusState, handle_event, send_request_if_due
from gatherlink.control.reserved import drain_reserved_service_events
from gatherlink.dataplane.rust_backend import bind_core_dataplane, reapply_core_dataplane
from gatherlink.dataplane.status import (
    merge_control_metadata,
    merge_disabled_service_errors,
    named_rust_control_metadata,
    named_rust_path_stats,
    named_rust_service_path_stats,
    named_rust_service_stats,
)
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.paths.capacity import (
    PathCapacityDetector,
    initial_path_capacity_estimates,
    merge_capacity_snapshots,
    save_path_capacity_cache,
)
from gatherlink.paths.telemetry import (
    DataTrafficLatencyTracker,
    PathLatencyTracker,
    take_data_transmit_sample_batch,
)
from gatherlink.protocol import SERVICE_ID_AUTH_CRYPTO, SERVICE_ID_CONTROL_METADATA, SERVICE_ID_REMOTE_STATUS
from gatherlink.runtime.plan import runtime_warnings
from gatherlink.runtime.rekey import LiveRekeyRuntimeContext, authenticated_session_from_runtime_config
from gatherlink.runtime.reload import hot_reapply_scheduler_from_status, scheduler_decision_event_from_status
from gatherlink.scheduling.compiler import default_effective_scheduler_policy
from gatherlink.scheduling.congestion import CongestionFairnessController
from gatherlink.scheduling.coordinator import SchedulerPolicyCoordinator
from gatherlink.scheduling.metrics import path_pressure_from_path_stats
from gatherlink.scheduling.service_budget import ServiceBudgetController, ServiceOutcomeSnapshot
from gatherlink.scheduling.service_outcome import service_outcome_snapshot_from_json
from gatherlink.scheduling.service_paths import ServicePathAllocator
from gatherlink.scheduling.service_priority import (
    service_budget_plan,
    service_poll_order,
    uses_service_budget_plan,
)
from gatherlink.scheduling.smoothing import SchedulerTelemetrySmoother
from gatherlink.shared.logging import get_logger
from gatherlink.time.offset import InternalClockSyncClient, InternalClockSyncMessage

logger = get_logger(__name__)
COUNTER_SNAPSHOT_INTERVAL_SECONDS = 1.0
STATUS_SNAPSHOT_INTERVAL_SECONDS = 0.25
REMOTE_STATUS_POLL_INTERVAL_SECONDS = 0.05
DIAGNOSTICS_DRAIN_INTERVAL_SECONDS = 0.10
# Keep the idle poll short enough that low-rate control, WireGuard handshakes,
# and TCP ACKs do not inherit a multi-millisecond delay from Python
# orchestration. Rust still performs packet work in bounded nonblocking bursts;
# this sleep only applies once a whole loop observes no dataplane/control work.
IDLE_SLEEP_SECONDS = 0.001
DATAPLANE_BURST_CYCLES = 64
DEFAULT_DATAPLANE_BATCH_SIZE = 512
DATA_TIMING_SAMPLE_CONTROL_INTERVAL_SECONDS = 1.0
AUTH_CRYPTO_MESSAGE_HISTORY_LIMIT = 16
AUTH_REKEY_EVALUATION_INTERVAL_SECONDS = 1.0


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
    service_config: list[dict[str, object]] = field(default_factory=list)
    running: bool = False
    iterations: int = 0
    forwarded_packets: int = 0
    forwarded_bytes: int = 0
    delivered_packets: int = 0
    delivered_bytes: int = 0
    service_stats: dict[str, dict[str, int]] | None = None
    service_path_stats: dict[str, dict[str, dict[str, int]]] | None = None
    path_stats: dict[str, dict[str, int]] | None = None
    service_budget: dict[str, object] | None = None
    control_metadata: dict[str, object] | None = None
    security_drops: dict[str, int] | None = None
    service_outcomes: list[dict[str, object]] | None = None
    service_outcome_snapshot: ServiceOutcomeSnapshot | None = None
    control_cadence: ControlCadenceState | None = None
    remote_status: RemoteStatusState | None = None
    auth_crypto_messages: list[dict[str, object]] = field(default_factory=list)

    def snapshot(self) -> dict[str, object]:
        """Return a service-monitor-friendly status payload."""
        remote_status = self.remote_status.status() if self.remote_status is not None else None
        payload: dict[str, object] = {
            "running": self.running and not self.stop_event.is_set(),
            "node": self.node,
            "security_mode": self.security_mode,
            "iterations": self.iterations,
            "services": self.service_names,
            "service_config": self.service_config,
            "tx_packets": self.forwarded_packets,
            "tx_bytes": self.forwarded_bytes,
            "rx_packets": self.delivered_packets,
            "rx_bytes": self.delivered_bytes,
        }
        if self.service_stats is not None:
            payload["service_stats"] = self.service_stats
        if self.service_path_stats is not None:
            payload["service_path_stats"] = self.service_path_stats
        if self.path_stats is not None:
            payload["path_stats"] = self.path_stats
        if self.service_budget is not None:
            payload["service_budget"] = self.service_budget
        if self.control_metadata is not None:
            payload["control_metadata"] = self.control_metadata
        if self.security_drops is not None:
            payload["security_drops"] = self.security_drops
        if self.service_outcomes is not None:
            payload["service_outcomes"] = self.service_outcomes
        if self.control_cadence is not None:
            payload["control_cadence"] = self.control_cadence.status()
        if remote_status is not None:
            payload["remote_status"] = remote_status["cache"]
            payload["remote_status_state"] = {key: value for key, value in remote_status.items() if key != "cache"}
        if self.auth_crypto_messages:
            payload["auth_crypto_messages"] = list(self.auth_crypto_messages)
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

    def request_service_outcome(self, request: dict[str, object]) -> dict[str, object]:
        """
        IPC command for Python-owned live service outcome facts.

        Helper or benchmark tooling can report facts such as protected TCP
        degradation here. The runner stores them in memory and the Python budget
        controller consumes them on the normal status cadence. Rust receives
        only any primitive drain plan Python later compiles.
        """
        snapshot = service_outcome_snapshot_from_json(request)
        if snapshot is None:
            raise ValueError("invalid service outcome payload")
        self.service_outcome_snapshot = snapshot
        self.service_outcomes = _service_outcomes_for_status(snapshot)
        return {"outcomes": self.service_outcomes}


DataplaneFactory = Callable[[RuntimeConfig], Any]
DataplaneReapply = Callable[[Any, RuntimeConfig], Any]


def run_core_service(
    runtime_config: RuntimeConfig,
    *,
    dataplane_factory: DataplaneFactory = bind_core_dataplane,
    stop_event: Event | None = None,
    max_iterations: int | None = None,
    batch_size: int = DEFAULT_DATAPLANE_BATCH_SIZE,
    diagnostics_bus: DiagnosticsBus | None = None,
    runner_state: CoreRunnerState | None = None,
    source_config: GatherlinkConfig | None = None,
    scheduler_reapply_interval_seconds: float | None = None,
    live_rekey_context: LiveRekeyRuntimeContext | None = None,
    dataplane_reapply: DataplaneReapply = reapply_core_dataplane,
) -> CoreRunnerResult:
    """
    Run a Rust-backed core service loop until stopped.

    ``max_iterations`` is a test and smoke-run escape hatch. Normal services
    pass no limit and are stopped through the Python supervisor or process
    signal path.
    """
    service_names = [service.name for service in runtime_config.services if service.listen]
    poll_service_names = service_poll_order(runtime_config.services)
    service_budget_controller = ServiceBudgetController()
    service_budget_overrides: dict[str, int] = {}
    service_byte_budget_overrides: dict[str, int] = {}
    poll_service_plan = _service_poll_plan_if_needed(
        runtime_config,
        batch_size,
        service_budget_overrides,
        service_byte_budget_overrides,
    )
    if not service_names and not runtime_config.paths:
        raise ValueError("core runner requires at least one service listener or path transport")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    for warning in runtime_warnings(runtime_config):
        logger.warning(warning.removeprefix("WARNING: "))
        if diagnostics_bus is not None:
            diagnostics_bus.publish(DiagnosticEvent.warning(warning.removeprefix("WARNING: ")))

    carrier_supervisor = CarrierSupervisor(runtime_config, diagnostics=diagnostics_bus)
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
    state.service_config = _service_config_for_status(runtime_config)
    state.running = True
    state.control_cadence = state.control_cadence or ControlCadenceState()
    state.remote_status = state.remote_status or RemoteStatusState()
    next_scheduler_reapply_at = (
        time.monotonic() + scheduler_reapply_interval_seconds
        if source_config is not None and scheduler_reapply_interval_seconds is not None
        else None
    )
    scheduler_telemetry_smoother = SchedulerTelemetrySmoother() if next_scheduler_reapply_at is not None else None
    scheduler_policy_coordinator = SchedulerPolicyCoordinator() if next_scheduler_reapply_at is not None else None
    service_path_allocator = ServicePathAllocator() if next_scheduler_reapply_at is not None else None
    congestion_controller = CongestionFairnessController() if next_scheduler_reapply_at is not None else None
    iterations = 0
    forwarded_packets = 0
    forwarded_bytes = 0
    delivered_packets = 0
    delivered_bytes = 0
    last_counter_snapshot: tuple[int, int, int, int] | None = None
    last_security_drop_packets = 0
    next_counter_snapshot_at = 0.0
    next_status_snapshot_at = 0.0
    next_remote_status_poll_at = 0.0
    next_diagnostics_drain_at = 0.0
    last_service_outcome_signature: tuple[tuple[str, bool, str], ...] | None = None
    decoded_control_metadata = empty_control_metadata()
    applied_disabled_services: set[str] = set()
    path_names_by_id = {path.scheduler.path_id: path.name for path in runtime_config.paths}
    path_names = [path.name for path in runtime_config.paths]
    path_ids = {path.name: path.scheduler.path_id for path in runtime_config.paths}
    clock_sync_client = None if runtime_config.role == "server" else InternalClockSyncClient(path_names)
    clock_sync_responses: list[InternalClockSyncMessage] = []
    path_latency_tracker = PathLatencyTracker(path_names)
    data_traffic_latency_tracker = DataTrafficLatencyTracker(path_names_by_id)
    tx_capacity_detector = PathCapacityDetector(
        path_names=path_names,
        direction="tx",
        initial_estimates=initial_path_capacity_estimates(runtime_config, path_names, direction="tx"),
    )
    rx_capacity_detector = PathCapacityDetector(
        path_names=path_names,
        direction="rx",
        initial_estimates=initial_path_capacity_estimates(runtime_config, path_names, direction="rx"),
    )
    path_capacity = merge_capacity_snapshots(tx_capacity_detector.snapshot(), rx_capacity_detector.snapshot())
    local_targets_by_service_id = {service.service_id: service.target for service in runtime_config.services}
    peer_names_by_scope = _peer_names_by_scope(runtime_config)
    _apply_reserved_service_schedulers(dataplane)
    next_control_metadata_at = 0.0
    next_data_sample_control_at = 0.0
    next_auth_rekey_at = 0.0 if live_rekey_context is not None else None
    pending_data_transmit_samples: list[tuple[int, int, int, int]] = []

    def apply_live_rekey_runtime(result: Any) -> bool:
        """
        Hot-apply a Python-validated replacement session into Rust execution.

        The coordinator has already verified topology, identity, and receiver
        indexes. This runner step is intentionally mechanical: recompile the
        existing runtime DTOs, ask Rust to swap executable AEAD state, and keep
        Python's current session metadata aligned for the next rekey window.
        """
        nonlocal runtime_config, peer_names_by_scope
        if result is None:
            return False
        if getattr(result, "fail_closed", False):
            stop_event.set()
            return False
        if not getattr(result, "applied", False):
            return False
        updated_runtime = result.runtime_config
        dataplane_reapply(dataplane, updated_runtime)
        runtime_config = updated_runtime
        peer_names_by_scope = _peer_names_by_scope(runtime_config)
        if live_rekey_context is not None:
            updated_session = authenticated_session_from_runtime_config(runtime_config)
            if updated_session is not None:
                live_rekey_context.current_session = updated_session
        if diagnostics_bus is not None:
            diagnostics_bus.publish(
                DiagnosticEvent.config_reapplied(
                    node=runtime_config.node,
                    details={"source": "auth_crypto_rekey"},
                )
            )
        return True

    def transmit_live_rekey(outbound: Any) -> None:
        """Send one Python-generated auth/crypto payload through Rust unchanged."""
        if outbound is None:
            return
        transmitted = dataplane.transmit_service_payload(SERVICE_ID_AUTH_CRYPTO, outbound.payload)
        if diagnostics_bus is not None:
            diagnostics_bus.publish(
                DiagnosticEvent.rekey_event(
                    code="rekey.started",
                    message=f"{outbound.message_type.replace('_', ' ')} sent",
                    peer=outbound.peer_node_id,
                    details={
                        "type": outbound.message_type,
                        "bytes": len(outbound.payload),
                        "paths": transmitted,
                    },
                )
            )

    while not stop_event.is_set():
        now = time.monotonic()
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        state.iterations = iterations
        did_dataplane_work = False
        try:
            forwarded_count, forwarded_byte_count, delivered_count, delivered_byte_count = _run_dataplane_available(
                dataplane,
                service_names,
                batch_size,
                poll_service_names=poll_service_names,
                poll_service_plan=poll_service_plan,
            )
        except Exception as exc:
            if _is_idle_receive_timeout(exc):
                forwarded_count = forwarded_byte_count = delivered_count = delivered_byte_count = 0
            else:
                raise
        did_dataplane_work = forwarded_count > 0 or delivered_count > 0
        forwarded_packets += forwarded_count
        forwarded_bytes += forwarded_byte_count
        state.forwarded_packets = forwarded_packets
        state.forwarded_bytes = forwarded_bytes
        delivered_packets += delivered_count
        delivered_bytes += delivered_byte_count
        state.delivered_packets = delivered_packets
        state.delivered_bytes = delivered_bytes
        now = time.monotonic()
        if did_dataplane_work:
            pending_data_transmit_samples.extend(
                data_traffic_latency_tracker.observe_local_samples(
                    _drain_data_timing_samples(dataplane),
                    local_clock_offset_us=_current_clock_offset_us(decoded_control_metadata),
                )
            )
            data_latency_changes = data_traffic_latency_tracker.promote_pending_peer_transmit_samples(
                local_clock_offset_us=_current_clock_offset_us(decoded_control_metadata),
                latency_tracker=path_latency_tracker,
                rtt_us=_current_clock_rtt_us(decoded_control_metadata),
                clock_error_us=_current_clock_error_us(decoded_control_metadata),
            )
            if data_latency_changes:
                merge_control_path_latency(decoded_control_metadata, data_latency_changes)
        if now >= next_control_metadata_at:
            _refresh_runner_state_from_dataplane(state, dataplane, runtime_config, decoded_control_metadata)
            service_outcome, last_service_outcome_signature = _service_outcome_for_budget(
                state,
                diagnostics_bus,
                last_service_outcome_signature,
            )
            budget_decision = service_budget_controller.update(
                runtime_config.services,
                state.service_stats,
                now=now,
                batch_size=batch_size,
                outcome=service_outcome,
            )
            state.service_budget = _service_budget_for_status(budget_decision)
            if budget_decision.changed:
                service_budget_overrides = budget_decision.packet_budget_overrides
                service_byte_budget_overrides = budget_decision.byte_budget_overrides
                poll_service_plan = _service_poll_plan_if_needed(
                    runtime_config,
                    batch_size,
                    service_budget_overrides,
                    service_byte_budget_overrides,
                )
                if diagnostics_bus is not None:
                    diagnostics_bus.publish(
                        DiagnosticEvent.scheduler_decision(
                            node=runtime_config.node,
                            message="service budget decision updated",
                            details={
                                "source": "service_budget_controller",
                                "reason": budget_decision.reason,
                                "packet_budget_overrides": service_budget_overrides,
                                "byte_budget_overrides": service_byte_budget_overrides,
                            },
                        )
                    )
            capacity_changes = merge_capacity_snapshots(
                tx_capacity_detector.observe(state.path_stats or {}, {}),
                rx_capacity_detector.observe(state.path_stats or {}, {}),
            )
            path_capacity = merge_capacity_snapshots(tx_capacity_detector.snapshot(), rx_capacity_detector.snapshot())
            if capacity_changes:
                save_path_capacity_cache(runtime_config, path_capacity)
            if runtime_config.role == "server":
                path_clock_sync = list(clock_sync_responses)
                clock_sync_responses.clear()
            elif clock_sync_client is not None:
                path_clock_sync = list(clock_sync_client.create_requests(path_names, path_ids).values())
            else:
                path_clock_sync = []
            if path_clock_sync:
                _try_announce_path_pinned_clock_sync(
                    dataplane,
                    runtime_config,
                    decoded_control_metadata,
                    path_clock_sync=path_clock_sync,
                )
            data_transmit_samples = take_data_transmit_sample_batch(pending_data_transmit_samples)
            path_latency = path_latency_tracker.dirty_snapshot()
            _try_announce_control_metadata(
                dataplane,
                runtime_config,
                decoded_control_metadata,
                path_capacity=path_capacity,
                path_latency=path_latency,
                path_pressure=path_pressure_from_path_stats(state.path_stats or {}),
                data_transmit_samples=data_transmit_samples,
                scheduler_status=_scheduler_status_tuple(
                    source_config,
                    runtime_config,
                    scheduler_policy_coordinator,
                ),
                include_sink_time=runtime_config.role == "server",
            )
            if path_latency:
                path_latency_tracker.mark_sent()
            if data_transmit_samples:
                next_data_sample_control_at = now + DATA_TIMING_SAMPLE_CONTROL_INTERVAL_SECONDS
            next_control_metadata_at = now + state.control_cadence.next_interval(forwarded_packets + delivered_packets)
        elif pending_data_transmit_samples and now >= next_data_sample_control_at:
            data_transmit_samples = take_data_transmit_sample_batch(pending_data_transmit_samples)
            path_latency = path_latency_tracker.dirty_snapshot()
            _try_announce_control_metadata(
                dataplane,
                runtime_config,
                decoded_control_metadata,
                path_capacity={},
                path_latency=path_latency,
                path_pressure={},
                data_transmit_samples=data_transmit_samples,
                scheduler_status=_scheduler_status_tuple(
                    source_config,
                    runtime_config,
                    scheduler_policy_coordinator,
                ),
                include_sink_time=runtime_config.role == "server",
            )
            if path_latency:
                path_latency_tracker.mark_sent()
            next_data_sample_control_at = now + DATA_TIMING_SAMPLE_CONTROL_INTERVAL_SECONDS
        if now >= next_remote_status_poll_at:
            try:
                send_request_if_due(dataplane, state.remote_status, peer_scopes=peer_names_by_scope)
            except RuntimeError as exc:
                logger.warning("remote status request skipped", extra={"error": str(exc)})
            next_remote_status_poll_at = now + REMOTE_STATUS_POLL_INTERVAL_SECONDS
        drained_reserved_events = _drain_python_reserved_services(
            dataplane,
            decoded_control_metadata,
            runner_state=state,
            path_names_by_id=path_names_by_id,
            local_targets_by_service_id=local_targets_by_service_id,
            remote_status=state.remote_status,
            peer_names_by_scope=peer_names_by_scope,
            fallback_peer_name=runtime_config.peer or "peer",
            status_provider=state.snapshot,
            clock_sync_client=clock_sync_client,
            clock_sync_responses=clock_sync_responses,
            path_latency_tracker=path_latency_tracker,
            data_traffic_latency_tracker=data_traffic_latency_tracker,
            diagnostics_bus=diagnostics_bus,
            live_rekey_context=live_rekey_context,
            runtime_config=runtime_config,
            apply_live_rekey_runtime=apply_live_rekey_runtime,
            transmit_live_rekey=transmit_live_rekey,
        )
        did_dataplane_work = did_dataplane_work or bool(drained_reserved_events)
        if drained_reserved_events and runtime_config.role == "server" and clock_sync_responses:
            immediate_clock_sync = list(clock_sync_responses)
            clock_sync_responses.clear()
            _try_announce_path_pinned_clock_sync(
                dataplane,
                runtime_config,
                decoded_control_metadata,
                path_clock_sync=immediate_clock_sync,
            )
            path_latency = path_latency_tracker.dirty_snapshot()
            _try_announce_control_metadata(
                dataplane,
                runtime_config,
                decoded_control_metadata,
                path_capacity={},
                path_latency=path_latency,
                path_pressure={},
                scheduler_status=_scheduler_status_tuple(
                    source_config,
                    runtime_config,
                    scheduler_policy_coordinator,
                ),
                include_sink_time=True,
            )
            if path_latency:
                path_latency_tracker.mark_sent()
        if drained_reserved_events:
            apply_control_policy_to_dataplane(
                dataplane,
                decoded_control_metadata,
                runtime_config=runtime_config,
                applied_disabled_services=applied_disabled_services,
                logger=logger.warning,
            )
        if next_auth_rekey_at is not None and live_rekey_context is not None and now >= next_auth_rekey_at:
            try:
                outbound = live_rekey_context.coordinator.maybe_start(
                    live_rekey_context.local_identity,
                    live_rekey_context.peer_identity,
                    live_rekey_context.topology,
                    live_rekey_context.current_session,
                    runtime_config,
                    state.snapshot(),
                    diagnostics=diagnostics_bus,
                )
                transmit_live_rekey(outbound)
            except (TypeError, ValueError, RuntimeError) as exc:
                logger.warning("auth rekey evaluation skipped", extra={"error": str(exc)})
                if diagnostics_bus is not None:
                    diagnostics_bus.publish(
                        DiagnosticEvent.warning(
                            "auth rekey evaluation skipped",
                            details={"error": str(exc), "source": "auth_crypto_rekey"},
                        )
                    )
            next_auth_rekey_at = now + AUTH_REKEY_EVALUATION_INTERVAL_SECONDS
        now = time.monotonic()
        if now >= next_status_snapshot_at or (max_iterations is not None and iterations == max_iterations):
            _refresh_runner_state_from_dataplane(state, dataplane, runtime_config, decoded_control_metadata)
            service_outcome, last_service_outcome_signature = _service_outcome_for_budget(
                state,
                diagnostics_bus,
                last_service_outcome_signature,
            )
            budget_decision = service_budget_controller.update(
                runtime_config.services,
                state.service_stats,
                now=now,
                batch_size=batch_size,
                outcome=service_outcome,
            )
            state.service_budget = _service_budget_for_status(budget_decision)
            if budget_decision.changed:
                service_budget_overrides = budget_decision.packet_budget_overrides
                service_byte_budget_overrides = budget_decision.byte_budget_overrides
                poll_service_plan = _service_poll_plan_if_needed(
                    runtime_config,
                    batch_size,
                    service_budget_overrides,
                    service_byte_budget_overrides,
                )
                if diagnostics_bus is not None:
                    diagnostics_bus.publish(
                        DiagnosticEvent.scheduler_decision(
                            node=runtime_config.node,
                            message="service budget decision updated",
                            details={
                                "source": "service_budget_controller",
                                "reason": budget_decision.reason,
                                "packet_budget_overrides": service_budget_overrides,
                                "byte_budget_overrides": service_byte_budget_overrides,
                            },
                        )
                    )
            next_status_snapshot_at = now + STATUS_SNAPSHOT_INTERVAL_SECONDS
        if diagnostics_bus is not None:
            last_security_drop_packets = _publish_security_drop_diagnostics(
                diagnostics_bus,
                state,
                node=runtime_config.node,
                peer=runtime_config.peer,
                last_packets=last_security_drop_packets,
            )
            current_counter_snapshot = (forwarded_packets, forwarded_bytes, delivered_packets, delivered_bytes)
            if current_counter_snapshot != last_counter_snapshot and now >= next_counter_snapshot_at:
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
                next_counter_snapshot_at = now + COUNTER_SNAPSHOT_INTERVAL_SECONDS
        if (
            next_scheduler_reapply_at is not None
            and scheduler_reapply_interval_seconds is not None
            and now >= next_scheduler_reapply_at
        ):
            try:
                runtime_config = hot_reapply_scheduler_from_status(
                    dataplane,
                    source_config,
                    runtime_config,
                    state.snapshot(),
                    telemetry_smoother=scheduler_telemetry_smoother,
                    scheduler_coordinator=scheduler_policy_coordinator,
                    service_path_allocator=service_path_allocator,
                    congestion_controller=congestion_controller,
                )
                _apply_reserved_service_schedulers(dataplane)
                if diagnostics_bus is not None:
                    diagnostics_bus.publish(
                        scheduler_decision_event_from_status(
                            source_config,
                            runtime_config,
                            state.snapshot(),
                            scheduler_coordinator=scheduler_policy_coordinator,
                            service_path_allocator=service_path_allocator,
                        )
                    )
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
            next_scheduler_reapply_at = now + scheduler_reapply_interval_seconds
        if diagnostics_bus is not None and now >= next_diagnostics_drain_at:
            diagnostics_bus.drain(limit=128)
            next_diagnostics_drain_at = now + DIAGNOSTICS_DRAIN_INTERVAL_SECONDS
        if not did_dataplane_work:
            time.sleep(IDLE_SLEEP_SECONDS)

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


def _service_poll_plan_if_needed(
    runtime_config: RuntimeConfig,
    batch_size: int,
    packet_budget_overrides: dict[str, int],
    byte_budget_overrides: dict[str, int],
) -> list[tuple[str, int, int]] | None:
    """Compile a Rust-executed service plan only when Python has non-default policy."""
    if not uses_service_budget_plan(runtime_config.services, packet_budget_overrides, byte_budget_overrides):
        return None
    return service_budget_plan(runtime_config.services, batch_size, packet_budget_overrides, byte_budget_overrides)


def _service_config_for_status(runtime_config: RuntimeConfig) -> list[dict[str, object]]:
    """Return operator-visible service policy facts without exposing Rust internals."""
    return [
        {
            "name": service.name,
            "service_id": service.service_id,
            "priority": service.priority,
            "priority_value": service.priority_value,
            "traffic_class": service.traffic_class,
            "listen": service.listen,
            "target": service.target,
        }
        for service in runtime_config.services
    ]


def _service_budget_for_status(decision: Any) -> dict[str, object]:
    """Return operator-visible service-budget facts while keeping policy in Python."""
    samples = getattr(decision, "samples", []) or []
    return {
        "active": bool(getattr(decision, "active", False)),
        "reason": str(getattr(decision, "reason", "")),
        "packet_budget_overrides": dict(getattr(decision, "packet_budget_overrides", {}) or {}),
        "byte_budget_overrides": dict(getattr(decision, "byte_budget_overrides", {}) or {}),
        "samples": [
            {
                "service": sample.service,
                "priority": sample.priority,
                "traffic_class": sample.traffic_class,
                "tx_packets_per_second": round(sample.tx_packets_per_second, 3),
                "tx_bytes_per_second": round(sample.tx_bytes_per_second, 3),
            }
            for sample in samples
        ],
    }


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
    state.service_stats = named_rust_service_stats(snapshot)
    state.service_path_stats = named_rust_service_path_stats(snapshot, runtime_config)
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


def _service_outcome_for_budget(
    state: CoreRunnerState,
    diagnostics_bus: DiagnosticsBus | None,
    last_signature: tuple[tuple[str, bool, str], ...] | None,
) -> tuple[ServiceOutcomeSnapshot | None, tuple[tuple[str, bool, str], ...] | None]:
    """
    Read Python-owned live service outcomes for the budget controller.

    Helpers and benchmark tooling inject these facts through the existing
    service IPC command path. This keeps the value in memory beside the runner
    state instead of adding a second file-watching control plane.
    """
    snapshot = state.service_outcome_snapshot
    signature = _service_outcome_signature(snapshot)
    state.service_outcomes = _service_outcomes_for_status(snapshot)
    if signature != last_signature and diagnostics_bus is not None:
        diagnostics_bus.publish(
            DiagnosticEvent.scheduler_decision(
                node=state.node,
                message="service outcome feedback updated",
                details={
                    "source": "service_ipc",
                    "outcomes": state.service_outcomes or [],
                },
            )
        )
    return snapshot, signature


def _service_outcome_signature(snapshot: ServiceOutcomeSnapshot | None) -> tuple[tuple[str, bool, str], ...] | None:
    """Return a stable signature for low-noise diagnostics."""
    if snapshot is None:
        return None
    return tuple(sorted((outcome.service, outcome.degraded, outcome.reason) for outcome in snapshot.outcomes))


def _service_outcomes_for_status(snapshot: ServiceOutcomeSnapshot | None) -> list[dict[str, object]] | None:
    """Return a JSON/status-friendly live outcome view."""
    if snapshot is None:
        return None
    return [
        {"service": outcome.service, "degraded": outcome.degraded, "reason": outcome.reason}
        for outcome in snapshot.outcomes
    ]


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
    runner_state: CoreRunnerState | None = None,
    path_names_by_id: dict[int, str],
    local_targets_by_service_id: dict[int, str],
    remote_status: RemoteStatusState | None = None,
    peer_names_by_scope: dict[int, str] | None = None,
    fallback_peer_name: str = "peer",
    status_provider: Callable[[], dict[str, object]] | None = None,
    clock_sync_client: InternalClockSyncClient | None = None,
    clock_sync_responses: list[InternalClockSyncMessage] | None = None,
    path_latency_tracker: PathLatencyTracker | None = None,
    data_traffic_latency_tracker: DataTrafficLatencyTracker | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
    live_rekey_context: LiveRekeyRuntimeContext | None = None,
    runtime_config: RuntimeConfig | None = None,
    apply_live_rekey_runtime: Callable[[Any], bool] | None = None,
    transmit_live_rekey: Callable[[Any], None] | None = None,
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
    extra_handlers[SERVICE_ID_AUTH_CRYPTO] = lambda event: _handle_auth_crypto_reserved_event(
        event,
        diagnostics=diagnostics_bus,
        peer_name=_peer_name_for_event(event, peer_names_by_scope or {}, fallback_peer_name),
        runner_state=runner_state,
        live_rekey_context=live_rekey_context,
        runtime_config=runtime_config,
        status_provider=status_provider,
        apply_live_rekey_runtime=apply_live_rekey_runtime,
        transmit_live_rekey=transmit_live_rekey,
    )
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
        clock_sync_client=clock_sync_client,
        clock_sync_responses=clock_sync_responses,
        path_latency_tracker=path_latency_tracker,
        data_traffic_latency_tracker=data_traffic_latency_tracker,
        extra_handlers=extra_handlers,
        logger=logger.warning,
    )


def _handle_auth_crypto_reserved_event(
    event: Any,
    *,
    diagnostics: DiagnosticsBus | None,
    peer_name: str | None,
    runner_state: CoreRunnerState | None,
    live_rekey_context: LiveRekeyRuntimeContext | None = None,
    runtime_config: RuntimeConfig | None = None,
    status_provider: Callable[[], dict[str, object]] | None = None,
    apply_live_rekey_runtime: Callable[[Any], bool] | None = None,
    transmit_live_rekey: Callable[[Any], None] | None = None,
) -> bool:
    """Decode auth/crypto reserved payloads and keep operator-safe Python state."""
    result = decode_auth_crypto_event(event, diagnostics=diagnostics, peer_name=peer_name)
    if runner_state is not None and result.message is not None:
        message = result.message
        runner_state.auth_crypto_messages.append(
            {
                "type": message.message_type,
                "peer": peer_name or message.sender_node_id,
                "sender_node_id": message.sender_node_id,
                "peer_node_id": message.peer_node_id,
                "topology_generation": message.topology_generation,
                "current_receiver_index": message.current_receiver_index,
                "created_at": message.created_at.isoformat(),
                "expires_at": message.expires_at.isoformat() if message.expires_at else None,
                "reason": message.reason,
                "has_noise": message.noise is not None,
                "path_id": _reserved_event_int(event, "path_id"),
                "sequence": _reserved_event_int(event, "sequence"),
            }
        )
        del runner_state.auth_crypto_messages[:-AUTH_CRYPTO_MESSAGE_HISTORY_LIMIT]
    if live_rekey_context is not None and runtime_config is not None:
        handled = live_rekey_context.coordinator.handle_peer_payload(
            live_rekey_context.local_identity,
            event.payload,
            live_rekey_context.topology,
            live_rekey_context.current_session,
            runtime_config,
            status_provider() if status_provider is not None else {},
            diagnostics=diagnostics,
        )
        if handled.outbound is not None and transmit_live_rekey is not None:
            # Rekey responses/rejects must be sent under the session that
            # authenticated the inbound control message. The peer cannot open a
            # responder's response if the responder swaps AEAD state before
            # transmitting it.
            transmit_live_rekey(handled.outbound)
        if handled.runtime_result is not None and apply_live_rekey_runtime is not None:
            apply_live_rekey_runtime(handled.runtime_result)
        return handled.accepted
    return result.accepted


def _reserved_event_int(event: Any, name: str) -> int:
    """Read an integer from either a Rust method-style event or a Python dataclass event."""
    value = getattr(event, name, 0)
    if callable(value):
        value = value()
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
    setter(SERVICE_ID_CONTROL_METADATA, 0, 0, 0, 0, 0, "inherit", None, None)
    # Auth/crypto payloads are stateful Python messages. Keep them on a single
    # scheduler-selected path here; Python can add explicit retry/dedupe policy
    # later without making Rust understand Noise or rekey semantics.
    setter(SERVICE_ID_AUTH_CRYPTO, 1, 0, 0, 0, 0, "inherit", None, None)
    setter(SERVICE_ID_REMOTE_STATUS, 1, 0, 0, 0, 0, "inherit", None, None)


def _try_announce_control_metadata(
    dataplane: Any,
    runtime_config: RuntimeConfig,
    control_metadata: dict[str, object],
    *,
    path_capacity: dict[str, dict[str, int | str | None]],
    path_latency: dict[str, dict[str, int | str | None]],
    path_pressure: dict[str, dict[str, int | str | None]],
    data_transmit_samples: list[tuple[int, int, int, int]] | None = None,
    scheduler_status: tuple[str, str, str] | None = None,
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
            path_capacity=path_capacity,
            path_latency=path_latency,
            path_pressure=path_pressure,
            data_transmit_samples=data_transmit_samples,
            scheduler_status=scheduler_status,
            include_sink_time=include_sink_time,
        )
    except RuntimeError as exc:
        logger.warning("control metadata announcement skipped", extra={"error": str(exc)})


def _drain_data_timing_samples(dataplane: Any) -> dict[str, object]:
    """Drain Rust-collected real-data timing facts without making Rust interpret them."""
    drain = getattr(dataplane, "drain_data_timing_samples", None)
    if not callable(drain):
        return {"tx": [], "rx": []}
    samples = drain()
    return samples if isinstance(samples, dict) else {"tx": [], "rx": []}


def _current_clock_offset_us(control_metadata: dict[str, object]) -> int | None:
    """Return the latest Python-owned local-to-shared clock offset for data samples."""
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    for key in ("mean_offset_us", "offset_us"):
        value = internal_clock.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _current_clock_rtt_us(control_metadata: dict[str, object]) -> int | None:
    """Return the current Python-owned RTT sanity value for data latency samples."""
    return _current_internal_clock_int(control_metadata, ("mean_rtt_us", "rtt_us"))


def _current_clock_error_us(control_metadata: dict[str, object]) -> int | None:
    """Return the current Python-owned clock error budget for data latency samples."""
    return _current_internal_clock_int(control_metadata, ("error_budget_us",))


def _current_internal_clock_int(control_metadata: dict[str, object], keys: tuple[str, ...]) -> int | None:
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    for key in keys:
        value = internal_clock.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _try_announce_path_pinned_clock_sync(
    dataplane: Any,
    runtime_config: RuntimeConfig,
    control_metadata: dict[str, object],
    *,
    path_clock_sync: list[InternalClockSyncMessage],
) -> None:
    """Send per-path clock probes only when the Rust bridge exposes exact-path injection."""
    try:
        announce_path_pinned_clock_sync(
            dataplane,
            runtime_config,
            control_metadata,
            path_clock_sync,
        )
    except RuntimeError as exc:
        logger.warning("path-pinned clock sync skipped", extra={"error": str(exc)})


def _scheduler_status_tuple(
    source_config: GatherlinkConfig | None,
    runtime_config: RuntimeConfig,
    scheduler_policy_coordinator: SchedulerPolicyCoordinator | None,
) -> tuple[str, str, str]:
    """Return local TX scheduler status for diagnostics and peer context."""
    configured_mode = source_config.scheduler.mode if source_config is not None else runtime_config.scheduler.mode
    effective_mode = configured_mode
    if scheduler_policy_coordinator is not None and scheduler_policy_coordinator.last_decision is not None:
        effective_mode = scheduler_policy_coordinator.last_decision.effective_mode
    elif source_config is not None and configured_mode == "coordinated_adaptive":
        effective_mode = default_effective_scheduler_policy(source_config)
    elif configured_mode == "coordinated_adaptive":
        effective_mode = runtime_config.scheduler.mode
    return str(configured_mode), str(effective_mode), str(runtime_config.scheduler.mode)


def _forward_available_for_service(dataplane: Any, service_name: str, batch_size: int) -> tuple[int, int]:
    """
    Drain one app-facing service without letting a quiet UDP socket stall supervision.

    The Rust dataplane keeps the blocking method for focused smoke tests, but the
    production runner needs the nonblocking shape so IPC status/stop, path receives,
    and scheduler reapply keep moving even when an application has no new datagram.
    """
    summary = getattr(dataplane, "forward_available_for_service_nonblocking_summary", None)
    if callable(summary):
        packet_count, byte_count = summary(service_name, batch_size)
        return int(packet_count), int(byte_count)
    nonblocking = getattr(dataplane, "forward_available_for_service_nonblocking", None)
    if callable(nonblocking):
        outcomes = nonblocking(service_name, batch_size)
    else:
        outcomes = dataplane.forward_available_for_service(service_name, batch_size)
    return len(outcomes), sum(int(outcome.payload_len()) for outcome in outcomes)


def _run_dataplane_available(
    dataplane: Any,
    service_names: list[str],
    batch_size: int,
    *,
    poll_service_names: list[str] | None = None,
    poll_service_plan: list[tuple[str, int, int]] | None = None,
) -> tuple[int, int, int, int]:
    """
    Drain Rust dataplane work with a bounded Rust-side burst when available.

    TODO(perf): This remains an execution primitive, not policy. Python chooses
    the service list, cadence, scheduler state, diagnostics, and stop behavior;
    Rust only repeats the same nonblocking socket/frame drains to amortize the
    Python bridge during high-rate packet flow.
    """
    plan_summary = getattr(dataplane, "run_available_budget_summary", None)
    if callable(plan_summary) and poll_service_plan is not None:
        forwarded_count, forwarded_bytes, delivered_count, delivered_bytes = plan_summary(
            poll_service_plan,
            batch_size,
            DATAPLANE_BURST_CYCLES,
        )
        return int(forwarded_count), int(forwarded_bytes), int(delivered_count), int(delivered_bytes)

    legacy_plan_summary = getattr(dataplane, "run_available_plan_summary", None)
    if callable(legacy_plan_summary) and poll_service_plan is not None:
        forwarded_count, forwarded_bytes, delivered_count, delivered_bytes = legacy_plan_summary(
            [(service_name, packet_budget) for service_name, packet_budget, _byte_budget in poll_service_plan],
            batch_size,
            DATAPLANE_BURST_CYCLES,
        )
        return int(forwarded_count), int(forwarded_bytes), int(delivered_count), int(delivered_bytes)

    summary = getattr(dataplane, "run_available_summary", None)
    if callable(summary):
        forwarded_count, forwarded_bytes, delivered_count, delivered_bytes = summary(
            poll_service_names or service_names,
            batch_size,
            DATAPLANE_BURST_CYCLES,
        )
        return int(forwarded_count), int(forwarded_bytes), int(delivered_count), int(delivered_bytes)

    forwarded_count = 0
    forwarded_bytes = 0
    for service_name in service_names:
        packet_count, byte_count = _forward_available_for_service(dataplane, service_name, batch_size)
        forwarded_count += packet_count
        forwarded_bytes += byte_count
    delivered_count, delivered_bytes = _receive_available_from_paths(dataplane, batch_size)
    return forwarded_count, forwarded_bytes, delivered_count, delivered_bytes


def _receive_available_from_paths(dataplane: Any, batch_size: int) -> tuple[int, int]:
    """Drain path frames using aggregate counters when the Rust bridge supports it."""
    summary = getattr(dataplane, "receive_available_from_paths_summary", None)
    if callable(summary):
        packet_count, byte_count = summary(batch_size)
        return int(packet_count), int(byte_count)
    outcomes = dataplane.receive_available_from_paths(batch_size)
    return len(outcomes), sum(int(outcome.payload_len()) for outcome in outcomes)


def _is_idle_receive_timeout(exc: Exception) -> bool:
    """Return whether Rust reported an idle UDP receive timeout."""
    text = str(exc).lower()
    # Rust maps socket idle timeouts through PyO3 as runtime errors. Treating
    # only receive-timeout wording as idle keeps real dataplane errors loud.
    return "failed to receive udp datagram" in text and (
        "timed out" in text or "would block" in text or "resource temporarily unavailable" in text
    )
