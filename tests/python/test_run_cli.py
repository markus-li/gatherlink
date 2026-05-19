from __future__ import annotations

import json
import os
from pathlib import Path

from gatherlink.cli.main import app
from typer.testing import CliRunner

EXAMPLES = Path("configs/examples")


def test_run_plan_cli_prints_core_userland_udp_plan() -> None:
    result = CliRunner().invoke(app, ["run", "plan", str(EXAMPLES / "minimal-client.json")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["transport_target"] == "core-userland-udp"
    assert payload["requires_root"] is False
    assert "security.mode=none" in payload["warnings"][0]
    assert payload["steps"][0]["details"]["security_mode"] == "none"
    assert payload["steps"][1]["action"] == "bind_udp_listener"


def test_run_service_cli_reports_missing_rust_bindings(monkeypatch) -> None:
    from gatherlink.cli import run as run_cli
    from gatherlink.dataplane.rust_backend import RustDataplaneUnavailableError

    def fake_run_core_service(*_args, **_kwargs):
        raise RustDataplaneUnavailableError("Rust dataplane bindings are not installed")

    monkeypatch.setattr(run_cli, "run_core_service", fake_run_core_service)
    result = CliRunner().invoke(
        app,
        ["run", "service", str(EXAMPLES / "minimal-client.json"), "--max-iterations", "1"],
    )

    assert result.exit_code == 1
    assert "Rust dataplane bindings are not installed" in result.output


def test_run_service_cli_writes_start_failure_to_jsonl(monkeypatch, tmp_path) -> None:
    from gatherlink.cli import run as run_cli
    from gatherlink.dataplane.rust_backend import RustDataplaneUnavailableError

    output = tmp_path / "events.jsonl"

    def fake_run_core_service(*_args, **_kwargs):
        raise RustDataplaneUnavailableError("Rust dataplane bindings are not installed")

    monkeypatch.setattr(run_cli, "run_core_service", fake_run_core_service)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "service",
            str(EXAMPLES / "minimal-client.json"),
            "--max-iterations",
            "1",
            "--diagnostics-jsonl",
            str(output),
        ],
    )

    assert result.exit_code == 1
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["code"] == "runtime.start_failed"
    assert rows[0]["severity"] == "error"
    assert rows[0]["details"]["error_type"] == "RustDataplaneUnavailableError"


def test_run_service_cli_wires_jsonl_diagnostics(monkeypatch, tmp_path) -> None:
    from gatherlink.cli import run as run_cli
    from gatherlink.diagnostics.events import DiagnosticEvent
    from gatherlink.runtime.runner import CoreRunnerResult

    output = tmp_path / "events.jsonl"

    def fake_run_core_service(_runtime_config, **kwargs):
        diagnostics_bus = kwargs["diagnostics_bus"]
        diagnostics_bus.publish(DiagnosticEvent.warning("plain test warning"))
        diagnostics_bus.drain()
        return CoreRunnerResult(1, 0, 0, 0, 0)

    monkeypatch.setattr(run_cli, "run_core_service", fake_run_core_service)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "service",
            str(EXAMPLES / "minimal-client.json"),
            "--max-iterations",
            "1",
            "--diagnostics-jsonl",
            str(output),
        ],
    )

    assert result.exit_code == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["code"] == "warning"
    assert rows[0]["message"] == "plain test warning"


def test_run_start_registers_process_managed_core_service(monkeypatch, tmp_path) -> None:
    from gatherlink.cli import run as run_cli
    from gatherlink.runtime.services import SERVICE_REGISTRY_ENV, ServiceRegistry

    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    launched = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            launched["command"] = command
            launched["kwargs"] = kwargs
            self.pid = os.getpid()

    monkeypatch.setattr(run_cli.subprocess, "Popen", FakePopen)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "start",
            str(EXAMPLES / "minimal-client.json"),
            "--name",
            "core.test-client",
            "--scheduler-reapply-interval",
            "5",
        ],
    )

    record = ServiceRegistry().resolve("core.test-client")
    assert result.exit_code == 0
    assert record.pid == os.getpid()
    assert record.kind == "core"
    assert "--service-name" in launched["command"]
    assert "core.test-client" in launched["command"]
    assert "--scheduler-reapply-interval" in launched["command"]
    assert launched["kwargs"]["start_new_session"] is True


def test_run_start_closes_existing_process_service_before_replacing(monkeypatch, tmp_path) -> None:
    from gatherlink.cli import run as run_cli
    from gatherlink.runtime.services import SERVICE_REGISTRY_ENV, ServiceRecord, ServiceRegistry

    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(tmp_path / "services"))
    registry = ServiceRegistry()
    registry.register(
        ServiceRecord(
            name="core.test-client",
            kind="core",
            pid=os.getpid(),
            log_file=tmp_path / "old.log",
        )
    )
    close_calls = []
    launched = {}

    def fake_close(self, query):
        close_calls.append(query)
        self.mark_stopped(query)
        return self.resolve(query)

    class FakePopen:
        def __init__(self, command, **kwargs):
            launched["command"] = command
            launched["kwargs"] = kwargs
            self.pid = os.getpid()

    monkeypatch.setattr(ServiceRegistry, "close", fake_close)
    monkeypatch.setattr(run_cli.subprocess, "Popen", FakePopen)

    result = CliRunner().invoke(
        app,
        ["run", "start", str(EXAMPLES / "minimal-client.json"), "--name", "core.test-client"],
    )

    assert result.exit_code == 0
    assert close_calls == ["core.test-client"]
    assert launched["command"]
