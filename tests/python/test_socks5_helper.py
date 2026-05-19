from __future__ import annotations

import asyncio
from dataclasses import dataclass

from asyncio_socks_server import Address, Connection
from gatherlink.cli.main import app
from gatherlink.diagnostics import DiagnosticsBus
from gatherlink.helpers.socks5 import (
    GatherlinkServiceExitConnector,
    GatherlinkSocks5Addon,
    Socks5ExitConnector,
    Socks5Policy,
    build_socks5_server,
)
from gatherlink.helpers.transport import MissingGatherlinkTransportError
from gatherlink.helpers.udp_stream import GatherlinkUdpStreamTransport
from typer.testing import CliRunner


@dataclass
class FakeDestination:
    host: str
    port: int


@dataclass
class FakeFlow:
    dst: FakeDestination
    bytes_up: int = 0
    bytes_down: int = 0


class FakeConnector(Socks5ExitConnector):
    def __init__(self) -> None:
        self.opened: list[tuple[str, int, float]] = []

    async def open(self, host: str, port: int, *, timeout_seconds: float) -> Connection:
        self.opened.append((host, port, timeout_seconds))
        return Connection(reader=None, writer=None, address=Address("127.0.0.1", 50000))


def test_socks5_policy_denies_by_default() -> None:
    decision = Socks5Policy().decide_connect("example.test", 443)

    assert decision.allowed is False
    assert "host is not allowed" in decision.reason


def test_socks5_policy_allows_explicit_host_and_port() -> None:
    policy = Socks5Policy.allow(hosts=["Example.Test"], ports=[443])

    decision = policy.decide_connect("example.test", 443)

    assert decision.allowed is True


def test_socks5_addon_uses_exit_connector_for_allowed_connect() -> None:
    connector = FakeConnector()
    bus = DiagnosticsBus()
    addon = GatherlinkSocks5Addon(
        policy=Socks5Policy.allow(hosts=["example.test"], ports=[443]),
        exit_connector=connector,
        diagnostics_bus=bus,
    )

    connection = asyncio.run(addon.on_connect(FakeFlow(FakeDestination("example.test", 443))))

    assert connection.address.host == "127.0.0.1"
    assert connector.opened == [("example.test", 443, 10.0)]
    assert addon.stats.accepted == 1
    assert addon.stats.opened == 1
    assert bus.queued_events == 1


def test_socks5_addon_rejects_denied_connect() -> None:
    bus = DiagnosticsBus()
    addon = GatherlinkSocks5Addon(
        policy=Socks5Policy.allow(hosts=["example.test"], ports=[443]),
        diagnostics_bus=bus,
    )

    try:
        asyncio.run(addon.on_connect(FakeFlow(FakeDestination("blocked.test", 443))))
    except PermissionError:
        pass
    else:
        raise AssertionError("expected denied SOCKS5 CONNECT to raise PermissionError")

    assert addon.stats.denied == 1
    assert bus.queued_events == 1


def test_socks5_default_exit_requires_gatherlink_transport() -> None:
    connector = GatherlinkServiceExitConnector()

    try:
        asyncio.run(connector.open("example.test", 443, timeout_seconds=0.1))
    except MissingGatherlinkTransportError:
        pass
    else:
        raise AssertionError("expected unconfigured production connector to require Gatherlink transport")


def test_build_socks5_server_uses_library_server_with_addon() -> None:
    server = build_socks5_server(
        listen_host="127.0.0.1",
        listen_port=1080,
        policy=Socks5Policy.allow(hosts=["example.test"], ports=[443]),
    )

    assert server.host == "127.0.0.1"
    assert server.port == 1080


def test_socks5_cli_requires_allow_lists(monkeypatch) -> None:
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("gatherlink.cli.helpers.run_lab_direct_socks5_server", fake_run)

    result = CliRunner().invoke(app, ["helpers", "socks5-serve"])

    assert result.exit_code != 0
    assert called is False


def test_socks5_cli_starts_with_explicit_policy(monkeypatch) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("gatherlink.cli.helpers.run_lab_direct_socks5_server", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "socks5-serve",
            "--listen",
            "127.0.0.1:1081",
            "--allow-host",
            "example.test",
            "--allow-port",
            "443",
            "--lab-direct",
        ],
    )

    assert result.exit_code == 0
    assert captured["listen_host"] == "127.0.0.1"
    assert captured["listen_port"] == 1081
    assert captured["allowed_hosts"] == ["example.test"]
    assert captured["allowed_ports"] == [443]


def test_socks5_cli_defaults_to_gatherlink_transport_runner(monkeypatch) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("gatherlink.cli.helpers.run_socks5_server", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "socks5-serve",
            "--allow-host",
            "example.test",
            "--allow-port",
            "443",
        ],
    )

    assert result.exit_code == 0
    assert captured["allowed_hosts"] == ["example.test"]


def test_socks5_cli_can_use_gatherlink_udp_service_transport(monkeypatch) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("gatherlink.cli.helpers.run_socks5_server", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "socks5-serve",
            "--allow-host",
            "example.test",
            "--allow-port",
            "443",
            "--gatherlink-service",
            "127.0.0.1:55180",
        ],
    )

    assert result.exit_code == 0
    connector = captured["exit_connector"]
    assert isinstance(connector, GatherlinkServiceExitConnector)
    assert isinstance(connector.transport, GatherlinkUdpStreamTransport)
    assert connector.transport.service_host == "127.0.0.1"
    assert connector.transport.service_port == 55180


def test_socks5_cli_wires_jsonl_diagnostics(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        kwargs["diagnostics_bus"].publish_warning("socks5 warning")

    monkeypatch.setattr("gatherlink.cli.helpers.run_socks5_server", fake_run)
    output = tmp_path / "socks5.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "socks5-serve",
            "--allow-host",
            "example.test",
            "--allow-port",
            "443",
            "--diagnostics-jsonl",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert captured["diagnostics_bus"] is not None
    assert '"message":"socks5 warning"' in output.read_text(encoding="utf-8")
