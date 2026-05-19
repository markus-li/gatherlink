from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event

from gatherlink.cli.main import app
from gatherlink.runtime.services import (
    SERVICE_REGISTRY_ENV,
    ServiceIpcServer,
    ServiceRecord,
    ServiceRegistry,
    iter_log_lines,
    request_service,
    service_name,
)
from typer.testing import CliRunner


def test_service_registry_auto_names_and_resolves_records(tmp_path: Path) -> None:
    registry = ServiceRegistry(tmp_path / "services")
    first = registry.register(
        ServiceRecord(
            name="lab.local-dual-path",
            kind="lab",
            pid=os.getpid(),
            pid_file=tmp_path / "service.pid",
            log_file=tmp_path / "service.log",
        )
    )
    second = registry.register(
        ServiceRecord(
            name="lab.local-dual-path",
            kind="lab",
            pid=os.getpid(),
            pid_file=tmp_path / "service-2.pid",
            log_file=tmp_path / "service-2.log",
        ),
        replace=False,
    )

    assert first.name == "lab.local-dual-path"
    assert second.name == "lab.local-dual-path-2"
    assert (
        registry.resolve("local-dual-path-2").pid_file
        == tmp_path / "services" / "lab.local-dual-path-2" / "current.pid"
    )
    assert (tmp_path / "services" / "lab.local-dual-path" / "service.json").exists()
    assert (tmp_path / "services" / "lab.local-dual-path" / "current.pid").read_text(encoding="utf-8").strip()


def test_service_registry_cleans_stale_process_pid(tmp_path: Path) -> None:
    registry = ServiceRegistry(tmp_path / "services")
    record = registry.register(
        ServiceRecord(
            name="helper.dead",
            kind="helper",
            pid=999_999_999,
            log_file=tmp_path / "dead.log",
        )
    )

    records = registry.list()

    assert records[0].name == "helper.dead"
    assert records[0].current_pid() is None
    assert records[0].metadata["last_status"] == "stale_pid_cleaned"
    assert record.pid_file is not None
    assert not record.pid_file.exists()


def test_service_registry_prunes_stopped_process_records(tmp_path: Path) -> None:
    registry = ServiceRegistry(tmp_path / "services")
    stopped = registry.register(
        ServiceRecord(
            name="core.stopped",
            kind="core",
            pid=999_999_999,
            log_file=tmp_path / "stopped.log",
        )
    )
    running = registry.register(
        ServiceRecord(
            name="core.running",
            kind="core",
            pid=os.getpid(),
            log_file=tmp_path / "running.log",
        )
    )

    assert registry.prune_stopped() == ["core.stopped"]

    remaining = registry.list()
    assert [service.name for service in remaining] == ["core.running"]
    assert not (tmp_path / "services" / "core.stopped").exists()
    assert (tmp_path / "services" / "core.running").exists()
    assert stopped.name == "core.stopped"
    assert running.name == "core.running"


def test_service_ipc_status_and_stop(tmp_path: Path) -> None:
    registry = ServiceRegistry(tmp_path / "services")
    stop_event = Event()
    record = registry.register(
        ServiceRecord(
            name="lab.ipc",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "ipc.log",
        )
    )
    server = ServiceIpcServer(
        record,
        status=lambda: {"packets": 3, "running": not stop_event.is_set()},
        stop=stop_event.set,
    )
    server.start()
    try:
        assert request_service(record, "status")["status"] == {"packets": 3, "running": True}
        assert request_service(record, "stop")["status"] == "stopping"
        assert stop_event.is_set()
    finally:
        server.close()


def test_service_ipc_status_works_while_custom_command_runs(tmp_path: Path) -> None:
    registry = ServiceRegistry(tmp_path / "services")
    command_started = Event()
    release_command = Event()
    record = registry.register(
        ServiceRecord(
            name="lab.concurrent-ipc",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "ipc.log",
        )
    )

    def slow_command(_request: dict[str, object]) -> dict[str, object]:
        command_started.set()
        release_command.wait(timeout=2)
        return {"done": True}

    server = ServiceIpcServer(
        record,
        status=lambda: {"running": True},
        stop=lambda: None,
        commands={"slow": slow_command},
    )
    server.start()
    try:
        from threading import Thread

        result: dict[str, object] = {}

        def run_slow_command() -> None:
            result.update(request_service(record, "slow", timeout_seconds=3))

        worker = Thread(target=run_slow_command, daemon=True)
        worker.start()
        assert command_started.wait(timeout=1)

        status_started_at = time.monotonic()
        assert request_service(record, "status")["status"] == {"running": True}
        assert time.monotonic() - status_started_at < 1

        release_command.set()
        worker.join(timeout=3)
        assert result["result"] == {"done": True}
    finally:
        release_command.set()
        server.close()


def test_iter_log_lines_tails_existing_log(tmp_path: Path) -> None:
    log_file = tmp_path / "service.log"
    log_file.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert list(iter_log_lines(log_file, tail=2)) == ["two", "three"]


def test_services_cli_lists_and_reads_logs(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    log_file = tmp_path / "service.log"
    log_file.write_text("ready\nforwarded packet=1\n", encoding="utf-8")
    ServiceRegistry(registry_path).register(
        ServiceRecord(
            name=service_name("lab", "local-dual-path"),
            kind="lab",
            pid=os.getpid(),
            pid_file=tmp_path / "service.pid",
            log_file=log_file,
        )
    )
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))

    list_result = CliRunner().invoke(app, ["services", "list"])
    logs_result = CliRunner().invoke(app, ["services", "logs", "local-dual-path", "--tail", "1"])

    assert list_result.exit_code == 0
    assert "lab.local-dual-path" in list_result.output
    assert "state=running" in list_result.output
    assert logs_result.exit_code == 0
    assert logs_result.output.strip() == "forwarded packet=1"


def test_services_cli_status_uses_service_ipc(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    record = ServiceRegistry(registry_path).register(
        ServiceRecord(
            name="lab.local-dual-path",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "service.log",
        )
    )
    server = ServiceIpcServer(record, status=lambda: {"packets": 7}, stop=lambda: None)
    server.start()
    try:
        result = CliRunner().invoke(app, ["services", "status", "local-dual-path"])
    finally:
        server.close()

    assert result.exit_code == 0
    assert '"packets": 7' in result.output


def test_services_cli_attach_can_render_aggregate_once(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    record = ServiceRegistry(registry_path).register(
        ServiceRecord(
            name="lab.local-dual-path",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "service.log",
        )
    )
    server = ServiceIpcServer(
        record,
        status=lambda: {"packets": 7, "bytes": 42, "running": True, "current_speed_bps": 210},
        stop=lambda: None,
    )
    server.start()
    try:
        result = CliRunner().invoke(app, ["services", "attach", "local-dual-path", "--mode", "aggregate", "--once"])
    finally:
        server.close()

    assert result.exit_code == 0
    assert "Gatherlink service monitor" in result.output
    assert "lab.local-dual-path" in result.output
    assert "txp" in result.output
    assert "rxp" in result.output
    assert "service time/control" in result.output
    assert "sys" in result.output
    assert "gl" in result.output
    assert "ntp" in result.output
    assert "42B" in result.output
    assert "1.6Kibit/s" in result.output
    assert "legend: - means" in result.output
    assert "speed bit/s" in result.output
    assert "binary" in result.output
    assert "b toggles" in result.output
    assert "m toggles" in result.output


def test_services_cli_monitor_can_render_multiple_aggregate_rows(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    registry = ServiceRegistry(registry_path)
    forwarder = registry.register(
        ServiceRecord(
            name="lab.tx",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "service.log",
        )
    )
    sink = registry.register(
        ServiceRecord(
            name="lab.rx",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "sink.log",
        )
    )
    forwarder_server = ServiceIpcServer(
        forwarder,
        status=lambda: {
            "packets": 2,
            "bytes": 10,
            "running": True,
            "target": "127.0.0.1:51820",
            "path_stats": {
                "path-a": {"packets": 1, "bytes": 5},
                "path-b": {"packets": 1, "bytes": 5},
            },
            "control_metadata": {
                "sent": {"frames": 2, "messages": 4, "bytes": 128, "last_at": "2026-05-17T09:48:11+00:00"},
                "received": {"frames": 1, "messages": 2, "bytes": 64, "last_at": "2026-05-17T09:48:12+00:00"},
                "path_control": {
                    "path-a": {
                        "tx": {"frames": 2, "messages": 4, "bytes": 128},
                        "rx": {"frames": 1, "messages": 2, "bytes": 64},
                    }
                },
                "path_control_count": 1,
                "path_capacity": {"path-a": {"tx_bps": 3_000_000, "rx_bps": 1_500_000}},
            },
        },
        stop=lambda: None,
    )
    sink_server = ServiceIpcServer(
        sink,
        status=lambda: {
            "packets": 2,
            "bytes": 4096,
            "running": True,
            "listen": "127.0.0.1:51820",
            "last_payload": "hello" * 20,
            "last_payload_bytes": 4096,
            "last_source": "('127.0.0.1', 55180)",
        },
        stop=lambda: None,
    )
    forwarder_server.start()
    sink_server.start()
    try:
        result = CliRunner().invoke(
            app,
            ["services", "monitor", "lab.tx", "lab.rx", "--once"],
        )
    finally:
        forwarder_server.close()
        sink_server.close()

    assert result.exit_code == 0
    assert "Gatherlink service monitor" in result.output
    assert "lab.tx" in result.output
    assert "path:path-a" in result.output
    assert "path:path-b" in result.output
    assert "lab.rx" in result.output
    assert "4.0KiB" in result.output
    assert ("hello" * 20) not in result.output
    assert "last=4096B" in result.output
    assert "..." in result.output
    assert "reord" in result.output
    assert "tx/s" in result.output
    assert "rx/s" in result.output
    assert "service time/control" in result.output
    assert "path control" in result.output
    assert "ctx" in result.output
    assert "crx" in result.output
    assert "2/128B" in result.output
    assert "1/64B" in result.output
    assert "tx=3.0Mb rx=1.5Mb" in result.output


def test_services_cli_monitor_handles_stopped_records_with_new_counter_columns(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    ServiceRegistry(registry_path).register(
        ServiceRecord(
            name="lab.stopped",
            kind="lab",
            pid=999_999,
            log_file=tmp_path / "stopped.log",
        )
    )

    result = CliRunner().invoke(app, ["services", "monitor", "lab.stopped", "--once"])

    assert result.exit_code == 0
    assert "lab.stopped" in result.output
    assert "xdup" in result.output
    assert "ffail" in result.output


def test_services_cli_prune_removes_stopped_records(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    ServiceRegistry(registry_path).register(
        ServiceRecord(
            name="core.stopped",
            kind="core",
            pid=999_999_999,
            log_file=tmp_path / "stopped.log",
        )
    )

    result = CliRunner().invoke(app, ["services", "prune"])

    assert result.exit_code == 0
    assert "pruned core.stopped" in result.output
    assert "services: none" in CliRunner().invoke(app, ["services", "list"]).output


def test_service_monitor_requests_temporary_control_cadence(tmp_path: Path, monkeypatch) -> None:
    from gatherlink.cli.services import _request_monitor_control_cadence
    from gatherlink.control import MONITOR_CONTROL_REQUEST_TTL_SECONDS

    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    requests = []
    record = ServiceRegistry(registry_path).register(
        ServiceRecord(
            name="lab.monitor-cadence",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "service.log",
        )
    )
    server = ServiceIpcServer(
        record,
        status=lambda: {"running": True},
        stop=lambda: None,
        commands={
            "control-cadence": lambda request: requests.append(request) or {"profile": request["profile"]},
        },
    )
    server.start()
    try:
        _request_monitor_control_cadence(record)
    finally:
        server.close()

    assert requests
    assert requests[0]["profile"] == "monitor"
    assert requests[0]["ttl_seconds"] == MONITOR_CONTROL_REQUEST_TTL_SECONDS


def test_services_cli_close_uses_service_ipc_and_clears_pid(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    stop_event = Event()
    record = ServiceRegistry(registry_path).register(
        ServiceRecord(
            name="lab.local-dual-path",
            kind="lab",
            pid=os.getpid(),
            log_file=tmp_path / "service.log",
        )
    )
    server = ServiceIpcServer(record, status=lambda: {}, stop=stop_event.set)
    server.start()
    try:
        result = CliRunner().invoke(app, ["services", "close", "local-dual-path"])
    finally:
        server.close()
    service = ServiceRegistry(registry_path).resolve("local-dual-path")

    assert result.exit_code == 0
    assert stop_event.is_set()
    assert service.current_pid() is None
    assert service.metadata["last_status"] == "stopped"


def test_service_registry_close_escalates_until_detached_process_exits(tmp_path: Path, monkeypatch) -> None:
    from gatherlink.runtime import services as service_module

    registry = ServiceRegistry(tmp_path / "services")
    record = registry.register(
        ServiceRecord(
            name="core.stubborn",
            kind="core",
            pid=123_456,
            log_file=tmp_path / "service.log",
        )
    )
    kill_calls: list[tuple[int, int]] = []
    wait_results = iter([False, False, True])

    monkeypatch.setattr(service_module, "pid_is_running", lambda pid: pid == 123_456)
    monkeypatch.setattr(service_module.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setattr(service_module, "wait_for_pid_exit", lambda _pid, *, timeout_seconds: next(wait_results))

    closed = registry.close(record.name)

    assert closed.name == "core.stubborn"
    assert kill_calls == [
        (123_456, service_module.signal.SIGTERM),
        (123_456, service_module.signal.SIGTERM),
        (123_456, service_module.signal.SIGKILL),
    ]
    assert registry.resolve(record.name).current_pid() is None


def test_services_cli_lists_systemd_records_without_process_ownership(tmp_path: Path, monkeypatch) -> None:
    class FakeBackend:
        def systemd_is_active(self, unit: str) -> bool:
            return unit == "gatherlink.service"

    registry_path = tmp_path / "services"
    ServiceRegistry(registry_path).register(
        ServiceRecord(
            name="core.gatherlink",
            kind="core",
            manager="systemd",
            systemd_unit="gatherlink.service",
            detached_from_console=False,
            log_file=tmp_path / "systemd-placeholder.log",
        )
    )
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))
    monkeypatch.setattr("gatherlink.runtime.services.default_debian_backend", lambda: FakeBackend())

    list_result = CliRunner().invoke(app, ["services", "list"])
    close_result = CliRunner().invoke(app, ["services", "close", "core.gatherlink"])

    assert list_result.exit_code == 0
    assert "manager=systemd" in list_result.output
    assert "state=systemd:active" in list_result.output
    assert "systemd_unit=gatherlink.service" in list_result.output
    assert "detached=False" in list_result.output
    assert close_result.exit_code == 1
    assert "managed by systemd unit gatherlink.service" in close_result.output


def test_services_cli_can_register_systemd_record(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))

    result = CliRunner().invoke(
        app,
        ["services", "register-systemd", "core.gatherlink", "gatherlink.service", "--kind", "core"],
    )
    records = ServiceRegistry(registry_path).list()

    assert result.exit_code == 0
    assert records[0].manager == "systemd"
    assert records[0].systemd_unit == "gatherlink.service"
    assert records[0].detached_from_console is False


def test_services_cli_registers_lab_config_as_systemd(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))

    result = CliRunner().invoke(
        app,
        ["services", "register", "configs/lab/local-dual-path.json", "--systemd"],
    )
    records = ServiceRegistry(registry_path).list()

    assert result.exit_code == 0
    assert records[0].name == "lab.local-dual-path"
    assert records[0].kind == "lab"
    assert records[0].manager == "systemd"
    assert records[0].systemd_unit == "gatherlink-lab@local-dual-path.service"
    assert records[0].metadata["config"] == "configs/lab/local-dual-path.json"


def test_services_cli_registers_core_config_as_systemd(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "services"
    monkeypatch.setenv(SERVICE_REGISTRY_ENV, str(registry_path))

    result = CliRunner().invoke(
        app,
        ["services", "register", "configs/examples/minimal-client.json", "--systemd"],
    )
    records = ServiceRegistry(registry_path).list()

    assert result.exit_code == 0
    assert records[0].name == "core.client"
    assert records[0].kind == "core"
    assert records[0].manager == "systemd"
    assert records[0].systemd_unit == "gatherlink@client.service"
    assert records[0].metadata["config"] == "configs/examples/minimal-client.json"
