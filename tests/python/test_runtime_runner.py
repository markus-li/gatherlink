from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event

import pytest
from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig
from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.protocol import SERVICE_ID_CONTROL_METADATA, encode_control_payload
from gatherlink.runtime.runner import CoreRunnerState, run_core_service


@dataclass
class FakeOutcome:
    length: int

    def payload_len(self) -> int:
        return self.length


class FakeDataplane:
    def __init__(self) -> None:
        self.calls = 0
        self.blocking_calls = 0

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
            "path_stats": {"0": {"tx_packets": 1, "tx_bytes": 10, "rx_packets": 2, "rx_bytes": 20}},
            "control_metadata": {"sent": {"frames": 1, "bytes": 32}},
            "disabled_services": {},
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
        self.scheduler_policies: list[tuple[int, int, int]] = []
        self._reserved_events = [
            FakeReservedEvent(
                SERVICE_ID_CONTROL_METADATA,
                1,
                10,
                encode_control_payload(
                    {1: "path-a"},
                    service_disables={256: "peer disabled test service"},
                    service_scheduler_policies={256: (2, 96)},
                ),
            )
        ]

    def drain_reserved_service_events(self):
        events = self._reserved_events
        self._reserved_events = []
        return events

    def disable_service(self, service_id: int, reason: str) -> None:
        self.disabled_services.append((service_id, reason))

    def set_service_scheduler(self, service_id: int, fanout: int, fanout_below_bytes: int) -> None:
        self.scheduler_policies.append((service_id, fanout, fanout_below_bytes))


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
    assert status["path_stats"]["path-a"]["tx_bytes"] == 10
    assert status["control_metadata"]["sent"]["frames"] == 1


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
    assert dataplane.scheduler_policies == [(256, 2, 96)]
    assert status["control_metadata"]["received"]["frames"] == 1
    assert status["control_metadata"]["service_disables"]["256"] == "peer disabled test service"
    assert status["control_metadata"]["service_scheduler_policies"]["256"] == {
        "fanout": 2,
        "fanout_below_bytes": 96,
    }


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

    def fake_reapply(dataplane, source_config, runtime_config, status):
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
    assert calls[0][3]["path_stats"]["path-a"]["tx_packets"] == 1


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
