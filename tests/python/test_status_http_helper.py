from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gatherlink.cli.main import app
from gatherlink.diagnostics import DiagnosticsBus
from gatherlink.helpers.status_http import (
    StatusHttpConfig,
    build_status_http_server,
    gather_status_payload,
    publish_status_http_start,
)
from gatherlink.runtime.services import SERVICE_REGISTRY_ENV, ServiceRecord, ServiceRegistry
from typer.testing import CliRunner


class MemorySink:
    def __init__(self) -> None:
        self.events = []

    def write(self, event) -> None:
        self.events.append(event)


def test_status_http_payload_includes_hidden_services(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    registry = ServiceRegistry()
    registry.register(ServiceRecord(name="core.client-a", kind="core", pid=os.getpid(), log_file=tmp_path / "a.log"))
    registry.register(
        ServiceRecord(name="core.client-a.hidden", kind="core", pid=os.getpid(), log_file=tmp_path / "h.log")
    )

    payload = gather_status_payload(StatusHttpConfig("127.0.0.1", 8765), registry=registry)

    assert payload["api"]["label"] == "EXPERIMENTAL"
    assert payload["api"]["writes_implemented"] is False
    assert payload["api"]["write_window_seconds"] == 3600
    assert payload["listen"] == {"host": "127.0.0.1", "port": 8765}
    assert payload["service_count"] == 2
    assert {service["name"] for service in payload["services"]} == {"core.client-a", "core.client-a.hidden"}
    assert next(service for service in payload["services"] if service["hidden"])["name"] == "core.client-a.hidden"


def test_status_http_payload_redacts_service_metadata_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    registry = ServiceRegistry()
    registry.register(
        ServiceRecord(
            name="core.client-a",
            kind="core",
            pid=os.getpid(),
            log_file=tmp_path / "a.log",
            metadata={
                "peer": "node-b",
                "session_key": "super-secret",
                "nested": {"api_token": "token-value"},
            },
        )
    )

    payload = gather_status_payload(StatusHttpConfig("127.0.0.1", 8765), registry=registry)
    metadata = payload["services"][0]["metadata"]

    assert metadata["peer"] == "node-b"
    assert metadata["session_key"] == "[redacted:12 chars]"
    assert metadata["nested"]["api_token"] == "[redacted:11 chars]"
    assert "super-secret" not in json.dumps(payload)
    assert "token-value" not in json.dumps(payload)


def test_status_http_write_window_expires_without_stopping_read_apis(tmp_path) -> None:
    config = StatusHttpConfig(
        "127.0.0.1",
        8765,
        write_window_seconds=3600,
        started_at=datetime.now(UTC) - timedelta(seconds=3601),
    )

    payload = gather_status_payload(config, registry=ServiceRegistry(path=tmp_path / "missing-services"))

    assert payload["api"]["writes_enabled"] is False
    assert payload["api"]["writes_implemented"] is False
    assert payload["services"] == []


def test_status_http_server_serves_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    ServiceRegistry().register(
        ServiceRecord(name="lab.local-dual-path.sink.hidden", kind="lab", pid=os.getpid(), log_file=tmp_path / "s.log")
    )
    server = build_status_http_server(StatusHttpConfig("127.0.0.1", 0))
    host, port = server.server_address[:2]
    post_status = None
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://{host}:{port}/json", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://{host}:{port}/v1/status", timeout=2) as response:
            v1_payload = json.loads(response.read().decode("utf-8"))
        request = Request(f"http://{host}:{port}/v1/status", method="POST")
        try:
            urlopen(request, timeout=2)
        except HTTPError as exc:
            post_status = exc.code
    finally:
        server.shutdown()
        server.server_close()

    assert payload["listen"]["host"] == "127.0.0.1"
    assert payload["services"][0]["hidden"] is True
    assert v1_payload["api"]["label"] == "EXPERIMENTAL"
    assert post_status == 405


def test_status_http_post_fails_closed_after_write_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    config = StatusHttpConfig(
        "127.0.0.1",
        0,
        write_window_seconds=1,
        started_at=datetime.now(UTC) - timedelta(seconds=2),
    )
    server = build_status_http_server(config)
    host, port = server.server_address[:2]
    post_status = None
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = Request(f"http://{host}:{port}/v1/status", method="POST")
        try:
            urlopen(request, timeout=2)
        except HTTPError as exc:
            post_status = exc.code
    finally:
        server.shutdown()
        server.server_close()

    assert post_status == 403


def test_status_http_cli_builds_server(monkeypatch) -> None:
    captured = {}

    def fake_run(config, *, diagnostics_bus=None):
        captured["config"] = config
        captured["diagnostics_bus"] = diagnostics_bus

    monkeypatch.setattr("gatherlink.cli.helpers.run_status_http_server", fake_run)
    result = CliRunner().invoke(app, ["helpers", "status-http", "--listen", "127.0.0.1:9999"])

    assert result.exit_code == 0
    assert captured["config"].listen_host == "127.0.0.1"
    assert captured["config"].listen_port == 9999
    assert captured["diagnostics_bus"] is None


def test_status_http_cli_wires_jsonl_diagnostics(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run(config, *, diagnostics_bus=None):
        captured["config"] = config
        captured["diagnostics_bus"] = diagnostics_bus

    monkeypatch.setattr("gatherlink.cli.helpers.run_status_http_server", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "status-http",
            "--listen",
            "127.0.0.1:9999",
            "--diagnostics-jsonl",
            str(tmp_path / "status-http.jsonl"),
        ],
    )

    assert result.exit_code == 0
    assert captured["diagnostics_bus"] is not None


def test_status_http_start_publishes_structured_diagnostics() -> None:
    sink = MemorySink()
    bus = DiagnosticsBus(sinks=[sink])
    config = StatusHttpConfig("0.0.0.0", 9999, allow_non_loopback=True)

    publish_status_http_start(config, diagnostics_bus=bus)

    bus.drain()
    events = sink.events
    assert [event.code for event in events] == ["helper.status_http.started", "helper.status_http.non_loopback_bind"]
    assert events[0].details["listen_host"] == "0.0.0.0"
    assert events[1].severity == "warning"


def test_status_http_rejects_non_loopback_without_danger_flag() -> None:
    result = CliRunner().invoke(app, ["helpers", "status-http", "--listen", "0.0.0.0:9999"])

    assert result.exit_code != 0
    assert "loopback only" in result.output


def test_status_http_allows_non_loopback_with_danger_flag(monkeypatch) -> None:
    captured = {}

    def fake_run(config, *, diagnostics_bus=None):
        captured["config"] = config

    monkeypatch.setattr("gatherlink.cli.helpers.run_status_http_server", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "status-http",
            "--listen",
            "0.0.0.0:9999",
            "--allow-non-loopback",
            "--write-window-seconds",
            "10",
        ],
    )

    assert result.exit_code == 0
    assert captured["config"].allow_non_loopback is True
    assert captured["config"].write_window_seconds == 10
    assert "DANGER" in result.output
