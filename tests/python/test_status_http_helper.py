from __future__ import annotations

import json
import os
from threading import Thread
from urllib.request import urlopen

from gatherlink.cli.main import app
from gatherlink.helpers.status_http import StatusHttpConfig, build_status_http_server, gather_status_payload
from gatherlink.runtime.services import SERVICE_REGISTRY_ENV, ServiceRecord, ServiceRegistry
from typer.testing import CliRunner


def test_status_http_payload_includes_hidden_services(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    registry = ServiceRegistry()
    registry.register(ServiceRecord(name="core.client-a", kind="core", pid=os.getpid(), log_file=tmp_path / "a.log"))
    registry.register(
        ServiceRecord(name="core.client-a.hidden", kind="core", pid=os.getpid(), log_file=tmp_path / "h.log")
    )

    payload = gather_status_payload(StatusHttpConfig("127.0.0.1", 8765), registry=registry)

    assert payload["listen"] == {"host": "127.0.0.1", "port": 8765}
    assert payload["service_count"] == 2
    assert {service["name"] for service in payload["services"]} == {"core.client-a", "core.client-a.hidden"}
    assert next(service for service in payload["services"] if service["hidden"])["name"] == "core.client-a.hidden"


def test_status_http_server_serves_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    ServiceRegistry().register(
        ServiceRecord(name="lab.local-dual-path.sink.hidden", kind="lab", pid=os.getpid(), log_file=tmp_path / "s.log")
    )
    server = build_status_http_server(StatusHttpConfig("127.0.0.1", 0))
    host, port = server.server_address[:2]
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://{host}:{port}/json", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()

    assert payload["listen"]["host"] == "127.0.0.1"
    assert payload["services"][0]["hidden"] is True


def test_status_http_cli_builds_server(monkeypatch) -> None:
    captured = {}

    def fake_run(config):
        captured["config"] = config

    monkeypatch.setattr("gatherlink.cli.helpers.run_status_http_server", fake_run)
    result = CliRunner().invoke(app, ["helpers", "status-http", "--listen", "127.0.0.1:9999"])

    assert result.exit_code == 0
    assert captured["config"].listen_host == "127.0.0.1"
    assert captured["config"].listen_port == 9999
