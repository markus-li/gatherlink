from __future__ import annotations

import json
import socket
from pathlib import Path
from threading import Thread

from gatherlink.cli.main import app
from gatherlink.lab import (
    LabShapeConfig,
    apply_lab_network_mode,
    apply_lab_profile,
    apply_lab_shape,
    apply_lab_shape_profile,
    apply_lab_sink_view_rates,
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
    assert scenario.paths[0].default_max_speed == "50mbit"
    assert scenario.reorder_policies[0].max_hold == "150ms"
    assert "rate-10mbit" in scenario.profiles
    assert "normal-saturated" in scenario.network_modes
    assert "forced-drop" in scenario.network_modes
    assert "latency-jitter-skew" in scenario.network_modes
    assert "wander-low" in scenario.network_modes
    assert "wander-mid" in scenario.network_modes
    assert "wander-high" in scenario.network_modes
    assert plan.supported is False
    assert plan.steps[0].status == "supported"
    assert any(step.action == "setup_network_namespaces_and_veths" for step in plan.steps)
    assert any(step.action == "compile_reorder_policy" for step in plan.steps)
    reorder_step = next(step for step in plan.steps if step.action == "compile_reorder_policy")
    assert reorder_step.details["default_minimum_hold"] == "2ms"
    assert any(step.action == "future_feature:receiver-metrics" for step in plan.steps)
    assert any(step.status == "not_implemented" for step in plan.steps)
    assert "security.mode=none" in plan.warnings[0]


def test_local_three_path_lab_config_has_scheduler_stress_modes() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-three-path.json")
    plan = plan_lab_scenario(scenario)

    assert scenario.name == "local-three-path"
    assert scenario.scenario == "local-multi-path"
    assert len(scenario.paths) == 3
    assert [path.name for path in scenario.paths] == ["path-a", "path-b", "path-c"]
    assert scenario.traffic.listen == "127.0.0.1:55280"
    assert "normal-saturated" in scenario.network_modes
    assert "forced-drop" in scenario.network_modes
    assert "latency-jitter-skew" in scenario.network_modes
    assert "loss-on-fast-path" in scenario.network_modes
    assert "wander-low" in scenario.network_modes
    assert "wander-mid" in scenario.network_modes
    assert "wander-high" in scenario.network_modes
    assert plan.steps[2].details["paths"] == ["path-a", "path-b", "path-c"]
    assert plan.supported is False


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
        lambda path, scenario, **_kwargs: ServiceStartResult(
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
        lambda path, scenario, **_kwargs: ServiceStartResult(
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

    def fake_send(
        scenario,
        *,
        payload,
        count,
        interval_seconds,
        duration_seconds=None,
        bandwidth=None,
        payload_size=None,
        use_namespace=False,
    ):
        calls.append(
            (scenario.name, payload, count, interval_seconds, duration_seconds, bandwidth, payload_size, use_namespace)
        )
        from gatherlink.lab.runtime import UdpSendResult

        return UdpSendResult(target=scenario.traffic.listen, packets=count, bytes=42)

    monkeypatch.setattr(lab_cli, "send_udp_packets", fake_send)
    result = CliRunner().invoke(
        app,
        ["lab", "send", str(LAB_CONFIGS / "local-dual-path.json"), "--count", "2", "--payload", "hello"],
    )

    assert result.exit_code == 0
    assert "packets=2" in result.output
    assert calls == [("local-dual-path", "hello", 2, 0.05, None, None, None, True)]


def test_lab_send_cli_accepts_rate_test_options(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli

    calls = []

    def fake_send(
        scenario,
        *,
        payload,
        count,
        interval_seconds,
        duration_seconds=None,
        bandwidth=None,
        payload_size=None,
        use_namespace=False,
    ):
        calls.append((payload, count, interval_seconds, duration_seconds, bandwidth, payload_size, use_namespace))
        from gatherlink.lab.runtime import UdpSendResult

        return UdpSendResult(target=scenario.traffic.listen, packets=10, bytes=12000)

    monkeypatch.setattr(lab_cli, "send_udp_packets", fake_send)
    result = CliRunner().invoke(
        app,
        [
            "lab",
            "send",
            str(LAB_CONFIGS / "local-dual-path.json"),
            "--payload",
            "rate7",
            "--duration",
            "20",
            "--bandwidth",
            "7mbit",
            "--payload-size",
            "1200",
            "--count",
            "1",
            "--interval",
            "0",
        ],
    )

    assert result.exit_code == 0
    assert "bytes=12000" in result.output
    assert calls == [("rate7", 1, 0.0, 20.0, "7mbit", 1200, True)]


def test_lab_send_cli_can_request_sink_originated_traffic(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli

    calls = []

    def fake_send_from_sink(
        scenario,
        *,
        payload,
        count,
        interval_seconds,
        duration_seconds=None,
        bandwidth=None,
        payload_size=None,
    ):
        calls.append((scenario.name, payload, count, interval_seconds, duration_seconds, bandwidth, payload_size))
        from gatherlink.lab.runtime import UdpSendResult

        return UdpSendResult(target="learned-forwarder-sources", packets=count, bytes=84)

    monkeypatch.setattr(lab_cli, "send_udp_packets_from_sink", fake_send_from_sink)
    result = CliRunner().invoke(
        app,
        [
            "lab",
            "send",
            str(LAB_CONFIGS / "local-dual-path.json"),
            "--direction",
            "from-sink",
            "--count",
            "2",
            "--payload",
            "reply",
        ],
    )

    assert result.exit_code == 0
    assert "direction=from-sink" in result.output
    assert calls == [("local-dual-path", "reply", 2, 0.05, None, None, None)]


def test_lab_rust_smoke_cli_reports_production_transport_result(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import RustTransportSmokeResult

    calls = []

    def fake_smoke(scenario, *, count, payload):
        calls.append((scenario.name, count, payload))
        return RustTransportSmokeResult(
            packets=count,
            bytes=123,
            paths=2,
            forwarded_packets=count,
            delivered_packets=count,
            client_listen="127.0.0.1:1",
            remote_target="127.0.0.1:2",
        )

    monkeypatch.setattr(lab_cli, "run_rust_transport_smoke", fake_smoke)
    result = CliRunner().invoke(
        app,
        ["lab", "rust-smoke", str(LAB_CONFIGS / "local-dual-path.json"), "--count", "2", "--payload", "hello"],
    )

    assert result.exit_code == 0
    assert "lab rust smoke: ok packets=2" in result.output
    assert calls == [("local-dual-path", 2, "hello")]


def test_lab_send_cli_can_drive_both_directions(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import UdpSendResult

    calls = []

    def fake_send(
        scenario,
        *,
        payload,
        count,
        interval_seconds,
        duration_seconds=None,
        bandwidth=None,
        payload_size=None,
        use_namespace=False,
    ):
        calls.append(("to-sink", scenario.name, payload, count, use_namespace))
        return UdpSendResult(target=scenario.traffic.listen, packets=count, bytes=40)

    def fake_send_from_sink(
        scenario,
        *,
        payload,
        count,
        interval_seconds,
        duration_seconds=None,
        bandwidth=None,
        payload_size=None,
    ):
        calls.append(("from-sink", scenario.name, payload, count, False))
        return UdpSendResult(target="learned-forwarder-sources", packets=count, bytes=44)

    monkeypatch.setattr(lab_cli, "send_udp_packets", fake_send)
    monkeypatch.setattr(lab_cli, "send_udp_packets_from_sink", fake_send_from_sink)
    result = CliRunner().invoke(
        app,
        [
            "lab",
            "send",
            str(LAB_CONFIGS / "local-dual-path.json"),
            "--direction",
            "both",
            "--count",
            "2",
            "--payload",
            "duplex",
        ],
    )

    assert result.exit_code == 0
    assert "direction=both" in result.output
    assert "packets=4" in result.output
    assert calls == [
        ("to-sink", "local-dual-path", "duplex", 2, True),
        ("from-sink", "local-dual-path", "duplex", 2, False),
    ]


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
        LabShapeConfig(rate="10mbit", delay="50ms", loss="2%", limit=64, mtu=1200, state="down"),
        runner=runner,
    )

    assert result.actions == ["mtu=1200", "state=down", "tc=netem"]
    assert any(command[:3] == ["sudo", "ip", "-n"] and "mtu" in command for command in runner.commands)
    assert any(command[:3] == ["sudo", "tc", "-n"] and "netem" in command for command in runner.commands)
    assert any("rate" in command and "10mbit" in command for command in runner.commands)
    assert any("limit" in command and "64" in command for command in runner.commands)


def test_apply_named_profile_and_clear_shape_use_one_shot_system_commands() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    runner = RecordingRunner()

    profile_results = apply_lab_profile(scenario, "path-a-down", runner=runner)
    clear_result = clear_lab_shape(scenario, "path-a", runner=runner)

    assert profile_results[0].actions == ["state=down"]
    assert clear_result.actions == ["clear_qdisc", "state=up"]
    assert any(command[:2] == ["sudo", "tc"] and "del" in command for command in runner.commands)


def test_cycle_network_modes_cli_applies_modes_in_order(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.netns import ShapeApplyResult

    calls: list[str] = []

    def fake_apply(scenario, mode_name):
        calls.append(mode_name)
        return [
            ShapeApplyResult(
                name="path-a",
                side="both",
                client_namespace="glab-local-dual-path-client",
                server_namespace="glab-local-dual-path-server",
                client_interface="glpath-ac",
                server_interface="glpath-as",
                actions=[f"mode={mode_name}"],
            )
        ]

    monkeypatch.setattr(lab_cli, "apply_lab_network_mode", fake_apply)

    result = CliRunner().invoke(
        app,
        [
            "lab",
            "cycle-network-modes",
            str(LAB_CONFIGS / "local-dual-path.json"),
            "--modes",
            "wander-low,wander-mid",
            "--interval",
            "0",
            "--cycles",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert calls == ["wander-low", "wander-mid", "wander-low", "wander-mid"]
    assert "applying=wander-low" in result.output
    assert "actions=mode=wander-mid" in result.output


def test_standalone_shape_config_can_apply_local_and_remote_sides() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    profile = load_lab_shape_profile_file(SHAPING_CONFIGS / "remote-loss-local-clean.json")
    runner = RecordingRunner()

    results = apply_lab_shape_profile(scenario, profile, runner=runner)

    assert [result.side for result in results] == ["local", "remote", "local", "remote"]
    assert any(command[:2] == ["sudo", "tc"] and "del" in command for command in runner.commands)
    assert any(command[:2] == ["sudo", "tc"] and "netem" in command for command in runner.commands)


def test_sink_view_asymmetric_rates_apply_to_sending_sides() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    runner = RecordingRunner()

    results = apply_lab_sink_view_rates(
        scenario,
        "path-a",
        sink_up_rate="2mbit",
        sink_down_rate="3mbit",
        runner=runner,
    )

    assert [result.side for result in results] == ["local", "remote"]
    assert "glpath-ac" in runner.commands[0]
    assert "3mbit" in runner.commands[0]
    assert "glpath-as" in runner.commands[1]
    assert "2mbit" in runner.commands[1]


def test_sample_sink_view_asymmetric_shape_config_has_expected_sides() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    profile = load_lab_shape_profile_file(SHAPING_CONFIGS / "sink-view-asymmetric-2x3-1x2.json")
    runner = RecordingRunner()

    results = apply_lab_shape_profile(scenario, profile, runner=runner)

    assert [result.name for result in results] == ["path-a", "path-a", "path-b", "path-b"]
    assert [result.side for result in results] == ["local", "remote", "local", "remote"]
    assert "3mbit" in runner.commands[0]
    assert "2mbit" in runner.commands[1]
    assert "2mbit" in runner.commands[2]
    assert "1mbit" in runner.commands[3]


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


def test_lab_shape_sink_view_cli_uses_directional_rates(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli

    calls = []

    def fake_apply(scenario, path_name, *, sink_up_rate, sink_down_rate):
        calls.append((scenario.name, path_name, sink_up_rate, sink_down_rate))
        return []

    monkeypatch.setattr(lab_cli, "apply_lab_sink_view_rates", fake_apply)
    result = CliRunner().invoke(
        app,
        [
            "lab",
            "shape-sink-view",
            str(LAB_CONFIGS / "local-dual-path.json"),
            "path-a",
            "--up",
            "2mbit",
            "--down",
            "3mbit",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("local-dual-path", "path-a", "2mbit", "3mbit")]


def test_lab_profiles_cli_lists_predefined_profiles() -> None:
    result = CliRunner().invoke(app, ["lab", "profiles", str(LAB_CONFIGS / "local-dual-path.json")])

    assert result.exit_code == 0
    assert "rate-10mbit" in result.output
    assert "path-a-down" in result.output


def test_lab_network_modes_apply_named_behavior() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json")
    runner = RecordingRunner()

    results = apply_lab_network_mode(scenario, "forced-drop", runner=runner)

    assert [result.name for result in results] == ["path-a", "path-b"]
    assert all(result.actions == ["tc=netem"] for result in results)
    assert any("limit" in command and "32" in command for command in runner.commands)
    assert any("limit" in command and "16" in command for command in runner.commands)


def test_lab_network_modes_cli_lists_and_applies_named_modes(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli

    list_result = CliRunner().invoke(app, ["lab", "network-modes", str(LAB_CONFIGS / "local-dual-path.json")])
    assert list_result.exit_code == 0
    assert "normal-saturated" in list_result.output
    assert "forced-drop" in list_result.output

    calls = []

    def fake_apply(scenario, mode):
        calls.append((scenario.name, mode))
        return []

    monkeypatch.setattr(lab_cli, "apply_lab_network_mode", fake_apply)
    apply_result = CliRunner().invoke(
        app,
        ["lab", "apply-network-mode", str(LAB_CONFIGS / "local-dual-path.json"), "normal-saturated"],
    )

    assert apply_result.exit_code == 0
    assert calls == [("local-dual-path", "normal-saturated")]


def test_tc_qdisc_stats_are_exposed_as_missed_packets() -> None:
    from gatherlink.lab.runtime import _parse_tc_qdisc_stats, _path_stats_with_qdisc, _qdisc_delta

    qdisc = _parse_tc_qdisc_stats(
        "qdisc netem 8015: root refcnt 33 limit 1000 rate 3Mbit\n"
        " Sent 8742716 bytes 7060 pkt (dropped 254, overlimits 0 requeues 0)\n"
        " backlog 0b 0p requeues 0\n"
    )
    merged = _path_stats_with_qdisc({"path-a": {"packets": 7294, "bytes": 8750424}}, {"path-a": qdisc})

    assert qdisc == {"sent_bytes": 8742716, "sent_packets": 7060, "dropped": 254, "rate_bps": 3_000_000}
    assert _qdisc_delta(
        {"path-a": qdisc},
        {"path-a": {"sent_bytes": 10, "sent_packets": 2, "dropped": 4, "rate_bps": 3_000_000}},
    ) == {"path-a": {"sent_bytes": 8742706, "sent_packets": 7058, "dropped": 250, "rate_bps": 3_000_000}}
    assert merged["path-a"]["missed_packets"] == 254
    assert merged["path-a"]["qdisc_dropped_packets"] == 254


def test_tc_qdisc_stats_parse_configured_rate() -> None:
    from gatherlink.lab.runtime import _parse_tc_qdisc_stats

    qdisc = _parse_tc_qdisc_stats(
        "qdisc netem 8025: root refcnt 33 limit 1000 rate 1500Kbit\n"
        " Sent 11124127 bytes 9304 pkt (dropped 383, overlimits 0 requeues 0)"
    )

    assert qdisc["rate_bps"] == 1_500_000


def test_path_stats_can_include_reverse_reply_counters() -> None:
    from gatherlink.lab.runtime import _path_stats_with_directional, _path_stats_with_rx

    merged = _path_stats_with_rx(
        {"path-a": {"packets": 2, "bytes": 1600}},
        {"path-a": {"packets": 2, "bytes": 1600}},
    )

    assert merged["path-a"]["packets"] == 2
    assert merged["path-a"]["bytes"] == 1600
    assert merged["path-a"]["rx_packets"] == 2
    assert merged["path-a"]["rx_bytes"] == 1600

    sink_view = _path_stats_with_directional(
        {"path-a": {"packets": 3, "bytes": 2400}},
        {"path-a": {"packets": 1, "bytes": 800}},
        primary_direction="rx",
    )

    assert sink_view["path-a"]["rx_packets"] == 3
    assert sink_view["path-a"]["rx_bytes"] == 2400
    assert sink_view["path-a"]["tx_packets"] == 1
    assert sink_view["path-a"]["tx_bytes"] == 800


def test_lab_data_frame_preserves_normal_udp_payload() -> None:
    from gatherlink.protocol import decode_data_frame, encode_data_frame

    wire_payload = encode_data_frame(sequence=42, path_id=2, payload=b"normal-user-udp")
    frame = decode_data_frame(wire_payload)

    assert frame is not None
    assert frame.sequence == 42
    assert frame.path_id == 2
    assert frame.payload == b"normal-user-udp"


def test_lab_control_frame_carries_path_metadata_for_sink_names() -> None:
    from gatherlink.control.metadata import (
        empty_control_metadata,
        record_control_metadata_received,
        record_control_metadata_sent,
        record_control_path_capacity,
        record_control_path_latency,
        record_sink_time,
    )
    from gatherlink.protocol import (
        decode_control_frame,
        encode_control_frame,
        encode_control_payload,
        encode_control_payload_path_metadata,
    )
    from gatherlink.time.offset import InternalClockSyncMessage, SinkTimeMessage

    payload = encode_control_payload(
        {1: "path-a", 2: "path-b"},
        service_metadata={256: "udp-main"},
        service_endpoint_assertions={256: "127.0.0.1:51820"},
        service_disables={257: "sink declined this service"},
        service_scheduler_policies={256: (2, 512)},
        path_capacity_bps={1: (3_000_000, None), 2: (None, 1_500_000)},
        path_latency_us={1: (12_000, 10_000, None, None), 2: (None, None, 14_000, 11_000)},
        path_mtu={1: (1500, 1200, None, None), 2: (1400, 1200, None, None)},
        path_clock_sync=[
            InternalClockSyncMessage(
                exchange_id=9,
                path_id=1,
                mode=2,
                origin_us=100,
                receive_us=120,
                transmit_us=121,
            )
        ],
        sink_time=[
            SinkTimeMessage(
                path_id=1,
                sink_unix_us=1_776_000_000_000_000,
                sink_internal_us=1_000_000,
                ntp_state=1,
            )
        ],
    )
    wire_frame = encode_control_frame(path_id=1, payload=payload)
    frame = decode_control_frame(wire_frame)

    assert frame is not None
    assert frame.path_metadata == {1: "path-a", 2: "path-b"}
    assert frame.service_metadata == {256: "udp-main"}
    assert frame.service_endpoint_assertions == {256: "127.0.0.1:51820"}
    assert frame.service_disables == {257: "sink declined this service"}
    assert frame.service_scheduler_policies == {256: (2, 512)}
    assert frame.path_capacity_bps == {1: (3_000_000, None), 2: (None, 1_500_000)}
    assert frame.path_latency_us == {1: (12_000, 10_000, None, None), 2: (None, None, 14_000, 11_000)}
    assert frame.path_mtu == {1: (1500, 1200, None, None), 2: (1400, 1200, None, None)}
    assert frame.internal_clock_sync == [
        InternalClockSyncMessage(exchange_id=9, path_id=1, mode=2, origin_us=100, receive_us=120, transmit_us=121)
    ]
    assert frame.sink_time == [
        SinkTimeMessage(path_id=1, sink_unix_us=1_776_000_000_000_000, sink_internal_us=1_000_000, ntp_state=1)
    ]
    assert decode_control_frame(encode_control_frame(1, encode_control_payload_path_metadata({1: "a"})))

    metadata = empty_control_metadata()
    record_control_metadata_sent(
        metadata,
        len(wire_frame),
        message_count=14,
        path_metadata=frame.path_metadata,
        path_capacity={
            "path-a": {"tx_bps": 3_000_000, "rx_bps": None, "source": "detected", "updated_at": None},
            "path-b": {"tx_bps": None, "rx_bps": 1_500_000, "source": "detected", "updated_at": None},
        },
        path_latency={
            "path-a": {"tx_current_us": 12_000, "tx_mean_us": 10_000, "source": "reply-rtt-half", "updated_at": None},
            "path-b": {"rx_current_us": 14_000, "rx_mean_us": 11_000, "source": "reply-rtt-half", "updated_at": None},
        },
        path_mtu={
            "path-a": {
                "tx_link_mtu": 1500,
                "tx_frame_mtu": 1200,
                "tx_payload_mtu": 1186,
                "source": "interface",
            }
        },
        internal_clock={"role": "syncing-to-sink", "offset_us": 12, "mean_offset_us": 10, "rtt_us": 4, "samples": 1},
        sink_time=frame.sink_time,
        path_name="path-a",
    )
    record_control_metadata_received(metadata, len(wire_frame), frame, ("10.80.1.1", 51820), path_name="path-b")
    record_control_path_capacity(metadata, frame.path_capacity_bps, {1: "path-a", 2: "path-b"}, {})
    record_control_path_latency(metadata, frame.path_latency_us, {1: "path-a", 2: "path-b"}, {})
    record_sink_time(metadata, frame.sink_time, {1: "path-a"}, received_at_internal_us=1_010_000)

    assert metadata["sent"]["frames"] == 1
    assert metadata["sent"]["messages"] == 14
    assert metadata["sent"]["bytes"] == len(wire_frame)
    assert metadata["received"]["frames"] == 1
    assert metadata["received"]["messages"] == 14
    assert metadata["received"]["bytes"] == len(wire_frame)
    assert metadata["path_control"]["path-a"]["tx"]["frames"] == 1
    assert metadata["path_control"]["path-b"]["rx"]["frames"] == 1
    assert metadata["path_metadata"] == {"1": "path-a", "2": "path-b"}
    assert metadata["path_metadata_count"] == 2
    assert metadata["service_metadata"] == {"256": "udp-main"}
    assert metadata["service_metadata_count"] == 1
    assert metadata["service_endpoint_assertions"] == {"256": "127.0.0.1:51820"}
    assert metadata["service_endpoint_assertion_count"] == 1
    assert metadata["service_disables"] == {"257": "sink declined this service"}
    assert metadata["service_disable_count"] == 1
    assert metadata["service_scheduler_policies"] == {"256": {"fanout": 2, "fanout_below_bytes": 512}}
    assert metadata["service_scheduler_policy_count"] == 1
    assert metadata["path_capacity_count"] == 2
    assert metadata["path_capacity"]["path-a"]["tx_bps"] == 3_000_000
    assert metadata["path_capacity"]["path-a"]["rx_bps"] == 3_000_000
    assert metadata["path_latency_count"] == 2
    assert metadata["path_latency"]["path-a"]["tx_current_us"] == 12_000
    assert metadata["path_latency"]["path-a"]["rx_current_us"] == 12_000
    assert metadata["path_latency"]["path-b"]["tx_current_us"] == 14_000
    assert metadata["path_latency"]["path-b"]["rx_current_us"] == 14_000
    assert metadata["path_mtu_count"] == 2
    assert metadata["path_mtu"]["path-a"]["tx_frame_mtu"] == 1200
    assert metadata["path_mtu"]["path-a"]["rx_frame_mtu"] == 1200
    assert metadata["path_mtu"]["path-b"]["rx_link_mtu"] == 1400
    assert metadata["internal_clock"]["offset_us"] == 12
    assert metadata["sink_time"]["ntp_state"] == "synchronized"
    assert metadata["sink_time"]["path"] == "path-a"
    assert metadata["sink_time"]["sink_sent_unix_us"] == 1_776_000_000_000_000


def test_reserved_service_dispatch_decodes_control_metadata_and_logs_unknown() -> None:
    from gatherlink.control.metadata import empty_control_metadata
    from gatherlink.control.reserved import drain_reserved_service_events
    from gatherlink.protocol import encode_control_payload

    class FakeEvent:
        def __init__(self, service_id: int, payload: bytes) -> None:
            self._service_id = service_id
            self._payload = payload

        def service_id(self) -> int:
            return self._service_id

        def path_id(self) -> int:
            return 1

        def sequence(self) -> int:
            return 9

        def payload(self) -> bytes:
            return self._payload

        def frame_bytes(self) -> int:
            return len(self._payload) + 14

    class FakeDataplane:
        def drain_reserved_service_events(self):
            return [
                FakeEvent(1, encode_control_payload({1: "path-a"})),
                FakeEvent(5, b"future-diagnostics"),
                FakeEvent(300, b"user-traffic-should-not-be-here"),
            ]

    logs: list[str] = []
    metadata = empty_control_metadata()

    handled = drain_reserved_service_events(
        FakeDataplane(),
        metadata,
        path_names_by_id={1: "path-a"},
        local_targets_by_service_id={256: "127.0.0.1:51820"},
        logger=logs.append,
    )

    assert handled == 1
    assert metadata["path_metadata"] == {"1": "path-a"}
    assert metadata["path_control"]["path-a"]["rx"]["frames"] == 1
    assert any("reserved service id 5 has no Python decoder" in log for log in logs)
    assert any("non-reserved service id 300 reached Python reserved dispatcher" in log for log in logs)


def test_reserved_service_dispatch_allows_python_extra_decoder() -> None:
    from gatherlink.control.metadata import empty_control_metadata
    from gatherlink.control.reserved import ReservedServicePayload, drain_reserved_service_events
    from gatherlink.protocol import SERVICE_ID_REMOTE_STATUS

    class FakeEvent:
        def service_id(self) -> int:
            return SERVICE_ID_REMOTE_STATUS

        def path_id(self) -> int:
            return 2

        def sequence(self) -> int:
            return 11

        def payload(self) -> bytes:
            return b"remote-status"

        def frame_bytes(self) -> int:
            return 51

    class FakeDataplane:
        def drain_reserved_service_events(self):
            return [FakeEvent()]

    seen: list[ReservedServicePayload] = []
    handled = drain_reserved_service_events(
        FakeDataplane(),
        empty_control_metadata(),
        path_names_by_id={2: "path-b"},
        local_targets_by_service_id={256: "127.0.0.1:51820"},
        extra_handlers={SERVICE_ID_REMOTE_STATUS: lambda event: seen.append(event) is None},
    )

    assert handled == 1
    assert seen[0].service_id == SERVICE_ID_REMOTE_STATUS
    assert seen[0].payload == b"remote-status"


def test_service_metadata_never_carries_target_endpoint() -> None:
    from gatherlink.protocol import decode_control_payload, encode_control_payload

    payload = encode_control_payload({}, service_metadata={256: "wireguard-main"})
    frame = decode_control_payload(payload)

    assert frame is not None
    assert frame.service_metadata == {256: "wireguard-main"}
    assert b"127.0.0.1" not in payload
    assert b"51820" not in payload


def test_endpoint_assertions_record_mismatch_without_setting_config() -> None:
    from gatherlink.control.metadata import (
        empty_control_metadata,
        record_control_metadata_received,
        verify_service_endpoint_assertions,
    )
    from gatherlink.protocol import decode_control_payload, encode_control_payload

    payload = encode_control_payload({}, service_endpoint_assertions={256: "127.0.0.1:9"})
    frame = decode_control_payload(payload)
    metadata = empty_control_metadata()

    assert frame is not None
    record_control_metadata_received(metadata, len(payload), frame, ("10.80.1.1", 51820))
    mismatches = verify_service_endpoint_assertions(metadata, {256: "127.0.0.1:51820"})

    assert mismatches
    assert metadata["service_endpoint_assertions"] == {"256": "127.0.0.1:9"}
    assert "127.0.0.1:51820" in metadata["service_endpoint_mismatches"]["256"]


def test_service_disable_metadata_is_generic_peer_control() -> None:
    from gatherlink.control.metadata import empty_control_metadata, record_control_metadata_received
    from gatherlink.protocol import decode_control_payload, encode_control_payload

    payload = encode_control_payload({}, service_disables={256: "sink does not want this service"})
    frame = decode_control_payload(payload)
    metadata = empty_control_metadata()

    assert frame is not None
    assert frame.service_disables == {256: "sink does not want this service"}
    record_control_metadata_received(metadata, len(payload), frame, ("10.80.1.1", 51820))

    assert metadata["service_disables"] == {"256": "sink does not want this service"}
    assert metadata["service_disable_count"] == 1


def test_service_monitor_summarizes_control_metadata_separately() -> None:
    from gatherlink.cli.services import (
        _gatherlink_time_context,
        _ntp_context,
        _path_capacity_context,
        _path_control_context,
        _path_mtu_context,
        _render_path_control_rows,
        _render_service_control_rows,
        _status_context,
    )

    status = {
        "listen": "127.0.0.1:51820",
        "control_metadata": {
            "sent": {"frames": 0, "messages": 0, "bytes": 0, "last_at": None},
            "received": {
                "frames": 2,
                "messages": 4,
                "bytes": 126,
                "last_at": "2026-05-17T09:48:11+00:00",
                "last_source": "('10.80.1.1', 51820)",
            },
            "path_control": {
                "path-a": {
                    "tx": {"frames": 2, "messages": 16, "bytes": 256},
                    "rx": {"frames": 1, "messages": 8, "bytes": 128},
                }
            },
            "path_control_count": 1,
            "path_metadata": {"1": "path-a", "2": "path-b"},
            "path_metadata_count": 2,
            "service_metadata": {"256": "udp-main"},
            "service_metadata_count": 1,
            "path_capacity": {
                "path-a": {"tx_bps": 3_000_000, "rx_bps": None},
                "path-b": {"tx_bps": None, "rx_bps": 1_500_000},
            },
            "path_capacity_count": 2,
            "path_latency": {
                "path-a": {"tx_current_us": 2_500, "tx_mean_us": 2_000, "rx_current_us": None, "rx_mean_us": None},
                "path-b": {"tx_current_us": None, "tx_mean_us": None, "rx_current_us": 5_000, "rx_mean_us": 4_000},
            },
            "path_latency_count": 2,
            "path_mtu": {
                "path-a": {"tx_link_mtu": 1500, "tx_frame_mtu": 1200, "tx_payload_mtu": 1186},
                "path-b": {"rx_link_mtu": 1400, "rx_frame_mtu": 1200, "rx_payload_mtu": 1186},
            },
            "path_mtu_count": 2,
            "internal_clock": {
                "role": "syncing-to-sink",
                "offset_us": 2500,
                "mean_offset_us": 2000,
                "rtt_us": 8000,
                "samples": 3,
            },
            "sink_time": {
                "system_unix_us": 1_776_000_001_000_000,
                "gatherlink_unix_us": 1_776_000_000_500_000,
                "sink_sent_unix_us": 1_776_000_000_000_000,
                "received_at": "2026-05-17T09:48:13+00:00",
                "ntp_state": "synchronized",
            },
        },
    }

    context = _status_context(status)
    rows = [
        {
            "service": "lab.local-dual-path",
            "row_type": "service",
            "state": "running",
            "system_time": "09:48:14",
            "gatherlink_time": _gatherlink_time_context(status["control_metadata"]),
            "ntp": _ntp_context(status["control_metadata"]),
            "control_metadata": status["control_metadata"],
        },
        {
            "service": "path:path-a",
            "row_type": "path",
            "parent": "lab.local-dual-path",
            "path": "path-a",
            "control_metadata": status["control_metadata"],
        },
    ]
    service_control = "\n".join(_render_service_control_rows(rows))
    path_control = "\n".join(_render_path_control_rows(rows))

    assert context == "listen=127.0.0.1:51820"
    assert "listen=127.0.0.1:51820" in context
    assert "service time/control" in service_control
    assert "2/126B" in service_control
    assert "paths svc pol err off lat last" in service_control
    assert "path control" in path_control
    assert "path-a" in path_control
    assert _path_capacity_context(status["control_metadata"], "path-a") == "tx=3.0Mb rx=- tl=2.5/2.0ms rl=-/-"
    assert "tx=frm:1200/pay:1186/lnk:1500" in _path_mtu_context(status["control_metadata"], "path-a")
    assert (
        _path_control_context(status["control_metadata"], "path-a")
        == "ctx=2/256B crx=1/128B tx=3.0Mb rx=- tl=2.5/2.0ms rl=-/-"
    )
    assert _path_capacity_context(status["control_metadata"], "path-b") == "tx=- rx=1.5Mb tl=-/- rl=5.0/4.0ms"
    assert _ntp_context(status["control_metadata"]) == "synchronized"
    assert "sent=" in _gatherlink_time_context(status["control_metadata"])


def test_path_capacity_detector_updates_and_caches_directional_estimate(tmp_path: Path) -> None:
    from gatherlink.lab.runtime import _initial_path_capacity_estimates, _save_path_capacity_cache
    from gatherlink.paths.capacity import (
        PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS,
        PATH_CAPACITY_DETECTION_WINDOW_SECONDS,
        PathCapacityDetector,
    )

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json").model_copy(
        update={"runtime_dir": str(tmp_path)}
    )
    detector = PathCapacityDetector(
        path_names=["path-a"],
        direction="tx",
        initial_estimates=_initial_path_capacity_estimates(scenario, ["path-a"], direction="tx"),
    )
    changed = {}
    sample_count = int(PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS / PATH_CAPACITY_DETECTION_WINDOW_SECONDS) + 1
    for sample in range(1, sample_count + 1):
        detector._last_sample_at -= PATH_CAPACITY_DETECTION_WINDOW_SECONDS + 0.1
        sample_changed = detector.observe(
            {"path-a": {"bytes": sample * 1_875_000}},
            {"path-a": {"sent_bytes": sample * 1_875_000, "dropped": sample}},
        )
        changed.update(sample_changed)

    assert changed["path-a"]["tx_bps"] < 50_000_000
    assert changed["path-a"]["rx_bps"] is None
    _save_path_capacity_cache(scenario, detector.snapshot())
    assert (tmp_path / "path-capacity-cache.json").exists()

    cached = PathCapacityDetector(
        path_names=["path-a"],
        direction="tx",
        initial_estimates=_initial_path_capacity_estimates(scenario, ["path-a"], direction="tx"),
    )
    assert cached.snapshot()["path-a"]["source"] == "cache"


def test_path_latency_tracker_reports_current_and_window_mean() -> None:
    from gatherlink.paths.telemetry import PathLatencyTracker

    tracker = PathLatencyTracker(["path-a"])

    tracker.observe("path-a", 2_000)
    changed = tracker.observe("path-a", 4_000)

    assert changed["path-a"]["tx_current_us"] == 4_000
    assert changed["path-a"]["tx_mean_us"] == 3_000
    assert changed["path-a"]["rx_current_us"] == 4_000
    assert tracker.dirty_snapshot()["path-a"]["source"] == "reply-rtt-half"
    tracker.mark_sent()
    assert tracker.dirty_snapshot() == {}


def test_path_capacity_detector_does_not_lower_without_drops(tmp_path: Path) -> None:
    from gatherlink.lab.runtime import _initial_path_capacity_estimates
    from gatherlink.paths.capacity import (
        PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS,
        PATH_CAPACITY_DETECTION_WINDOW_SECONDS,
        PathCapacityDetector,
    )

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path.json").model_copy(
        update={"runtime_dir": str(tmp_path)}
    )
    detector = PathCapacityDetector(
        path_names=["path-a"],
        direction="tx",
        initial_estimates=_initial_path_capacity_estimates(scenario, ["path-a"], direction="tx"),
    )
    sample_count = int(PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS / PATH_CAPACITY_DETECTION_WINDOW_SECONDS) + 1
    changed = {}
    for sample in range(1, sample_count + 1):
        detector._last_sample_at -= PATH_CAPACITY_DETECTION_WINDOW_SECONDS + 0.1
        changed = detector.observe(
            {"path-a": {"bytes": sample * 1_875_000}},
            {"path-a": {"sent_bytes": sample * 1_875_000, "dropped": 0}},
        )

    assert changed == {}


def test_path_mtu_detection_reads_interface_and_clamps_to_link(tmp_path: Path) -> None:
    from gatherlink.paths.mtu import detect_interface_mtu, observe_path_mtu

    iface = tmp_path / "eth-test"
    iface.mkdir()
    (iface / "mtu").write_text("900\n", encoding="utf-8")

    assert detect_interface_mtu("eth-test", sys_class_net=tmp_path) == 900

    observation = observe_path_mtu("missing-test", 1200)
    assert observation.frame_mtu == 1200
    assert observation.payload_mtu == 1186
