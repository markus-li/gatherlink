from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass

import pytest
from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, SchedulerConfig, ServiceConfig
from gatherlink.config.runtime import RuntimePathSchedulerConfig
from gatherlink.dataplane.rust_backend import (
    RustRuntimeBridgeError,
    RustRuntimeDtos,
    build_rust_runtime_dtos,
    reapply_core_scheduler,
)
from gatherlink.scheduling.compiler import compile_scheduler


@dataclass
class FakeUdpServiceConfig:
    name: str
    target: str
    listen: str | None
    priority: int
    return_mode: str
    service_id: int
    scheduler_fanout: int
    scheduler_fanout_below_bytes: int


@dataclass
class FakePathConfig:
    path_id: int
    mtu: int
    route_id: int
    busy: bool
    enabled: bool
    state: str
    weight: int
    tx_capacity_bps: int | None
    rx_capacity_bps: int | None
    latency_us: int | None
    loss_ppm: int
    reorder_hold_us: int
    max_in_flight_packets: int
    max_in_flight_bytes: int
    transport_bind: str | None
    transport_remote: str | None


@dataclass
class FakeSchedulerConfig:
    mode: str


@dataclass
class FakeTransportSecurityConfig:
    mode: str
    receiver_index: int | None = None
    send_key: bytes | None = None
    receive_key: bytes | None = None

    @staticmethod
    def none() -> FakeTransportSecurityConfig:
        return FakeTransportSecurityConfig("none")

    @staticmethod
    def static_keys(receiver_index: int, send_key: bytes, receive_key: bytes) -> FakeTransportSecurityConfig:
        return FakeTransportSecurityConfig("static", receiver_index, send_key, receive_key)


class FakeBindings:
    UdpServiceConfig = FakeUdpServiceConfig
    PathConfig = FakePathConfig
    SchedulerConfig = FakeSchedulerConfig
    TransportSecurityConfig = FakeTransportSecurityConfig


class FakeDataplane:
    def __init__(self) -> None:
        self.calls = []

    def reapply_scheduler(self, paths, scheduler):
        self.calls.append((paths, scheduler))
        return "reapplied"


def test_runtime_config_converts_to_rust_binding_dtos() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="capacity_aware"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                transport_bind="127.0.0.1:56001",
                transport_remote="127.0.0.1:56002",
                scheduler={
                    "weight": 2,
                    "mtu": 1300,
                    "tx_capacity_bps": 3_000_000,
                    "rx_capacity_bps": 1_500_000,
                    "latency_us": 12_000,
                    "loss_ppm": 500,
                    "reorder_hold_us": 6_000,
                    "max_in_flight_packets": 32,
                    "max_in_flight_bytes": 262_144,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820", priority="high")],
    )
    runtime_config = expand_config(config)
    runtime_config.scheduler = compile_scheduler(config)

    dtos = build_rust_runtime_dtos(runtime_config, bindings=FakeBindings)

    assert isinstance(dtos, RustRuntimeDtos)
    assert dtos.scheduler.mode == "capacity_aware"
    assert dtos.security == FakeTransportSecurityConfig.none()
    assert dtos.services == [
        FakeUdpServiceConfig("udp-main", "127.0.0.1:51820", "127.0.0.1:55180", 200, "fixed", 256, 1, 0),
    ]
    assert dtos.paths == [
        FakePathConfig(
            path_id=0,
            mtu=1300,
            route_id=0,
            busy=False,
            enabled=True,
            state="active",
            weight=3,
            tx_capacity_bps=3_000_000,
            rx_capacity_bps=1_500_000,
            latency_us=12_000,
            loss_ppm=500,
            reorder_hold_us=6_000,
            max_in_flight_packets=32,
            max_in_flight_bytes=262_144,
            transport_bind="127.0.0.1:56001",
            transport_remote="127.0.0.1:56002",
        )
    ]


def test_static_security_config_compiles_to_rust_binding_dto_and_inner_mtu() -> None:
    send_key = b64encode(bytes([0x11]) * 32).decode("ascii")
    receive_key = b64encode(bytes([0x22]) * 32).decode("ascii")
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        security={
            "mode": "static",
            "receiver_index": 123,
            "send_key": send_key,
            "receive_key": receive_key,
        },
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                transport_bind="127.0.0.1:56001",
                transport_remote="127.0.0.1:56002",
                scheduler={"mtu": 1200},
            )
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    runtime_config = expand_config(config)

    dtos = build_rust_runtime_dtos(runtime_config, bindings=FakeBindings)

    assert dtos.security == FakeTransportSecurityConfig(
        "static",
        123,
        bytes([0x11]) * 32,
        bytes([0x22]) * 32,
    )
    assert dtos.paths[0].mtu == 1171


def test_reapply_core_scheduler_uses_socket_preserving_binding(monkeypatch) -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")],
    )
    runtime_config = expand_config(config)
    dataplane = FakeDataplane()

    monkeypatch.setattr("gatherlink.dataplane.rust_backend._load_bindings", lambda: FakeBindings)
    outcome = reapply_core_scheduler(dataplane, runtime_config)

    assert outcome == "reapplied"
    assert len(dataplane.calls) == 1
    paths, scheduler = dataplane.calls[0]
    assert len(paths) == 1
    assert scheduler == FakeSchedulerConfig("round_robin")


def test_rust_bridge_rejects_values_that_do_not_fit_rust_dto_widths() -> None:
    scheduler = RuntimePathSchedulerConfig(path_id=2**16, mtu=1200)
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    runtime_config = expand_config(config)
    runtime_config.paths[0].scheduler = scheduler

    with pytest.raises(RustRuntimeBridgeError, match=r"path\.scheduler\.path_id"):
        build_rust_runtime_dtos(runtime_config, bindings=FakeBindings)
