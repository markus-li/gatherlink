from __future__ import annotations

import json
import socket
from pathlib import Path
from threading import Thread

from gatherlink.cli.main import app
from gatherlink.lab import (
    LabShapeConfig,
    apply_lab_profile,
    apply_lab_shape,
    apply_lab_shape_profile,
    cleanup_lab_runtime,
    clear_lab_shape,
    inspect_lab_interfaces,
    load_lab_scenario_file,
    load_lab_shape_profile_file,
    plan_lab_scenario,
    prepare_lab_runtime,
    read_service_status,
    send_udp_packets,
)
from typer.testing import CliRunner

LAB_CONFIGS = Path("configs/lab")
SHAPING_CONFIGS = LAB_CONFIGS / "shaping"


def test_local_dual_path_lab_config_plans_extensible_scenario() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    plan = plan_lab_scenario(scenario)

    assert scenario.security.mode == "none"
    assert len(scenario.paths) == 2
    assert "rate-10mbit" in scenario.profiles
    assert plan.supported is False
    assert plan.steps[0].status == "supported"
    assert any(step.action == "setup_network_namespaces_and_veths" for step in plan.steps)
    assert any(step.action == "future_feature:receiver-metrics" for step in plan.steps)
    assert any(step.status == "not_implemented" for step in plan.steps)
    assert "security.mode=none" in plan.warnings[0]


def test_future_scenario_kind_reports_not_implemented() -> None:
    data = json.loads((LAB_CONFIGS / "local-dual-path.json").read_text(encoding="utf-8"))
    data["name"] = "peer-failover-later"
    data["scenario"] = "peer-failover"

    from gatherlink.lab.scenarios import LabScenarioConfig

    plan = plan_lab_scenario(LabScenarioConfig(**data))

    assert any(step.action == "scenario:peer-failover" for step in plan.steps)
    assert plan.supported is False


def test_lab_plan_cli_prints_not_implemented_steps() -> None:
    result = CliRunner().invoke(app, ["lab", "plan", str(LAB_CONFIGS / "local-dual-path.json")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["scenario"] == "local-dual-path"
    assert payload["supported"] is False
    assert any(step["status"] == "not_implemented" for step in payload["steps"])


def test_lab_status_cli_reports_stopped_service(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GATHERLINK_SERVICE_REGISTRY", str(tmp_path / "services"))
    result = CliRunner().invoke(app, ["lab", "status", str(LAB_CONFIGS / "local-dual-path.json")])

    assert result.exit_code == 0
    assert "lab service: stopped" in result.output
    assert "lab sink service: stopped" in result.output
    assert "lab path: path-a" in result.output


def test_lab_up_starts_forwarder_and_sink_services(monkeypatch, tmp_path: Path) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import ServiceStartResult

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    scenario.runtime_dir = str(tmp_path / "lab-runtime")
    monkeypatch.setattr(lab_cli, "load_lab_scenario_file", lambda path: scenario)
    monkeypatch.setattr(lab_cli, "prepare_lab_runtime", lambda scenario: [])
    monkeypatch.setattr(
        lab_cli,
        "start_lab_service",
        lambda path, scenario: ServiceStartResult(
            name="lab.local-dual-path",
            pid=101,
            user="markus",
            pid_file=tmp_path / "forwarder.pid",
            log_file=tmp_path / "service.log",
            status="started",
        ),
    )
    monkeypatch.setattr(
        lab_cli,
        "start_lab_sink_service",
        lambda path, scenario: ServiceStartResult(
            name="lab.local-dual-path.sink",
            pid=102,
            user="markus",
            pid_file=tmp_path / "sink.pid",
            log_file=tmp_path / "sink.log",
            status="started",
        ),
    )

    result = CliRunner().invoke(app, ["lab", "up", str(LAB_CONFIGS / "local-dual-path.json")])

    assert result.exit_code == 0
    assert "lab service: started name=lab.local-dual-path" in result.output
    assert "lab sink service: started name=lab.local-dual-path.sink" in result.output


def test_top_level_status_uses_default_lab_config() -> None:
    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0
    assert "local-dual-path" in result.output


def test_lab_send_cli_reports_sent_packets(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli

    calls = []

    def fake_send(scenario, *, payload, count, interval_seconds):
        calls.append((scenario.name, payload, count, interval_seconds))
        from gatherlink.lab.runtime import UdpSendResult

        return UdpSendResult(target=scenario.traffic.listen, packets=count, bytes=42)

    monkeypatch.setattr(lab_cli, "send_udp_packets", fake_send)
    result = CliRunner().invoke(
        app,
        ["lab", "send", str(LAB_CONFIGS / "local-dual-path.json"), "--count", "2", "--payload", "hello"],
    )

    assert result.exit_code == 0
    assert "packets=2" in result.output
    assert calls == [("local-dual-path", "hello", 2, 0.05)]


def test_lab_smoke_cli_fails_when_packets_are_missing(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import UdpReceiveResult

    monkeypatch.setattr(
        lab_cli,
        "run_udp_smoke_test",
        lambda scenario, *, payload, count, timeout_seconds: UdpReceiveResult(
            listen=scenario.traffic.target,
            packets=1,
            bytes=5,
            payloads=["one"],
        ),
    )
    result = CliRunner().invoke(app, ["lab", "smoke", str(LAB_CONFIGS / "local-dual-path.json"), "--count", "2"])

    assert result.exit_code == 1
    assert "expected=2 received=1" in result.output


def test_send_udp_packets_sends_to_configured_listener() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    host, port = receiver.getsockname()
    scenario.traffic.listen = f"{host}:{port}"
    received = []

    def receive_one() -> None:
        payload, _ = receiver.recvfrom(65535)
        received.append(payload.decode("utf-8"))

    thread = Thread(target=receive_one)
    thread.start()
    result = send_udp_packets(scenario, payload="probe", count=1, interval_seconds=0)
    thread.join(timeout=1)
    receiver.close()

    assert result.packets == 1
    assert received == ["probe"]


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, check: bool = True):
        self.commands.append(command)

        class Result:
            returncode = 1
            stdout = "fake interface output"
            stderr = ""

        return Result()


def test_prepare_lab_runtime_uses_root_tools_only_for_network_setup(tmp_path: Path) -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    scenario.runtime_dir = str(tmp_path / "lab-runtime")
    runner = RecordingRunner()

    results = prepare_lab_runtime(scenario, runner=runner)

    assert [result.status for result in results] == ["created", "created"]
    assert all(result.shape_actions == ["tc=netem"] for result in results)
    assert (tmp_path / "lab-runtime" / "scenario.json").exists()
    assert all(command[0] == "sudo" and command[1] in {"ip", "tc"} for command in runner.commands)
    assert any("netns" in command for command in runner.commands)
    assert any(command[:2] == ["sudo", "tc"] and "rate" in command for command in runner.commands)


def test_inspect_lab_interfaces_reads_namespaced_interfaces() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    runner = RecordingRunner()

    outputs = inspect_lab_interfaces(scenario, runner=runner)

    assert len(outputs) == 4
    assert outputs[0].startswith("# path-a:")
    assert all(command[:3] == ["sudo", "ip", "-n"] and "addr" in command for command in runner.commands)


def test_cleanup_lab_runtime_deletes_unique_namespaces() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    runner = RecordingRunner()

    results = cleanup_lab_runtime(scenario, runner=runner)

    assert [result.namespace for result in results] == [
        "glab-local-dual-path-client",
        "glab-local-dual-path-server",
    ]
    assert all(result.action == "delete_namespace" for result in results)
    assert all(result.status == "absent_or_already_removed" for result in results)
    assert runner.commands == [
        ["sudo", "ip", "netns", "del", "glab-local-dual-path-client"],
        ["sudo", "ip", "netns", "del", "glab-local-dual-path-server"],
    ]


def test_lab_cleanup_cli_stops_service_and_removes_namespaces(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import LabCleanupResult, ServiceStatus

    calls = []

    def fake_stop(scenario):
        calls.append(("stop", scenario.name))
        return ServiceStatus(
            running=False, pid=None, pid_file=Path("/tmp/service.pid"), log_file=Path("/tmp/service.log")
        )

    def fake_cleanup(scenario):
        calls.append(("cleanup", scenario.name))
        return [LabCleanupResult(namespace="glab-local-dual-path-client", status="removed")]

    monkeypatch.setattr(lab_cli, "stop_lab_service", fake_stop)
    monkeypatch.setattr(lab_cli, "cleanup_lab_runtime", fake_cleanup)

    result = CliRunner().invoke(app, ["lab", "cleanup", str(LAB_CONFIGS / "local-dual-path.json")])

    assert result.exit_code == 0
    assert calls == [("stop", "local-dual-path"), ("cleanup", "local-dual-path")]
    assert "lab service: stopped" in result.output
    assert "namespace=glab-local-dual-path-client status=removed" in result.output


def test_read_service_status_uses_runtime_pid_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GATHERLINK_SERVICE_REGISTRY", str(tmp_path / "services"))
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    scenario.runtime_dir = str(tmp_path / "lab-runtime")
    status = read_service_status(scenario)

    assert status.running is False
    assert status.pid is None
    assert status.pid_file == tmp_path / "lab-runtime" / "service.pid"


def test_apply_lab_shape_sets_mtu_state_and_netem() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    runner = RecordingRunner()

    result = apply_lab_shape(
        scenario,
        "path-a",
        LabShapeConfig(rate="10mbit", delay="50ms", loss="2%", mtu=1200, state="down"),
        runner=runner,
    )

    assert result.actions == ["mtu=1200", "state=down", "tc=netem"]
    assert any(command[:3] == ["sudo", "ip", "-n"] and "mtu" in command for command in runner.commands)
    assert any(command[:3] == ["sudo", "tc", "-n"] and "netem" in command for command in runner.commands)
    assert any("rate" in command and "10mbit" in command for command in runner.commands)


def test_apply_named_profile_and_clear_shape_use_one_shot_system_commands() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    runner = RecordingRunner()

    profile_results = apply_lab_profile(scenario, "path-a-down", runner=runner)
    clear_result = clear_lab_shape(scenario, "path-a", runner=runner)

    assert profile_results[0].actions == ["state=down"]
    assert clear_result.actions == ["clear_qdisc", "state=up"]
    assert any(command[:2] == ["sudo", "tc"] and "del" in command for command in runner.commands)


def test_standalone_shape_config_can_apply_local_and_remote_sides() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    profile = load_lab_shape_profile_file(SHAPING_CONFIGS / "remote-loss-local-clean.json")
    runner = RecordingRunner()

    results = apply_lab_shape_profile(scenario, profile, runner=runner)

    assert [result.side for result in results] == ["local", "remote", "local", "remote"]
    assert any(command[:2] == ["sudo", "tc"] and "del" in command for command in runner.commands)
    assert any(command[:2] == ["sudo", "tc"] and "netem" in command for command in runner.commands)


def test_clear_all_shape_config_clears_both_paths() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    profile = load_lab_shape_profile_file(SHAPING_CONFIGS / "clear-all.json")
    runner = RecordingRunner()

    results = apply_lab_shape_profile(scenario, profile, runner=runner)

    assert [result.name for result in results] == ["path-a", "path-b"]
    assert all(result.actions == ["clear_qdisc", "state=up"] for result in results)


def test_lab_apply_shape_config_cli_loads_sample_config(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli

    calls = []

    def fake_apply(scenario, profile):
        calls.append((scenario.name, profile.name))
        return []

    monkeypatch.setattr(lab_cli, "apply_lab_shape_profile", fake_apply)
    result = CliRunner().invoke(
        app,
        [
            "lab",
            "apply-shape-config",
            str(LAB_CONFIGS / "local-dual-path.json"),
            str(SHAPING_CONFIGS / "rate-limit-pair.json"),
        ],
    )

    assert result.exit_code == 0
    assert calls == [("local-dual-path", "rate-limit-pair")]


def test_lab_profiles_cli_lists_predefined_profiles() -> None:
    result = CliRunner().invoke(app, ["lab", "profiles", str(LAB_CONFIGS / "local-dual-path.json")])

    assert result.exit_code == 0
    assert "rate-10mbit" in result.output
    assert "path-a-down" in result.output
