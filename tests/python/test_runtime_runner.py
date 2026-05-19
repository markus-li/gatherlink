from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest
from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig
from gatherlink.runtime.runner import run_core_service


@dataclass
class FakeOutcome:
    length: int

    def payload_len(self) -> int:
        return self.length


class FakeDataplane:
    def __init__(self) -> None:
        self.calls = 0

    def forward_available_for_service(self, service_name: str, batch_size: int):
        self.calls += 1
        assert service_name == "udp-main"
        assert batch_size == 8
        if self.calls == 1:
            return [FakeOutcome(100), FakeOutcome(50)]
        raise RuntimeError("failed to receive UDP datagram: timed out")

    def receive_available_from_paths(self, batch_size: int):
        assert batch_size == 8
        return [FakeOutcome(25)] if self.calls == 1 else []


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

    with pytest.raises(ValueError, match="listen address"):
        run_core_service(expand_config(config), dataplane_factory=lambda _runtime_config: FakeDataplane())
