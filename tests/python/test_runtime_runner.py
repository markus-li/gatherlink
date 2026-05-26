from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event

import pytest
from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, PathSchedulerConfig, SchedulerConfig, ServiceConfig
from gatherlink.control.policy import apply_control_policy_to_dataplane
from gatherlink.control.remote_status import encode_request
from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.protocol import (
    GATHERLINK_V1_HEADER_LEN,
    SERVICE_ID_AUTH_CRYPTO,
    SERVICE_ID_CONTROL_METADATA,
    SERVICE_ID_REMOTE_STATUS,
    decode_control_payload,
    encode_control_payload,
)
from gatherlink.runtime.rekey import (
    LiveRekeyRuntimeContext,
    create_runtime_rekey_initiation,
    runtime_security_from_session,
)
from gatherlink.runtime.runner import (
    CoreRunnerState,
    _run_dataplane_available,
    _scheduler_status_tuple,
    run_core_service,
)
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import ProvisionedNode, TopologyBundleBody
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.rekey_control import RekeyControlMessage
from gatherlink.security.sessions import plan_authenticated_static_session
from gatherlink.time.offset import InternalClockSyncMessage


@dataclass
class FakeOutcome:
    length: int

    def payload_len(self) -> int:
        return self.length


class MemoryDiagnosticSink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    def write(self, event: DiagnosticEvent) -> None:
        self.events.append(event)


class FakeDataplane:
    def __init__(self) -> None:
        self.calls = 0
        self.blocking_calls = 0
        self.transmitted_payloads: list[tuple[int, bytes]] = []

    def forward_available_for_service(self, service_name: str, batch_size: int):
        self.blocking_calls += 1
        return self.forward_available_for_service_nonblocking(service_name, batch_size)

    def forward_available_for_service_nonblocking(self, service_name: str, batch_size: int):
        self.calls += 1
        assert service_name == "udp-main"
        assert batch_size == 8
        if self.calls == 1:
            return [FakeOutcome(100), FakeOutcome(50)]
        raise RuntimeError("failed to receive UDP datagram: timed out")

    def receive_available_from_paths(self, batch_size: int):
        assert batch_size == 8
        return [FakeOutcome(25)] if self.calls == 1 else []

    def status_snapshot(self):
        return {
            "services": {"udp-main": {"tx_packets": 3, "tx_bytes": 150, "rx_packets": 1, "rx_bytes": 25}},
            "path_stats": {"0": {"tx_packets": 1, "tx_bytes": 10, "rx_packets": 2, "rx_bytes": 20}},
            "service_path_stats": {
                "udp-main": {"0": {"tx_packets": 1, "tx_bytes": 10, "rx_packets": 2, "rx_bytes": 20}}
            },
            "control_metadata": {"sent": {"frames": 1, "bytes": 32}},
            "disabled_services": {},
        }

    def transmit_service_payload(self, service_id: int, payload: bytes) -> int:
        self.transmitted_payloads.append((service_id, payload))
        return 1

    def transmit_service_payload_on_path(self, service_id: int, path_id: int, payload: bytes) -> int:
        _ = path_id
        self.transmitted_payloads.append((service_id, payload))
        return 1


class SummaryFakeDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.detailed_forward_calls = 0
        self.detailed_receive_calls = 0

    def forward_available_for_service_nonblocking_summary(self, service_name: str, batch_size: int):
        self.calls += 1
        assert service_name == "udp-main"
        assert batch_size == 8
        if self.calls == 1:
            return 2, 150
        raise RuntimeError("failed to receive UDP datagram: timed out")

    def forward_available_for_service_nonblocking(self, service_name: str, batch_size: int):
        self.detailed_forward_calls += 1
        return super().forward_available_for_service_nonblocking(service_name, batch_size)

    def receive_available_from_paths_summary(self, batch_size: int):
        assert batch_size == 8
        return (1, 25) if self.calls == 1 else (0, 0)

    def receive_available_from_paths(self, batch_size: int):
        self.detailed_receive_calls += 1
        return super().receive_available_from_paths(batch_size)


class BurstFakeDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.burst_calls = 0
        self.service_names_seen: list[list[str]] = []
        self.max_cycles_seen: list[int] = []

    def run_available_summary(self, service_names: list[str], batch_size: int, max_cycles: int):
        self.burst_calls += 1
        self.service_names_seen.append(service_names)
        self.max_cycles_seen.append(max_cycles)
        assert batch_size == 8
        if self.burst_calls == 1:
            return 2, 150, 1, 25
        return 0, 0, 0, 0

    def forward_available_for_service_nonblocking_summary(self, service_name: str, batch_size: int):
        raise AssertionError("burst summary should be preferred over per-service summary")

    def receive_available_from_paths_summary(self, batch_size: int):
        raise AssertionError("burst summary should be preferred over per-path summary")


class PlannedBurstFakeDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.plan_seen: list[list[tuple[str, int]]] = []

    def run_available_plan_summary(
        self,
        service_plan: list[tuple[str, int]],
        path_batch_size: int,
        max_cycles: int,
    ):
        _ = max_cycles
        self.plan_seen.append(service_plan)
        assert path_batch_size == 8
        return 3, 300, 1, 25

    def run_available_summary(self, service_names: list[str], batch_size: int, max_cycles: int):
        raise AssertionError("per-service drain plan should be preferred over legacy burst summary")


class BudgetBurstFakeDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.plan_seen: list[list[tuple[str, int, int]]] = []

    def status_snapshot(self):
        return {
            "services": {
                "stable": {"tx_packets": self.calls * 100, "tx_bytes": self.calls * 100_000},
                "fast": {"tx_packets": self.calls * 600, "tx_bytes": self.calls * 800_000},
            },
            "path_stats": {"0": {"tx_packets": 1, "tx_bytes": 10, "rx_packets": 2, "rx_bytes": 20}},
            "control_metadata": {"sent": {"frames": 1, "bytes": 32}},
            "disabled_services": {},
        }

    def run_available_budget_summary(
        self,
        service_plan: list[tuple[str, int, int]],
        path_batch_size: int,
        max_cycles: int,
    ):
        _ = max_cycles
        self.calls += 1
        self.plan_seen.append(service_plan)
        assert path_batch_size == 8
        return 3, 300, 1, 25

    def run_available_plan_summary(
        self,
        service_plan: list[tuple[str, int]],
        path_batch_size: int,
        max_cycles: int,
    ):
        raise AssertionError("budget-aware drain plan should be preferred over legacy drain plan")


class OutcomeStatusFakeDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.summary_calls = 0

    def run_available_summary(self, service_names: list[str], batch_size: int, max_cycles: int):
        _ = service_names, max_cycles
        self.summary_calls += 1
        assert batch_size == 8
        return 2, 200, 1, 25

    def status_snapshot(self):
        return {
            "services": {
                "stable": {"tx_packets": self.summary_calls * 100, "tx_bytes": self.summary_calls * 100_000},
                "fast": {"tx_packets": self.summary_calls * 600, "tx_bytes": self.summary_calls * 800_000},
            },
            "path_stats": {"0": {"tx_packets": 1, "tx_bytes": 10, "rx_packets": 2, "rx_bytes": 20}},
            "control_metadata": {"sent": {"frames": 1, "bytes": 32}},
            "disabled_services": {},
        }


class PlanCapableLegacyFakeDataplane(BurstFakeDataplane):
    def run_available_plan_summary(
        self,
        service_plan: list[tuple[str, int]],
        path_batch_size: int,
        max_cycles: int,
    ):
        _ = service_plan, path_batch_size, max_cycles
        raise AssertionError("zero service drain quantum should keep the legacy hot path")


class ShortBurstTimingDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.burst_calls = 0
        self.drain_calls = 0

    def run_available_summary(self, service_names: list[str], batch_size: int, max_cycles: int):
        _ = service_names, batch_size, max_cycles
        self.burst_calls += 1
        if self.burst_calls == 2:
            return 1, 200, 0, 0
        return 0, 0, 0, 0

    def drain_data_timing_samples(self):
        self.drain_calls += 1
        return {
            "tx": [
                {
                    "path_id": 0,
                    "sequence": 1024,
                    "packet_count": 1,
                    "observed_at_us": 10_000,
                }
            ],
            "rx": [],
        }


class FakeReservedEvent:
    def __init__(self, service_id: int, path_id: int, sequence: int, payload: bytes) -> None:
        self._service_id = service_id
        self._path_id = path_id
        self._sequence = sequence
        self._payload = payload

    def service_id(self) -> int:
        return self._service_id

    def path_id(self) -> int:
        return self._path_id

    def sequence(self) -> int:
        return self._sequence

    def payload(self) -> bytes:
        return self._payload

    def frame_bytes(self) -> int:
        return len(self._payload) + 14


class FakeControlDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.disabled_services: list[tuple[int, str]] = []
        self.scheduler_policies: list[tuple[int, int, int, int, int, int, str]] = []
        self._reserved_events = [
            FakeReservedEvent(
                SERVICE_ID_CONTROL_METADATA,
                1,
                10,
                encode_control_payload(
                    {1: "path-a"},
                    service_disables={256: "peer disabled test service"},
                    service_scheduler_policies={256: (2, 96, 50_000, 500_000, 64, 1)},
                ),
            )
        ]

    def drain_reserved_service_events(self):
        events = self._reserved_events
        self._reserved_events = []
        return events

    def disable_service(self, service_id: int, reason: str) -> None:
        self.disabled_services.append((service_id, reason))

    def set_service_scheduler(
        self,
        service_id: int,
        fanout: int,
        fanout_below_bytes: int,
        flowlet_idle_us: int = 0,
        flowlet_max_hold_us: int = 0,
        path_run_datagrams: int = 0,
        path_policy: str = "inherit",
        allowed_path_ids: list[int] | None = None,
        path_weights: list[tuple[int, int]] | None = None,
    ) -> None:
        self.scheduler_policies.append(
            (
                service_id,
                fanout,
                fanout_below_bytes,
                flowlet_idle_us,
                flowlet_max_hold_us,
                path_run_datagrams,
                path_policy,
            )
        )


class FakeClockRequestDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.scheduler_policies: list[tuple[int, int, int, int, int, int, str]] = []
        self._reserved_events = [
            FakeReservedEvent(
                SERVICE_ID_CONTROL_METADATA,
                1,
                10,
                encode_control_payload(
                    {0: "path-a"},
                    path_clock_sync=[
                        InternalClockSyncMessage(exchange_id=9, path_id=0, mode=1, origin_us=1_000),
                    ],
                ),
            )
        ]

    def drain_reserved_service_events(self):
        events = self._reserved_events
        self._reserved_events = []
        return events

    def set_service_scheduler(
        self,
        service_id: int,
        fanout: int,
        fanout_below_bytes: int,
        flowlet_idle_us: int = 0,
        flowlet_max_hold_us: int = 0,
        path_run_datagrams: int = 0,
        path_policy: str = "inherit",
        allowed_path_ids: list[int] | None = None,
        path_weights: list[tuple[int, int]] | None = None,
    ) -> None:
        self.scheduler_policies.append(
            (
                service_id,
                fanout,
                fanout_below_bytes,
                flowlet_idle_us,
                flowlet_max_hold_us,
                path_run_datagrams,
                path_policy,
            )
        )


class FakeRemoteStatusDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.scheduler_policies: list[tuple[int, int, int, int, int, int, str]] = []
        self._reserved_events = [FakeReservedEvent(SERVICE_ID_REMOTE_STATUS, 1, 20, encode_request(7))]

    def drain_reserved_service_events(self):
        events = self._reserved_events
        self._reserved_events = []
        return events

    def set_service_scheduler(
        self,
        service_id: int,
        fanout: int,
        fanout_below_bytes: int,
        flowlet_idle_us: int = 0,
        flowlet_max_hold_us: int = 0,
        path_run_datagrams: int = 0,
        path_policy: str = "inherit",
        allowed_path_ids: list[int] | None = None,
        path_weights: list[tuple[int, int]] | None = None,
    ) -> None:
        self.scheduler_policies.append(
            (
                service_id,
                fanout,
                fanout_below_bytes,
                flowlet_idle_us,
                flowlet_max_hold_us,
                path_run_datagrams,
                path_policy,
                allowed_path_ids or [],
            )
        )


class FakeAuthCryptoDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        self._reserved_events = [FakeReservedEvent(SERVICE_ID_AUTH_CRYPTO, 1, 20, b"not-json")]

    def drain_reserved_service_events(self):
        events = self._reserved_events
        self._reserved_events = []
        return events


class FakeValidAuthCryptoDataplane(FakeDataplane):
    def __init__(self) -> None:
        super().__init__()
        message = RekeyControlMessage(
            message_type="rekey_initiation",
            sender_node_id="peer-node",
            peer_node_id="local-node",
            topology_generation=7,
            current_receiver_index=42,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            noise={"phase": "initiation"},
        )
        self._reserved_events = [FakeReservedEvent(SERVICE_ID_AUTH_CRYPTO, 1, 21, message.encode())]

    def drain_reserved_service_events(self):
        events = self._reserved_events
        self._reserved_events = []
        return events

    def set_service_scheduler(self, *args):
        _ = args


class FakeLiveRekeyDataplane(FakeDataplane):
    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self._reserved_events = [FakeReservedEvent(SERVICE_ID_AUTH_CRYPTO, 1, 22, payload)]
        self.rekey_transmits: list[tuple[int, bytes]] = []
        self.rekey_operations: list[str] = []

    def drain_reserved_service_events(self):
        events = self._reserved_events
        self._reserved_events = []
        return events

    def transmit_service_payload(self, service_id: int, payload: bytes) -> int:
        self.rekey_transmits.append((service_id, payload))
        if service_id == SERVICE_ID_AUTH_CRYPTO:
            self.rekey_operations.append("transmit")
        return 1

    def set_service_scheduler(self, *args):
        _ = args


class PathOnlyFakeDataplane(FakeDataplane):
    def receive_available_from_paths(self, batch_size: int):
        assert batch_size == 8
        return [FakeOutcome(25)]


class SecurityDropFakeDataplane(FakeDataplane):
    def status_snapshot(self):
        snapshot = super().status_snapshot()
        snapshot["security_drops"] = {"packets": 3, "bytes": 192}
        return snapshot


class MemorySink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    def write(self, event: DiagnosticEvent) -> None:
        self.events.append(event)


class PolicyCaptureDataplane:
    def __init__(self) -> None:
        self.scheduler_policies: list[tuple[int, list[int] | None, list[tuple[int, int]] | None]] = []

    def set_service_scheduler(
        self,
        service_id: int,
        fanout: int,
        fanout_below_bytes: int,
        flowlet_idle_us: int = 0,
        flowlet_max_hold_us: int = 0,
        path_run_datagrams: int = 0,
        path_policy: str = "inherit",
        allowed_path_ids: list[int] | None = None,
        path_weights: list[tuple[int, int]] | None = None,
    ) -> None:
        _ = fanout, fanout_below_bytes, flowlet_idle_us, flowlet_max_hold_us, path_run_datagrams, path_policy
        self.scheduler_policies.append((service_id, allowed_path_ids, path_weights))


def _runtime_config():
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    return expand_config(config)


def _rekey_topology(local: NodeIdentity, peer: NodeIdentity, now: datetime) -> TopologyBundleBody:
    return TopologyBundleBody(
        generation=7,
        issuer_node_id=IdentityPublicRecord.from_identity(local).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="local", identity=IdentityPublicRecord.from_identity(local)),
            ProvisionedNode(name="peer", identity=IdentityPublicRecord.from_identity(peer)),
        ],
    )


def test_remote_scheduler_policy_preserves_local_service_path_selection() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[
            PathConfig(name="path-a", interface="gl-a"),
            PathConfig(name="path-b", interface="gl-b"),
            PathConfig(name="path-c", interface="gl-c"),
        ],
        services=[
            ServiceConfig(
                name="wireguard-fast",
                listen="127.0.0.1:55181",
                target="127.0.0.1:19094",
                scheduler_allowed_paths=["path-b", "path-c"],
                scheduler_path_weights={"path-b": 140, "path-c": 90},
            )
        ],
    )
    runtime_config = expand_config(config)
    dataplane = PolicyCaptureDataplane()

    apply_control_policy_to_dataplane(
        dataplane,
        {
            "service_scheduler_policies": {
                "256": {
                    "fanout": 1,
                    "fanout_below_bytes": 0,
                    "flowlet_idle_us": 0,
                    "flowlet_max_hold_us": 0,
                    "path_run_datagrams": 0,
                    "path_policy": "weighted_round_robin",
                }
            }
        },
        runtime_config=runtime_config,
    )

    assert dataplane.scheduler_policies == [(256, [1, 2], [(1, 140), (2, 90)])]


def test_core_runner_uses_rust_dataplane_handle_without_packet_logic() -> None:
    dataplane = FakeDataplane()

    result = run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=2,
        batch_size=8,
    )

    assert result.iterations == 2
    assert result.forwarded_packets == 2
    assert result.forwarded_bytes == 150
    assert result.delivered_packets == 1
    assert result.delivered_bytes == 25
    assert dataplane.calls == 2
    assert dataplane.blocking_calls == 0


def test_core_runner_uses_summary_bridge_on_hot_path() -> None:
    dataplane = SummaryFakeDataplane()

    result = run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=2,
        batch_size=8,
    )

    assert result.forwarded_packets == 2
    assert result.forwarded_bytes == 150
    assert result.delivered_packets == 1
    assert result.delivered_bytes == 25
    assert dataplane.detailed_forward_calls == 0
    assert dataplane.detailed_receive_calls == 0


def test_core_runner_prefers_bounded_rust_burst_bridge() -> None:
    dataplane = BurstFakeDataplane()

    result = run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=2,
        batch_size=8,
    )

    assert result.forwarded_packets == 2
    assert result.forwarded_bytes == 150
    assert result.delivered_packets == 1
    assert result.delivered_bytes == 25
    assert dataplane.burst_calls == 2
    assert dataplane.service_names_seen == [["udp-main", "udp-main"], ["udp-main", "udp-main"]]
    assert all(max_cycles > 1 for max_cycles in dataplane.max_cycles_seen)


def test_core_runner_bypasses_service_drain_plan_when_no_quantum_is_configured() -> None:
    dataplane = PlanCapableLegacyFakeDataplane()

    result = run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=1,
        batch_size=8,
    )

    assert result.forwarded_packets == 2
    assert result.forwarded_bytes == 150
    assert dataplane.service_names_seen == [["udp-main", "udp-main"]]


def test_core_runner_passes_python_compiled_service_drain_plan() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="stable",
                listen="127.0.0.1:55180",
                target="127.0.0.1:51820",
                priority="high",
            ),
            ServiceConfig(
                name="fast",
                listen="127.0.0.1:55181",
                target="127.0.0.1:51821",
                priority="bulk",
                scheduler_poll_batch_packets=4,
            ),
        ],
    )
    dataplane = PlannedBurstFakeDataplane()
    state = CoreRunnerState(
        node="local",
        security_mode="none",
        service_names=["stable", "fast"],
        stop_event=Event(),
    )

    result = run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=1,
        batch_size=8,
        runner_state=state,
    )

    assert result.forwarded_packets == 3
    assert result.forwarded_bytes == 300
    assert result.delivered_packets == 1
    assert result.delivered_bytes == 25
    assert dataplane.plan_seen == [[("stable", 8), ("stable", 8), ("stable", 8), ("fast", 4)]]
    assert state.snapshot()["service_config"] == [
        {
            "name": "stable",
            "service_id": 256,
            "priority": "high",
            "priority_value": 200,
            "traffic_class": "unknown",
            "listen": "127.0.0.1:55180",
            "target": "127.0.0.1:51820",
        },
        {
            "name": "fast",
            "service_id": 257,
            "priority": "bulk",
            "priority_value": 50,
            "traffic_class": "unknown",
            "listen": "127.0.0.1:55181",
            "target": "127.0.0.1:51821",
        },
    ]


def test_runner_hot_path_prefers_budget_aware_service_plan_when_available() -> None:
    dataplane = BudgetBurstFakeDataplane()

    result = _run_dataplane_available(
        dataplane,
        ["stable", "fast"],
        batch_size=8,
        poll_service_plan=[("stable", 8, 0), ("fast", 4, 256_000)],
    )

    assert result == (3, 300, 1, 25)
    assert dataplane.plan_seen == [[("stable", 8, 0), ("fast", 4, 256_000)]]


def test_core_runner_uses_ipc_service_outcome_for_python_budgeting() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="stable",
                listen="127.0.0.1:55180",
                target="127.0.0.1:51820",
                priority="high",
            ),
            ServiceConfig(
                name="fast",
                listen="127.0.0.1:55181",
                target="127.0.0.1:51821",
                priority="bulk",
            ),
        ],
    )
    state = CoreRunnerState(
        node="local",
        security_mode="none",
        service_names=["stable", "fast"],
        stop_event=Event(),
    )
    sink = MemoryDiagnosticSink()
    bus = DiagnosticsBus(sinks=[sink])
    state.request_service_outcome(
        {"outcomes": [{"service": "stable", "degraded": True, "reason": "live tcp retransmits increased"}]}
    )

    run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: OutcomeStatusFakeDataplane(),
        max_iterations=1,
        batch_size=8,
        runner_state=state,
        diagnostics_bus=bus,
    )

    bus.drain()
    assert state.service_outcomes == [
        {"service": "stable", "degraded": True, "reason": "live tcp retransmits increased"}
    ]
    assert state.snapshot()["service_outcomes"] == state.service_outcomes
    assert state.snapshot()["service_budget"]["active"] is False
    assert state.snapshot()["service_budget"]["reason"] == "not enough samples"
    assert any(
        event.code == "scheduler.decision" and event.details.get("source") == "service_ipc" for event in sink.events
    )


def test_core_runner_flushes_short_burst_data_timing_before_idle_cadence() -> None:
    dataplane = ShortBurstTimingDataplane()

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=2,
        batch_size=8,
    )

    decoded_payloads = [
        decode_control_payload(payload)
        for service_id, payload in dataplane.transmitted_payloads
        if service_id == SERVICE_ID_CONTROL_METADATA
    ]
    advertised_samples = [
        sample for frame in decoded_payloads if frame is not None for sample in frame.data_transmit_samples
    ]
    assert dataplane.drain_calls == 1
    assert advertised_samples == [(0, 1024, 1, 10_000)]


def test_control_metadata_trims_data_timing_samples_to_path_mtu() -> None:
    from gatherlink.control.announcements import announce_control_metadata
    from gatherlink.control.metadata import empty_control_metadata

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler=PathSchedulerConfig(mtu=1200)),
            PathConfig(name="path-b", interface="gl-b", scheduler=PathSchedulerConfig(mtu=1200)),
            PathConfig(name="path-c", interface="gl-c", scheduler=PathSchedulerConfig(mtu=1200)),
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    dataplane = FakeDataplane()
    runtime_config = expand_config(config)
    samples = [(0, 10_000 + index, 1, 1_000_000 + index) for index in range(128)]

    announcement = announce_control_metadata(
        dataplane,
        runtime_config,
        empty_control_metadata(),
        data_transmit_samples=samples,
    )

    assert announcement.data_transmit_sample_count < len(samples)
    assert announcement.omitted_data_transmit_sample_count == len(samples) - announcement.data_transmit_sample_count
    service_id, payload = dataplane.transmitted_payloads[-1]
    assert service_id == SERVICE_ID_CONTROL_METADATA
    assert len(payload) + GATHERLINK_V1_HEADER_LEN <= 1200
    decoded = decode_control_payload(payload)
    assert decoded is not None
    assert 0 < len(decoded.data_transmit_samples) == announcement.data_transmit_sample_count


def test_core_runner_supervises_standard_carrier_before_rust_bridge() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="server",
        peer="remote",
        paths=[
            PathConfig(
                name="path-a",
                interface="lo",
                carrier="quic-datagram",
                transport_bind="127.0.0.1:0",
            )
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:0", target="127.0.0.1:51820")],
    )
    seen_runtime_configs = []

    def fake_factory(runtime_config):
        seen_runtime_configs.append(runtime_config)
        return FakeDataplane()

    run_core_service(expand_config(config), dataplane_factory=fake_factory, max_iterations=1, batch_size=8)

    rust_path = seen_runtime_configs[0].paths[0]
    assert rust_path.carrier == "udp"
    assert rust_path.transport_bind.startswith("127.0.0.1:")
    assert rust_path.transport_remote.startswith("127.0.0.1:")
    assert rust_path.transport_bind != "127.0.0.1:0"


def test_core_runner_reports_standard_carrier_start_failures() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[
            PathConfig(
                name="path-a",
                interface="lo",
                carrier="quic-datagram",
            )
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:0", target="127.0.0.1:51820")],
    )
    sink = MemorySink()
    bus = DiagnosticsBus(sinks=[sink])

    with pytest.raises(RuntimeError, match="transport_bind"):
        run_core_service(
            expand_config(config),
            dataplane_factory=lambda _runtime_config: FakeDataplane(),
            max_iterations=1,
            batch_size=8,
            diagnostics_bus=bus,
        )
    bus.drain()

    carrier_events = [event for event in sink.events if event.code == "carrier.connect_failed"]
    assert carrier_events
    assert carrier_events[0].path == "path-a"
    assert carrier_events[0].details["carrier"] == "quic-datagram"
    assert carrier_events[0].details["source"] == "carrier_supervisor"


def test_core_runner_emits_lifecycle_diagnostics() -> None:
    sink = MemorySink()
    bus = DiagnosticsBus(sinks=[sink])

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: FakeDataplane(),
        max_iterations=1,
        batch_size=8,
        diagnostics_bus=bus,
    )
    bus.drain()

    assert [event.code for event in sink.events] == [
        "warning",
        "warning",
        "service.bound",
        "counter.snapshot",
        "runtime.shutdown",
    ]
    assert sink.events[2].details["target"] == "127.0.0.1:51820"
    assert sink.events[3].details["tx_packets"] == 2
    assert sink.events[3].details["rx_packets"] == 1


def test_core_runner_emits_structured_security_drop_diagnostics() -> None:
    sink = MemorySink()
    bus = DiagnosticsBus(sinks=[sink])

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: SecurityDropFakeDataplane(),
        max_iterations=2,
        batch_size=8,
        diagnostics_bus=bus,
    )
    bus.drain()

    drop_events = [event for event in sink.events if event.kind == "drop"]
    assert len(drop_events) == 1
    assert drop_events[0].code == "crypto.auth_failed"
    assert drop_events[0].details["packets"] == 3
    assert drop_events[0].details["delta_packets"] == 3


def test_core_runner_state_exposes_ipc_status_from_rust_snapshot() -> None:
    state = CoreRunnerState(
        node="local",
        security_mode="none",
        service_names=["udp-main"],
        stop_event=Event(),
    )

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: FakeDataplane(),
        max_iterations=1,
        batch_size=8,
        runner_state=state,
    )
    status = state.snapshot()

    assert status["tx_packets"] == 2
    assert status["rx_packets"] == 1
    assert status["service_stats"]["udp-main"]["tx_bytes"] == 150
    assert status["service_path_stats"]["udp-main"]["path-a"]["tx_bytes"] == 10
    assert status["path_stats"]["path-a"]["tx_bytes"] == 10
    assert status["control_metadata"]["sent"]["frames"] == 2


def test_core_runner_drains_python_reserved_services_and_applies_policy() -> None:
    dataplane = FakeControlDataplane()
    state = CoreRunnerState(
        node="local",
        security_mode="none",
        service_names=["udp-main"],
        stop_event=Event(),
    )

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=1,
        batch_size=8,
        runner_state=state,
    )
    status = state.snapshot()

    assert dataplane.disabled_services == [(256, "peer disabled test service")]
    assert dataplane.scheduler_policies == [
        (1, 0, 0, 0, 0, 0, "inherit"),
        (7, 1, 0, 0, 0, 0, "inherit"),
        (8, 1, 0, 0, 0, 0, "inherit"),
        (256, 2, 96, 50_000, 500_000, 64, "single_best_path"),
    ]
    assert status["control_metadata"]["received"]["frames"] == 1
    assert status["control_metadata"]["service_disables"]["256"] == "peer disabled test service"
    assert status["control_metadata"]["service_scheduler_policies"]["256"] == {
        "fanout": 2,
        "fanout_below_bytes": 96,
        "flowlet_idle_us": 50_000,
        "flowlet_max_hold_us": 500_000,
        "path_run_datagrams": 64,
        "path_policy": "single_best_path",
    }


def test_core_runner_routes_reserved_auth_crypto_payloads_to_python_diagnostics() -> None:
    dataplane = FakeAuthCryptoDataplane()
    sink = MemoryDiagnosticSink()
    bus = DiagnosticsBus(sinks=[sink])

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=1,
        batch_size=8,
        diagnostics_bus=bus,
    )

    events = [event for event in sink.events if event.code == "rekey.rejected"]
    assert len(events) == 1
    assert events[0].kind == "runtime"
    assert events[0].severity == "warning"
    assert events[0].details["path_id"] == 1
    assert events[0].details["sequence"] == 20
    assert "invalid rekey control payload" in events[0].details["reason"]


def test_core_runner_exposes_decoded_auth_crypto_facts_in_status() -> None:
    dataplane = FakeValidAuthCryptoDataplane()
    state = CoreRunnerState(
        node="local",
        security_mode="authenticated",
        service_names=["udp-main"],
        stop_event=Event(),
    )

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=1,
        batch_size=8,
        runner_state=state,
    )

    status = state.snapshot()
    assert status["auth_crypto_messages"] == [
        {
            "type": "rekey_initiation",
            "peer": "remote",
            "sender_node_id": "peer-node",
            "peer_node_id": "local-node",
            "topology_generation": 7,
            "current_receiver_index": 42,
            "created_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "reason": None,
            "has_noise": True,
            "path_id": 1,
            "sequence": 21,
        }
    ]


def test_core_runner_hot_reapplies_valid_live_rekey_and_sends_response() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    now = datetime.now(UTC)
    topology = _rekey_topology(local, peer, now)
    local_session = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="responder",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    peer_session = plan_authenticated_static_session(
        peer,
        IdentityPublicRecord.from_identity(local),
        topology,
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    pending = create_runtime_rekey_initiation(
        peer,
        IdentityPublicRecord.from_identity(local),
        topology,
        peer_session,
        now=now,
        receiver_index=222,
    )
    runtime_config = _runtime_config().model_copy(update={"security": runtime_security_from_session(local_session)})
    dataplane = FakeLiveRekeyDataplane(pending.payload)
    applied = []

    def fake_reapply(_dataplane, updated_runtime_config):
        dataplane.rekey_operations.append("reapply")
        applied.append(updated_runtime_config.security)

    run_core_service(
        runtime_config,
        dataplane_factory=lambda _runtime_config: dataplane,
        dataplane_reapply=fake_reapply,
        max_iterations=1,
        batch_size=8,
        live_rekey_context=LiveRekeyRuntimeContext(
            local_identity=local,
            peer_identity=IdentityPublicRecord.from_identity(peer),
            topology=topology,
            current_session=local_session,
        ),
    )

    assert applied
    assert applied[0].source_mode == "authenticated"
    assert applied[0].local_node_id == IdentityPublicRecord.from_identity(local).node_id
    auth_payloads = [
        payload for service_id, payload in dataplane.rekey_transmits if service_id == SERVICE_ID_AUTH_CRYPTO
    ]
    assert auth_payloads
    response = RekeyControlMessage.decode(auth_payloads[0])
    assert response.message_type == "rekey_response"
    assert response.peer_node_id == IdentityPublicRecord.from_identity(peer).node_id
    assert dataplane.rekey_operations[:2] == ["transmit", "reapply"]


def test_core_runner_sends_clock_sync_responses_without_waiting_for_next_cadence() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="sink",
        role="server",
        peer="source",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    dataplane = FakeClockRequestDataplane()

    run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=1,
        batch_size=8,
    )

    decoded_payloads = [
        decode_control_payload(payload)
        for service_id, payload in dataplane.transmitted_payloads
        if service_id == SERVICE_ID_CONTROL_METADATA
    ]
    clock_responses = [
        message
        for frame in decoded_payloads
        if frame is not None
        for message in frame.internal_clock_sync
        if message.mode == 2
    ]
    assert len(dataplane.transmitted_payloads) >= 2
    assert clock_responses
    assert clock_responses[0].exchange_id == 9


def test_core_runner_handles_remote_status_requests_in_production_path() -> None:
    dataplane = FakeRemoteStatusDataplane()
    state = CoreRunnerState(
        node="local",
        security_mode="none",
        service_names=["udp-main"],
        stop_event=Event(),
    )

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=1,
        batch_size=8,
        runner_state=state,
    )

    remote_status_payloads = [
        payload for service_id, payload in dataplane.transmitted_payloads if service_id == SERVICE_ID_REMOTE_STATUS
    ]
    assert remote_status_payloads
    assert b'"type":"status_response"' in remote_status_payloads[-1]


def test_core_runner_runs_scheduler_reapply_loop_from_status(monkeypatch) -> None:
    from gatherlink.runtime import runner as runner_module

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    calls = []

    def fake_reapply(dataplane, source_config, runtime_config, status, **kwargs):
        calls.append((dataplane, source_config, runtime_config, status))
        return runtime_config

    monkeypatch.setattr(runner_module, "hot_reapply_scheduler_from_status", fake_reapply)
    run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: FakeDataplane(),
        max_iterations=2,
        batch_size=8,
        source_config=config,
        scheduler_reapply_interval_seconds=0.0,
    )

    assert calls
    assert calls[0][1] is config
    assert calls[0][3]["service_stats"]["udp-main"]["tx_packets"] == 3
    assert calls[0][3]["path_stats"]["path-a"]["tx_packets"] == 1


def test_core_runner_advertises_configured_path_capacity_as_control_metadata(tmp_path, monkeypatch) -> None:
    from gatherlink.paths import capacity as capacity_module

    config = GatherlinkConfig(
        schema_version=1,
        node="capacity-client",
        role="client",
        peer="remote",
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler=PathSchedulerConfig(tx_capacity_bps=300_000_000, rx_capacity_bps=500_000_000),
            )
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    state = CoreRunnerState(
        node="capacity-client",
        security_mode="none",
        service_names=["udp-main"],
        stop_event=Event(),
    )
    monkeypatch.setattr(
        capacity_module,
        "path_capacity_cache_file",
        lambda runtime_config, *, cache_dir=None: tmp_path / "path-capacity-cache.json",
    )

    run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: FakeDataplane(),
        max_iterations=2,
        batch_size=8,
        runner_state=state,
    )

    assert state.control_metadata is not None
    assert state.control_metadata["path_capacity"]["path-a"]["tx_bps"] == 300_000_000
    assert state.control_metadata["path_capacity"]["path-a"]["rx_bps"] == 500_000_000
    assert state.control_metadata["local_scheduler"] == {
        "configured_mode": "round_robin",
        "effective_mode": "round_robin",
        "rust_mode": "round_robin",
        "updated_at": state.control_metadata["local_scheduler"]["updated_at"],
    }


def test_core_runner_prefers_configured_capacity_over_stale_cache(tmp_path, monkeypatch) -> None:
    from gatherlink.paths import capacity as capacity_module

    cache_file = tmp_path / "path-capacity-cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "paths": {
                    "path-a": {
                        "source": "cache",
                        "tx_bps": 100_000_000,
                        "rx_bps": 100_000_000,
                        "updated_at": "2026-05-24T00:00:00Z",
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = GatherlinkConfig(
        schema_version=1,
        node="capacity-client",
        role="client",
        peer="remote",
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler=PathSchedulerConfig(tx_capacity_bps=700_000_000, rx_capacity_bps=500_000_000),
            )
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    state = CoreRunnerState(
        node="capacity-client",
        security_mode="none",
        service_names=["udp-main"],
        stop_event=Event(),
    )
    monkeypatch.setattr(
        capacity_module,
        "path_capacity_cache_file",
        lambda runtime_config, *, cache_dir=None: cache_file,
    )

    run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: FakeDataplane(),
        max_iterations=2,
        batch_size=8,
        runner_state=state,
    )

    assert state.control_metadata is not None
    assert state.control_metadata["path_capacity"]["path-a"]["tx_bps"] == 700_000_000
    assert state.control_metadata["path_capacity"]["path-a"]["rx_bps"] == 500_000_000
    assert state.control_metadata["path_capacity"]["path-a"]["source"] == "config"


def test_core_runner_warns_when_service_id_is_explicit(caplog: pytest.LogCaptureFixture) -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="udp-main",
                service_id=300,
                listen="127.0.0.1:55180",
                target="127.0.0.1:51820",
            )
        ],
    )
    caplog.set_level(logging.WARNING)

    run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: FakeDataplane(),
        max_iterations=1,
        batch_size=8,
    )

    assert "explicit service_id is not recommended" in caplog.text


def test_scheduler_status_reports_python_policy_before_first_coordinator_decision() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="capacity-client",
        role="client",
        peer="capacity-server",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler=PathSchedulerConfig(tx_capacity_bps=300_000_000, rx_capacity_bps=500_000_000),
            )
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    runtime_config = expand_config(config)

    assert _scheduler_status_tuple(config, runtime_config, None) == (
        "coordinated_adaptive",
        "single_best_path",
        "weighted_round_robin",
    )


def test_core_runner_requires_a_listening_service() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="server",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    result = run_core_service(
        expand_config(config),
        dataplane_factory=lambda _runtime_config: PathOnlyFakeDataplane(),
        max_iterations=1,
        batch_size=8,
    )

    assert result.delivered_packets == 1


def test_core_runner_rejects_config_without_listener_or_path() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="server",
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    with pytest.raises(ValueError, match="service listener or path transport"):
        run_core_service(expand_config(config), dataplane_factory=lambda _runtime_config: FakeDataplane())


def test_core_runner_does_not_snapshot_status_every_hot_loop_iteration() -> None:
    class SnapshotCountingDataplane(FakeDataplane):
        def __init__(self) -> None:
            super().__init__()
            self.status_calls = 0

        def status_snapshot(self):
            self.status_calls += 1
            return super().status_snapshot()

    dataplane = SnapshotCountingDataplane()

    run_core_service(
        _runtime_config(),
        dataplane_factory=lambda _runtime_config: dataplane,
        max_iterations=8,
        batch_size=8,
    )

    # Status snapshots build Python dictionaries from Rust counters for IPC and
    # monitoring. They are intentionally sampled, not done on every packet-loop
    # iteration, so metrics cannot become the dataplane throttle again.
    assert dataplane.status_calls < dataplane.calls
