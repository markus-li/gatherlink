from __future__ import annotations

import json
import socket
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
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
    assert [path.default_max_speed for path in scenario.paths] == ["300mbit", "500mbit", "700mbit"]
    assert scenario.traffic.listen == "127.0.0.1:55280"
    assert "acceptance-300-500-700" in scenario.network_modes
    assert "acceptance-uneven-high" in scenario.network_modes
    assert "realworld-fiber-plus-5g" in scenario.network_modes
    assert "realworld-starlink-plus-5g" in scenario.network_modes
    assert "realworld-starlink-plus-2x5g" in scenario.network_modes
    assert "normal-saturated" in scenario.network_modes
    assert "forced-drop" in scenario.network_modes
    assert "latency-jitter-skew" in scenario.network_modes
    assert "loss-on-fast-path" in scenario.network_modes
    assert "wander-low" in scenario.network_modes
    assert "wander-mid" in scenario.network_modes
    assert "wander-high" in scenario.network_modes
    assert plan.steps[2].details["paths"] == ["path-a", "path-b", "path-c"]
    assert plan.supported is False


def test_local_three_path_acceptance_profiles_encode_realistic_rates_and_pressure() -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-three-path.json")

    hard_limit = scenario.network_modes["acceptance-300-500-700"]
    high_uneven = scenario.network_modes["acceptance-uneven-high"]
    fiber_5g = scenario.network_modes["realworld-fiber-plus-5g"]
    starlink_2x5g = scenario.network_modes["realworld-starlink-plus-2x5g"]

    assert [target.shape.rate for target in hard_limit.targets] == ["300mbit", "500mbit", "700mbit"]
    assert [target.shape.limit for target in hard_limit.targets] == [131072, 131072, 131072]
    assert "1.55gbit" in (hard_limit.description or "")
    assert [target.shape.rate for target in high_uneven.targets] == ["600mbit", "900mbit", "1300mbit"]
    assert "2.9gbit" in (high_uneven.description or "")
    assert [target.shape.rate for target in fiber_5g.targets] == ["800mbit", "160mbit", "85mbit"]
    assert [target.shape.delay for target in fiber_5g.targets] == ["12ms", "45ms", "70ms"]
    assert [target.shape.rate for target in starlink_2x5g.targets] == ["180mbit", "140mbit", "90mbit"]
    assert [target.shape.jitter for target in starlink_2x5g.targets] == ["25ms", "20ms", "30ms"]


def test_lab_runtime_config_uses_scenario_scheduler_mode() -> None:
    from gatherlink.lab.runtime import _lab_runtime_config

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-three-path.json").model_copy(
        update={"scheduler_mode": "capacity_aware"}
    )

    runtime_config = _lab_runtime_config(scenario, role="client")

    assert runtime_config.scheduler.mode == "capacity_aware"


def test_lab_runtime_config_treats_reorder_policy_as_cap_not_clean_path_hold() -> None:
    from gatherlink.lab.runtime import _lab_runtime_config

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-three-path.json")

    runtime_config = _lab_runtime_config(scenario, role="client")

    assert [path.scheduler.reorder_hold_us for path in runtime_config.paths] == [0, 0, 0]


def test_lab_runtime_config_compiles_delay_jitter_to_reorder_hold() -> None:
    from gatherlink.lab.runtime import _lab_runtime_config

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-three-path.json")

    runtime_config = _lab_runtime_config(scenario, role="client")
    fiber_mode = scenario.network_modes["realworld-fiber-plus-5g"]
    patched = scenario.model_copy(
        update={
            "paths": [
                path.model_copy(update={"shape": target.shape})
                for path, target in zip(scenario.paths, fiber_mode.targets, strict=True)
            ]
        }
    )
    runtime_config = _lab_runtime_config(patched, role="client")

    assert [path.scheduler.latency_us for path in runtime_config.paths] == [12_000, 45_000, 70_000]
    assert [path.scheduler.reorder_hold_us for path in runtime_config.paths] == [18_750, 75_000, 118_750]


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


def test_lab_bundle_hyperv_three_node_generates_manifest_configs_and_commands(tmp_path: Path) -> None:
    output = tmp_path / "bundle"

    result = CliRunner().invoke(app, ["lab", "bundle", "hyperv-three-node", "--out", str(output)])

    assert result.exit_code == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "gatherlink.lab.bundle.manifest"
    assert manifest["topology"] == "hyperv-three-node"
    assert manifest["monitor_groups"]["wg-demo"] == ["vm.shared-sink", "remote:source-a", "remote:source-c"]
    assert len(manifest["paths"]) == 6
    assert (output / "configs" / "sink-b.json").exists()
    assert (output / "commands.md").exists()

    preflight = CliRunner().invoke(app, ["lab", "preflight", str(output / "manifest.json")])

    assert preflight.exit_code == 0
    assert "code=bundle.config_valid node=sink-b" in preflight.output
    assert "code=bundle.debian_commands" in preflight.output


def test_lab_bundle_cleanup_uses_manifest_resources_only(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    CliRunner().invoke(app, ["lab", "bundle", "hyperv-three-node", "--out", str(output)])

    result = CliRunner().invoke(app, ["lab", "cleanup", str(output / "manifest.json")])

    assert result.exit_code == 0
    assert "gatherlink services close vm.shared-sink" in result.output
    assert "sudo ip link del wg-gl-a" in result.output
    assert "while read" not in result.output


def test_lab_bundle_cleanup_execute_runs_only_manifest_commands(tmp_path: Path) -> None:
    from subprocess import CompletedProcess

    from gatherlink.lab.bundles import execute_lab_bundle_cleanup

    output = tmp_path / "bundle"
    CliRunner().invoke(app, ["lab", "bundle", "hyperv-three-node", "--out", str(output)])
    calls = []

    def fake_runner(argv):
        calls.append(argv)
        return CompletedProcess(argv, 0, stdout="closed\n", stderr="")

    results = execute_lab_bundle_cleanup(output / "manifest.json", runner=fake_runner)

    assert results
    assert [call[:3] for call in calls[:3]] == [["gatherlink", "services", "close"]] * 3
    assert all(
        call[:3] == ["gatherlink", "services", "close"] or call[:4] == ["sudo", "ip", "link", "del"] for call in calls
    )


def test_lab_bundle_cleanup_execute_blocks_unscoped_commands(tmp_path: Path) -> None:
    from gatherlink.lab.bundles import execute_lab_bundle_cleanup

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "kind": "gatherlink.lab.bundle.manifest",
                "schema_version": 1,
                "topology": "hyperv-three-node",
                "nodes": [],
                "paths": [],
                "resources": [{"kind": "temporary-file", "node": "node", "name": "bad", "command": "rm -rf /tmp/bad"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not allowed"):
        execute_lab_bundle_cleanup(manifest)


def test_lab_shared_sink_smoke_cli_reports_two_sources(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import SharedSinkSmokeResult

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path-encrypted.json")
    monkeypatch.setattr(lab_cli, "load_lab_scenario_file", lambda path: scenario)
    monkeypatch.setattr(
        lab_cli,
        "run_shared_sink_transport_smoke",
        lambda scenario, **_kwargs: SharedSinkSmokeResult(
            source_count=2,
            packets=10,
            bytes=260,
            paths=2,
            sink_transport="127.0.0.1:56002",
            remote_target="127.0.0.1:51820",
        ),
    )

    result = CliRunner().invoke(
        app,
        ["lab", "shared-sink-smoke", str(LAB_CONFIGS / "local-dual-path-encrypted.json"), "--count", "5"],
    )

    assert result.exit_code == 0
    assert "sources=2" in result.output
    assert "sink_transport=127.0.0.1:56002" in result.output


def test_lab_carrier_smoke_cli_reports_standard_adapter(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import StandardCarrierSmokeResult

    monkeypatch.setattr(
        lab_cli,
        "run_standard_carrier_smoke",
        lambda carrier, **_kwargs: StandardCarrierSmokeResult(
            carrier=carrier,
            packets=6,
            bytes=180,
            client_udp="127.0.0.1:41000",
            server_udp="127.0.0.1:42000",
            carrier_endpoint="127.0.0.1:43000",
        ),
    )

    result = CliRunner().invoke(app, ["lab", "carrier-smoke", "quic-datagram", "--count", "3"])

    assert result.exit_code == 0
    assert "carrier=quic-datagram" in result.output
    assert "packets=6" in result.output


def test_lab_carrier_proxy_smoke_cli_reports_traefik_adapter(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import StandardCarrierProxySmokeResult

    monkeypatch.setattr(
        lab_cli,
        "run_standard_carrier_proxy_smoke",
        lambda carrier, **_kwargs: StandardCarrierProxySmokeResult(
            carrier=carrier,
            packets=6,
            bytes=180,
            client_udp="127.0.0.1:41000",
            server_udp="127.0.0.1:42000",
            carrier_endpoint="127.0.0.1:43000",
            proxy="traefik",
            proxy_endpoint="127.0.0.1:43000",
            upstream_endpoint="127.0.0.1:44000",
        ),
    )

    result = CliRunner().invoke(app, ["lab", "carrier-proxy-smoke", "quic-datagram", "--count", "3"])

    assert result.exit_code == 0
    assert "carrier=quic-datagram" in result.output
    assert "proxy=traefik" in result.output
    assert "packets=6" in result.output


def test_lab_carrier_compare_cli_reports_all_rows(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import CarrierComparisonReport, CarrierComparisonRow

    monkeypatch.setattr(
        lab_cli,
        "run_standard_carrier_comparison",
        lambda **_kwargs: CarrierComparisonReport(
            count=3,
            rows=(
                CarrierComparisonRow("udp", "direct", True, packets=6, bytes=180, detail="udp baseline"),
                CarrierComparisonRow("quic-datagram", "direct", True, packets=6, bytes=180, detail="quic direct"),
                CarrierComparisonRow("http3-datagram", "direct", True, packets=6, bytes=180, detail="h3 direct"),
            ),
        ),
    )

    result = CliRunner().invoke(app, ["lab", "carrier-compare", "--count", "3"])

    assert result.exit_code == 0
    assert "lab carrier compare: ok rows=3 count=3" in result.output
    assert "carrier=udp path=direct status=ok" in result.output
    assert "carrier=quic-datagram path=direct status=ok" in result.output
    assert "carrier=http3-datagram path=direct status=ok" in result.output


def test_lab_carrier_compare_cli_reports_json(monkeypatch) -> None:
    from gatherlink.cli import lab as lab_cli
    from gatherlink.lab.runtime import CarrierComparisonReport, CarrierComparisonRow

    monkeypatch.setattr(
        lab_cli,
        "run_standard_carrier_comparison",
        lambda **_kwargs: CarrierComparisonReport(
            count=2,
            rows=(CarrierComparisonRow("udp", "direct", True, packets=4, bytes=120, detail="udp baseline"),),
        ),
    )

    result = CliRunner().invoke(app, ["lab", "carrier-compare", "--count", "2", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["rows"][0]["carrier"] == "udp"
    assert payload["rows"][0]["path"] == "direct"


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
            user="gatherlink-user",
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
            user="gatherlink-user",
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


def test_lab_rust_transport_smoke_supports_ipv6_loopback() -> None:
    from gatherlink.lab.runtime import run_rust_transport_smoke

    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-dual-path-ipv6.json")

    result = run_rust_transport_smoke(scenario, count=2, payload="ipv6-rust-smoke")

    assert result.packets == 2
    assert result.paths == 2
    assert result.forwarded_packets == 2
    assert result.delivered_packets == 2
    assert result.client_listen.startswith("[::1]:")
    assert result.remote_target.startswith("[::1]:")


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


def test_lab_dataplane_step_uses_summary_hot_path(monkeypatch) -> None:
    from gatherlink.lab.runtime import _step_rust_lab_dataplane

    class FakeDataplane:
        def __init__(self) -> None:
            self.summary_calls: list[tuple[list[str], int, int]] = []

        def run_available_summary(self, service_names, batch_size, max_cycles):
            self.summary_calls.append((service_names, batch_size, max_cycles))
            return (3, 300, 2, 200)

        def forward_available_for_service_nonblocking(self, service_name, batch_size):  # pragma: no cover
            raise AssertionError("lab hot path should use aggregate Rust summary calls")

    monkeypatch.delenv("GATHERLINK_LAB_PACKET_LOG", raising=False)
    dataplane = FakeDataplane()

    assert _step_rust_lab_dataplane(dataplane) is True
    assert dataplane.summary_calls == [(["udp-main"], 512, 8)]


def test_lab_dataplane_step_allows_bounded_burst_cycle_experiments(monkeypatch) -> None:
    from gatherlink.lab.runtime import _step_rust_lab_dataplane

    class FakeDataplane:
        def __init__(self) -> None:
            self.summary_calls: list[tuple[list[str], int, int]] = []

        def run_available_summary(self, service_names, batch_size, max_cycles):
            self.summary_calls.append((service_names, batch_size, max_cycles))
            return (1, 100, 0, 0)

    monkeypatch.delenv("GATHERLINK_LAB_PACKET_LOG", raising=False)
    monkeypatch.setenv("GATHERLINK_LAB_DATAPLANE_BURST_CYCLES", "2")
    dataplane = FakeDataplane()

    assert _step_rust_lab_dataplane(dataplane) is True
    assert dataplane.summary_calls == [(["udp-main"], 512, 2)]


def test_lab_dataplane_step_ignores_unbounded_burst_cycle_experiments(monkeypatch) -> None:
    from gatherlink.lab.runtime import _step_rust_lab_dataplane

    class FakeDataplane:
        def __init__(self) -> None:
            self.summary_calls: list[tuple[list[str], int, int]] = []

        def run_available_summary(self, service_names, batch_size, max_cycles):
            self.summary_calls.append((service_names, batch_size, max_cycles))
            return (1, 100, 0, 0)

    monkeypatch.delenv("GATHERLINK_LAB_PACKET_LOG", raising=False)
    monkeypatch.setenv("GATHERLINK_LAB_DATAPLANE_BURST_CYCLES", "999")
    dataplane = FakeDataplane()

    assert _step_rust_lab_dataplane(dataplane) is True
    assert dataplane.summary_calls == [(["udp-main"], 512, 8)]


def test_lab_dataplane_step_keeps_packet_log_opt_in(monkeypatch, capsys) -> None:
    from gatherlink.lab.runtime import _step_rust_lab_dataplane

    class FakeOutcome:
        def __init__(self, payload_len: int, path_id: int, target: str) -> None:
            self._payload_len = payload_len
            self._path_id = path_id
            self._target = target

        def payload_len(self) -> int:
            return self._payload_len

        def path_id(self) -> int:
            return self._path_id

        def target(self) -> str:
            return self._target

    class FakeDataplane:
        def run_available_summary(self, service_names, batch_size, max_cycles):  # pragma: no cover
            raise AssertionError("packet-log mode should use outcome-producing calls")

        def forward_available_for_service_nonblocking(self, service_name, batch_size):
            return [FakeOutcome(10, 1, "127.0.0.1:1")]

        def receive_available_from_paths(self, batch_size):
            return [FakeOutcome(20, 2, "127.0.0.1:2")]

    monkeypatch.setenv("GATHERLINK_LAB_PACKET_LOG", "1")

    assert _step_rust_lab_dataplane(FakeDataplane()) is True
    output = capsys.readouterr().out
    assert "lab service: rust forwarded bytes=10 path=1 target=127.0.0.1:1" in output
    assert "lab service: rust delivered bytes=20 path=2 target=127.0.0.1:2" in output


def test_lab_app_sink_updates_counters_without_default_packet_logs(monkeypatch, capsys) -> None:
    from gatherlink.lab.runtime import _drain_app_sink_socket, _LabAppSinkState

    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sink.bind(("127.0.0.1", 0))
        sink.setblocking(False)
        sender.sendto(b"hello", sink.getsockname())
        state = _LabAppSinkState()
        monkeypatch.delenv("GATHERLINK_LAB_PACKET_LOG", raising=False)

        assert _drain_app_sink_socket(sink, state) is True

        assert state.packets == 1
        assert state.bytes == 5
        assert state.last_payload == "hello"
        assert "lab sink app: received" not in capsys.readouterr().out
    finally:
        sink.close()
        sender.close()


def test_lab_app_sink_packet_logs_are_explicit(monkeypatch, capsys) -> None:
    from gatherlink.lab.runtime import _drain_app_sink_socket, _LabAppSinkState

    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sink.bind(("127.0.0.1", 0))
        sink.setblocking(False)
        sender.sendto(b"hello", sink.getsockname())
        state = _LabAppSinkState()
        monkeypatch.setenv("GATHERLINK_LAB_PACKET_LOG", "1")

        assert _drain_app_sink_socket(sink, state) is True

        assert "lab sink app: received packet=1" in capsys.readouterr().out
    finally:
        sink.close()
        sender.close()


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


def test_lab_network_modes_persist_capacity_starting_hints(tmp_path: Path) -> None:
    scenario = load_lab_scenario_file(LAB_CONFIGS / "local-three-path.json").model_copy(
        update={"runtime_dir": str(tmp_path)}
    )
    runner = RecordingRunner()

    apply_lab_network_mode(scenario, "realworld-starlink-plus-2x5g", runner=runner)

    payload = json.loads((tmp_path / "path-capacity-cache.json").read_text(encoding="utf-8"))
    assert payload["paths"]["path-a"]["tx_bps"] == 180_000_000
    assert payload["paths"]["path-a"]["rx_bps"] == 180_000_000
    assert payload["paths"]["path-b"]["tx_bps"] == 140_000_000
    assert payload["paths"]["path-c"]["rx_bps"] == 90_000_000
    assert payload["paths"]["path-c"]["source"] == "lab-network-mode"


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


def test_scheduler_interval_path_stats_keep_queue_depth_but_delta_counters() -> None:
    from gatherlink.lab.runtime import _scheduler_interval_path_stats

    interval = _scheduler_interval_path_stats(
        {
            "path-a": {
                "packets": 1500,
                "qdisc_dropped_packets": 250,
                "missed_packets": 300,
                "queue_depth_packets": 64,
                "queue_oldest_age_us": 500_000,
            }
        },
        {
            "path-a": {
                "packets": 1000,
                "qdisc_dropped_packets": 200,
                "missed_packets": 225,
                "queue_depth_packets": 128,
                "queue_oldest_age_us": 900_000,
            }
        },
    )

    assert interval["path-a"]["packets"] == 500
    assert interval["path-a"]["qdisc_dropped_packets"] == 50
    assert interval["path-a"]["missed_packets"] == 75
    assert interval["path-a"]["queue_depth_packets"] == 64
    assert interval["path-a"]["queue_oldest_age_us"] == 500_000


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


def test_lab_path_pressure_from_stats_builds_scheduler_feedback() -> None:
    from gatherlink.scheduling.metrics import path_pressure_from_path_stats

    pressure = path_pressure_from_path_stats(
        {
            "path-a": {
                "packets": 100,
                "missed_packets": 2,
                "qdisc_dropped_packets": 3,
                "queue_depth_packets": 4,
                "queue_depth_bytes": 8192,
                "queue_oldest_age_us": 2500,
                "send_failed_packets": 1,
                "packets_needing_reorder": 6,
                "reorder_depth_packets": 5,
                "security_drop_packets": 7,
                "scheduler_in_flight_packets": 8,
                "scheduler_in_flight_bytes": 9000,
                "scheduler_predicted_delivery_us": 12000,
                "reorder_buffer_packets": 9,
                "reorder_buffer_oldest_age_us": 3000,
                "socket_receive_buffer_bytes": 65536,
                "socket_send_buffer_bytes": 32768,
                "socket_drain_quantum": 16,
            }
        }
    )

    assert pressure["path-a"]["loss_ppm"] == 47_619
    assert pressure["path-a"]["queue_depth_packets"] == 4
    assert pressure["path-a"]["receive_gaps"] == 6
    assert pressure["path-a"]["reorder_depth_packets"] == 5
    assert pressure["path-a"]["local_drops"] == 10
    assert pressure["path-a"]["scheduler_in_flight_packets"] == 8
    assert pressure["path-a"]["scheduler_predicted_delivery_us"] == 12000
    assert pressure["path-a"]["reorder_buffer_packets"] == 9
    assert pressure["path-a"]["socket_drain_quantum"] == 16


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
        record_control_path_latency_quality,
        record_sink_time,
    )
    from gatherlink.protocol import (
        decode_control_frame,
        encode_control_frame,
        encode_control_payload,
        encode_control_payload_path_metadata,
    )
    from gatherlink.scheduling.metrics import scheduler_metrics_from_control_metadata
    from gatherlink.time.offset import InternalClockSyncMessage, SinkTimeMessage

    payload = encode_control_payload(
        {1: "path-a", 2: "path-b"},
        service_metadata={256: "udp-main"},
        service_endpoint_assertions={256: "127.0.0.1:51820"},
        service_disables={257: "sink declined this service"},
        service_scheduler_policies={256: (2, 512, 50_000, 500_000, 64)},
        path_capacity_bps={1: (3_000_000, None), 2: (None, 1_500_000)},
        path_latency_us={1: (12_000, 10_000, None, None), 2: (None, None, 14_000, 11_000)},
        path_latency_quality={1: ("data-traffic-one-way", "good"), 2: ("clock-synced-one-way", "warming")},
        path_mtu={1: (1500, 1200, None, None), 2: (1400, 1200, None, None)},
        path_pressure={1: (1200, 3, 4096, 2500, 1, 2, 3, 4, 5, 8192, 12_000, 6, 2500)},
        scheduler_status=("coordinated_adaptive", "flowlet_adaptive", "adaptive"),
        data_transmit_samples=[(1, 2048, 16, 5_000_000)],
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
    assert frame.service_scheduler_policies == {256: (2, 512, 50_000, 500_000, 64, 0)}
    assert frame.path_capacity_bps == {1: (3_000_000, None), 2: (None, 1_500_000)}
    assert frame.path_latency_us == {1: (12_000, 10_000, None, None), 2: (None, None, 14_000, 11_000)}
    assert frame.path_latency_quality == {1: ("data-traffic-one-way", "good"), 2: ("clock-synced-one-way", "warming")}
    assert frame.path_mtu == {1: (1500, 1200, None, None), 2: (1400, 1200, None, None)}
    assert frame.path_pressure == {1: (1200, 3, 4096, 2500, 1, 2, 3, 4, 5, 8192, 12_000, 6, 2500)}
    assert frame.scheduler_status == ("coordinated_adaptive", "flowlet_adaptive", "adaptive")
    assert frame.data_transmit_samples == [(1, 2048, 16, 5_000_000)]
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
        message_count=19,
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
        path_pressure={
            "path-a": {
                "loss_ppm": 1200,
                "queue_depth_packets": 3,
                "queue_depth_bytes": 4096,
                "queue_oldest_age_us": 2500,
                "send_failures": 1,
                "receive_gaps": 2,
                "reorder_depth_packets": 3,
                "local_drops": 4,
                "scheduler_in_flight_packets": 5,
                "scheduler_in_flight_bytes": 8192,
                "scheduler_predicted_delivery_us": 12_000,
                "reorder_buffer_packets": 6,
                "reorder_buffer_oldest_age_us": 2500,
                "source": "local-path-stats",
            }
        },
        internal_clock={"role": "syncing-to-sink", "offset_us": 12, "mean_offset_us": 10, "rtt_us": 4, "samples": 1},
        sink_time=frame.sink_time,
        path_name="path-a",
    )
    record_control_metadata_received(metadata, len(wire_frame), frame, ("10.80.1.1", 51820), path_name="path-b")
    record_control_path_capacity(metadata, frame.path_capacity_bps, {1: "path-a", 2: "path-b"}, {})
    record_control_path_latency(metadata, frame.path_latency_us, {1: "path-a", 2: "path-b"}, {})
    record_control_path_latency_quality(metadata, frame.path_latency_quality, {1: "path-a", 2: "path-b"}, {})
    record_sink_time(metadata, frame.sink_time, {1: "path-a"}, received_at_internal_us=1_010_000)

    assert metadata["sent"]["frames"] == 1
    assert metadata["sent"]["messages"] == 19
    assert metadata["sent"]["bytes"] == len(wire_frame)
    assert metadata["received"]["frames"] == 1
    assert metadata["received"]["messages"] == 19
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
    assert metadata["service_scheduler_policies"] == {
        "256": {
            "fanout": 2,
            "fanout_below_bytes": 512,
            "flowlet_idle_us": 50_000,
            "flowlet_max_hold_us": 500_000,
            "path_run_datagrams": 64,
            "path_policy": "inherit",
        }
    }
    assert metadata["service_scheduler_policy_count"] == 1
    assert metadata["peer_scheduler"]["configured_mode"] == "coordinated_adaptive"
    assert metadata["peer_scheduler"]["effective_mode"] == "flowlet_adaptive"
    assert metadata["peer_scheduler"]["rust_mode"] == "adaptive"
    assert metadata["peer_scheduler_count"] == 1
    assert metadata["path_capacity_count"] == 2
    assert metadata["path_capacity"]["path-a"]["tx_bps"] == 3_000_000
    assert metadata["path_capacity"]["path-a"]["rx_bps"] == 3_000_000
    assert metadata["path_latency_count"] == 2
    assert metadata["path_latency"]["path-a"]["tx_current_us"] == 12_000
    assert metadata["path_latency"]["path-a"]["rx_current_us"] == 12_000
    assert metadata["path_latency"]["path-a"]["source"] == "data-traffic-one-way"
    assert metadata["path_latency"]["path-a"]["confidence"] == "good"
    assert metadata["path_latency"]["path-b"]["tx_current_us"] == 14_000
    assert metadata["path_latency"]["path-b"]["rx_current_us"] == 14_000
    assert metadata["path_latency"]["path-b"]["source"] == "clock-synced-one-way"
    assert metadata["path_latency"]["path-b"]["confidence"] == "warming"
    scheduler_snapshot = scheduler_metrics_from_control_metadata(metadata, default_path_ids={"path-a": 1, "path-b": 2})
    assert scheduler_snapshot.paths["path-a"].has_trusted_real_data_latency
    assert not scheduler_snapshot.paths["path-b"].has_trusted_real_data_latency
    assert metadata["path_mtu_count"] == 2
    assert metadata["path_mtu"]["path-a"]["tx_frame_mtu"] == 1200
    assert metadata["path_mtu"]["path-a"]["rx_frame_mtu"] == 1200
    assert metadata["path_mtu"]["path-b"]["rx_link_mtu"] == 1400
    assert metadata["path_pressure_count"] == 1
    assert metadata["path_pressure"]["path-a"]["loss_ppm"] == 1200
    assert metadata["path_pressure"]["path-a"]["queue_depth_packets"] == 3
    assert metadata["path_pressure"]["path-a"]["receive_gaps"] == 2
    assert metadata["path_pressure"]["path-a"]["scheduler_in_flight_packets"] == 5
    assert metadata["path_pressure"]["path-a"]["scheduler_predicted_delivery_us"] == 12_000
    assert metadata["path_pressure"]["path-a"]["reorder_buffer_packets"] == 6
    assert metadata["internal_clock"]["offset_us"] == 12
    assert metadata["sink_time"]["ntp_state"] == "synchronized"
    assert metadata["sink_time"]["path"] == "path-a"
    assert metadata["sink_time"]["sink_sent_unix_us"] == 1_776_000_000_000_000


def test_control_metadata_dispatch_handles_clock_sync_requests_and_responses(monkeypatch) -> None:
    import gatherlink.time.offset as offset_module
    from gatherlink.control.metadata import empty_control_metadata
    from gatherlink.control.reserved import ReservedServicePayload, handle_control_metadata_event
    from gatherlink.protocol import SERVICE_ID_CONTROL_METADATA, encode_control_payload
    from gatherlink.time.offset import InternalClockSyncClient, InternalClockSyncMessage

    request_payload = encode_control_payload(
        {},
        path_clock_sync=[
            InternalClockSyncMessage(exchange_id=7, path_id=1, mode=1, origin_us=1_000),
        ],
    )
    responses = []
    assert handle_control_metadata_event(
        ReservedServicePayload(SERVICE_ID_CONTROL_METADATA, 1, 1, request_payload, len(request_payload)),
        empty_control_metadata(),
        path_names_by_id={1: "path-a"},
        local_targets_by_service_id={},
        clock_sync_responses=responses,
    )
    assert responses and responses[0].mode == 2
    assert responses[0].exchange_id == 7

    response_payload = encode_control_payload(
        {},
        path_clock_sync=[
            InternalClockSyncMessage(
                exchange_id=7,
                path_id=1,
                mode=2,
                origin_us=1_000,
                receive_us=2_000,
                transmit_us=2_500,
            ),
        ],
    )
    client = InternalClockSyncClient(["path-a"])
    client._pending[7] = ("path-a", 1_000)
    monkeypatch.setattr(offset_module, "internal_monotonic_us", lambda: 5_000)
    metadata = empty_control_metadata()

    assert handle_control_metadata_event(
        ReservedServicePayload(SERVICE_ID_CONTROL_METADATA, 1, 2, response_payload, len(response_payload)),
        metadata,
        path_names_by_id={1: "path-a"},
        local_targets_by_service_id={},
        clock_sync_client=client,
    )
    assert metadata["internal_clock"]["samples"] == 1
    assert metadata["internal_clock"]["path_summaries"]["path-a"]["confidence"] == "warming"
    assert metadata["path_latency"] == {}


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
                    "tx": {"frames": 2, "messages": 16, "bytes": 256, "last_gap_us": 2500},
                    "rx": {"frames": 1, "messages": 8, "bytes": 128},
                }
            },
            "path_control_count": 1,
            "path_metadata": {"1": "path-a", "2": "path-b"},
            "path_metadata_count": 2,
            "service_metadata": {"256": "udp-main"},
            "service_metadata_count": 1,
            "local_scheduler": {
                "configured_mode": "coordinated_adaptive",
                "effective_mode": "flowlet_adaptive",
                "rust_mode": "adaptive",
            },
            "peer_scheduler": {
                "configured_mode": "capacity_aware",
                "effective_mode": "capacity_aware",
                "rust_mode": "weighted_round_robin",
            },
            "peer_scheduler_count": 1,
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
            "service": "path path-a",
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
    assert "lsch" in service_control
    assert "psch" in service_control
    assert "coordinated_adaptiv..." in service_control
    assert "capacity_aware" in service_control
    assert "paths svc pol err off lat last" in service_control
    assert "path control" in path_control
    assert "path-a" in path_control
    assert _path_capacity_context(status["control_metadata"], "path-a") == "tx=3.0Mb rx=- tl=2.5/2.0ms rl=-/-"
    assert "tx=frm:1200/pay:1186/lnk:1500" in _path_mtu_context(status["control_metadata"], "path-a")
    assert (
        _path_control_context(status["control_metadata"], "path-a")
        == "ctx=2/256B/g=2.5ms crx=1/128B tx=3.0Mb rx=- tl=2.5/2.0ms rl=-/-"
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
    assert changed["path-a"]["tx_jitter_us"] == 1_000
    assert changed["path-a"]["tx_p95_us"] == 4_000
    assert changed["path-a"]["rx_current_us"] == 4_000
    assert changed["path-a"]["rx_jitter_us"] == 1_000
    assert changed["path-a"]["rx_p95_us"] == 4_000
    assert tracker.dirty_snapshot()["path-a"]["source"] == "reply-rtt-half"
    assert tracker.dirty_snapshot()["path-a"]["confidence"] == "coarse"
    tracker.mark_sent()
    assert tracker.dirty_snapshot() == {}


def test_path_latency_tracker_records_directional_samples_and_rejects_impossible_values() -> None:
    from gatherlink.paths.telemetry import PathLatencyTracker

    tracker = PathLatencyTracker(["path-a"])

    changed = tracker.observe_directional(
        "path-a",
        tx_one_way_us=3_000,
        rx_one_way_us=5_000,
        source="clock-synced-one-way",
        confidence="good",
        rtt_us=8_500,
        clock_error_us=500,
    )

    assert changed["path-a"]["tx_current_us"] == 3_000
    assert changed["path-a"]["rx_current_us"] == 5_000
    assert changed["path-a"]["source"] == "clock-synced-one-way"
    assert changed["path-a"]["confidence"] == "good"
    assert changed["path-a"]["clock_error_us"] == 500

    rejected = tracker.observe_directional(
        "path-a",
        tx_one_way_us=10_000,
        rx_one_way_us=10_000,
        rtt_us=8_000,
        clock_error_us=100,
    )

    assert rejected["path-a"]["source"] == "rejected"
    assert rejected["path-a"]["confidence"] == "rejected"
    assert rejected["path-a"]["rejection_reason"] == "impossible-rtt"
    assert tracker.dirty_snapshot()["path-a"]["tx_current_us"] == 3_000

    one_way_rejected = tracker.observe_directional(
        "path-a",
        tx_one_way_us=12_000,
        source="data-traffic-one-way",
        confidence="good",
        rtt_us=4_000,
        clock_error_us=500,
    )

    assert one_way_rejected["path-a"]["source"] == "rejected"
    assert one_way_rejected["path-a"]["rejection_reason"] == "impossible-rtt"
    assert tracker.dirty_snapshot()["path-a"]["tx_current_us"] == 3_000

    unreasonable = tracker.observe_directional(
        "path-a",
        tx_one_way_us=9_000_000_000,
        source="data-traffic-one-way",
        confidence="warming",
    )

    assert unreasonable["path-a"]["source"] == "rejected"
    assert unreasonable["path-a"]["rejection_reason"] == "unreasonable-sample"
    assert tracker.dirty_snapshot()["path-a"]["tx_current_us"] == 3_000


def test_data_traffic_latency_tracker_matches_real_transmit_samples() -> None:
    from gatherlink.paths.telemetry import DataTrafficLatencyTracker, PathLatencyTracker

    latency_tracker = PathLatencyTracker(["path-a"])
    data_tracker = DataTrafficLatencyTracker({1: "path-a"})
    control_samples = data_tracker.observe_local_samples(
        {
            "tx": [{"path_id": 1, "sequence": 2048, "packet_count": 1, "observed_at_us": 1_000_000}],
            "rx": [{"path_id": 1, "sequence": 4096, "packet_count": 1, "observed_at_us": 2_001_500}],
        },
        local_clock_offset_us=500,
    )

    assert control_samples == [(1, 2048, 1, 1_000_500)]

    changed = data_tracker.observe_peer_transmit_samples(
        [(1, 4096, 1, 2_000_000)],
        peer_scope=None,
        local_clock_offset_us=500,
        latency_tracker=latency_tracker,
        rtt_us=4_000,
        clock_error_us=1_000,
    )

    assert changed["path-a"]["source"] == "data-traffic-one-way"
    assert changed["path-a"]["tx_current_us"] == 2_000

    delayed_data_tracker = DataTrafficLatencyTracker({1: "path-a"})
    delayed_data_tracker.observe_peer_transmit_samples(
        [(1, 8192, 1, 5_000_000)],
        peer_scope=None,
        local_clock_offset_us=500,
        latency_tracker=latency_tracker,
        rtt_us=4_000,
        clock_error_us=1_000,
    )
    delayed_data_tracker.observe_local_samples(
        {"rx": [{"path_id": 1, "sequence": 8192, "packet_count": 1, "observed_at_us": 5_001_500}]},
        local_clock_offset_us=500,
    )
    delayed = delayed_data_tracker.promote_pending_peer_transmit_samples(
        local_clock_offset_us=500,
        latency_tracker=latency_tracker,
        rtt_us=4_000,
        clock_error_us=1_000,
    )
    assert delayed["path-a"]["tx_current_us"] == 2_000


def test_lab_control_state_drains_real_data_timing_samples() -> None:
    from gatherlink.lab.runtime import _drain_lab_data_timing_samples, _LabControlState
    from gatherlink.paths.telemetry import DataTrafficLatencyTracker, PathLatencyTracker

    class FakeTimingDataplane:
        def drain_data_timing_samples(self):
            return {
                "tx": [{"path_id": 1, "sequence": 2048, "packet_count": 1, "observed_at_us": 1_000_000}],
                "rx": [{"path_id": 1, "sequence": 4096, "packet_count": 1, "observed_at_us": 2_001_500}],
            }

    control_state = _LabControlState(
        control_metadata={},
        service_disables={},
        path_capacity={},
        path_mtu={},
        path_pressure={},
        path_latency_tracker=PathLatencyTracker(["path-a"]),
        data_traffic_latency_tracker=DataTrafficLatencyTracker({1: "path-a"}),
    )

    _drain_lab_data_timing_samples(FakeTimingDataplane(), control_state)

    assert control_state.pending_data_transmit_samples == [(1, 2048, 1, 1_000_000)]


def test_path_pinned_clock_sync_announcement_uses_exact_path() -> None:
    from gatherlink.control.announcements import announce_path_pinned_clock_sync
    from gatherlink.control.metadata import empty_control_metadata
    from gatherlink.time.offset import InternalClockSyncMessage

    runtime_config = SimpleNamespace(
        paths=[
            SimpleNamespace(name="path-a", scheduler=SimpleNamespace(path_id=1)),
            SimpleNamespace(name="path-b", scheduler=SimpleNamespace(path_id=2)),
        ]
    )

    class FakeDataplane:
        def __init__(self) -> None:
            self.sent: list[tuple[int, int, bytes]] = []

        def transmit_service_payload_on_path(self, service_id: int, path_id: int, payload: bytes) -> int:
            self.sent.append((service_id, path_id, payload))
            return 1

    dataplane = FakeDataplane()
    metadata = empty_control_metadata()

    result = announce_path_pinned_clock_sync(
        dataplane,
        runtime_config,
        metadata,
        [
            InternalClockSyncMessage(exchange_id=11, path_id=1, mode=1, origin_us=1000),
            InternalClockSyncMessage(exchange_id=12, path_id=2, mode=1, origin_us=2000),
        ],
    )

    assert result.sent_paths == 2
    assert [path_id for _service_id, path_id, _payload in dataplane.sent] == [1, 2]
    assert metadata["path_control"]["path-a"]["tx"]["frames"] == 1
    assert metadata["path_control"]["path-b"]["tx"]["frames"] == 1


def test_clock_sync_observations_feed_path_latency_tracker() -> None:
    from gatherlink.control.metadata import empty_control_metadata
    from gatherlink.control.reserved import _merge_clock_sync_latency
    from gatherlink.paths.telemetry import PathLatencyTracker

    metadata = empty_control_metadata()
    tracker = PathLatencyTracker(["path-a"])

    _merge_clock_sync_latency(
        metadata,
        tracker,
        {
            "path_latency_observations": [
                {
                    "path": "path-a",
                    "tx_one_way_us": 1_500,
                    "rx_one_way_us": 1_700,
                    "source": "clock-synced-one-way",
                    "confidence": "good",
                    "rtt_us": 3_500,
                    "clock_error_us": 200,
                }
            ]
        },
    )

    assert metadata["path_latency"]["path-a"]["source"] == "clock-synced-one-way"
    assert metadata["path_latency"]["path-a"]["confidence"] == "good"
    assert metadata["path_latency"]["path-a"]["tx_current_us"] == 1_500

    _merge_clock_sync_latency(
        metadata,
        tracker,
        {"path_latency_rejections": [{"path": "path-a", "reason": "offset-outlier"}]},
    )

    assert metadata["path_latency"]["path-a"]["source"] == "rejected"
    assert metadata["path_latency"]["path-a"]["rejection_reason"] == "offset-outlier"
    assert metadata["path_latency"]["path-a"]["tx_current_us"] == 1_500


def test_service_monitor_path_latency_context_names_source_and_rejections() -> None:
    from gatherlink.cli.services import _path_latency_context

    assert (
        _path_latency_context(
            {
                "path_latency": {
                    "path-a": {
                        "tx_current_us": 1_500,
                        "tx_mean_us": 2_000,
                        "rx_current_us": 1_700,
                        "rx_mean_us": 2_200,
                        "source": "clock-synced-one-way",
                        "confidence": "good",
                    }
                }
            },
            "path-a",
        )
        == "src=clock conf=good tl=1.5/2.0ms rl=1.7/2.2ms"
    )
    assert (
        _path_latency_context(
            {
                "path_latency": {
                    "path-a": {
                        "tx_current_us": 1_500,
                        "tx_mean_us": 2_000,
                        "source": "data-traffic-one-way",
                        "confidence": "good",
                    }
                }
            },
            "path-a",
        )
        == "src=data conf=good tl=1.5/2.0ms rl=-/-"
    )
    assert (
        _path_latency_context(
            {
                "path_latency": {
                    "path-a": {
                        "tx_current_us": 1_500,
                        "tx_mean_us": 2_000,
                        "source": "rejected",
                        "rejection_reason": "offset-outlier",
                    }
                }
            },
            "path-a",
        )
        == "src=reject reason=offset-outlier tl=1.5/2.0ms rl=-/-"
    )


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


def test_path_capacity_detector_uses_directional_runtime_bytes() -> None:
    from gatherlink.paths.capacity import (
        PATH_CAPACITY_DETECTION_WINDOW_SECONDS,
        PathCapacityDetector,
    )

    detector = PathCapacityDetector(
        path_names=["path-a"],
        direction="rx",
        initial_estimates={"path-a": {"rx_bps": 1_000_000, "source": "config"}},
    )

    detector._last_sample_at -= PATH_CAPACITY_DETECTION_WINDOW_SECONDS + 0.1
    changed = detector.observe(
        {"path-a": {"tx_bytes": 10, "rx_bytes": 16 * 1024 * 1024}},
        {},
    )

    assert changed == {}
    assert detector.snapshot()["path-a"]["rx_bps"] == 1_000_000
    assert detector._sustained["path-a"]["direction"] == "increase"


def test_path_mtu_detection_reads_interface_and_clamps_to_link(tmp_path: Path) -> None:
    from gatherlink.paths.mtu import detect_interface_mtu, observe_path_mtu

    iface = tmp_path / "eth-test"
    iface.mkdir()
    (iface / "mtu").write_text("900\n", encoding="utf-8")

    assert detect_interface_mtu("eth-test", sys_class_net=tmp_path) == 900

    observation = observe_path_mtu("missing-test", 1200)
    assert observation.frame_mtu == 1200
    assert observation.payload_mtu == 1186


def test_path_mtu_detection_honors_carrier_max_datagram_size() -> None:
    from gatherlink.paths.mtu import observe_path_mtu

    observation = observe_path_mtu("missing-test", 1200, carrier_max_datagram_size=1000)

    assert observation.status == "clamped"
    assert observation.frame_mtu == 1000
    assert observation.payload_mtu == 986
    assert observation.export_dict()["carrier_max_datagram_size"] == 1000


def test_path_mtu_downgrade_uses_only_explicit_triggers() -> None:
    from gatherlink.paths.mtu import recommend_path_mtu_downgrade

    assert (
        recommend_path_mtu_downgrade(
            "path-a",
            current_frame_mtu=1200,
            path_status={"missed_packets": 100},
        )
        is None
    )

    carrier_recommendation = recommend_path_mtu_downgrade(
        "path-a",
        current_frame_mtu=1200,
        path_status={},
        carrier_max_datagram_size=1000,
    )
    assert carrier_recommendation is not None
    assert carrier_recommendation.changed
    assert carrier_recommendation.recommended_frame_mtu == 1000

    symptom_recommendation = recommend_path_mtu_downgrade(
        "path-b",
        current_frame_mtu=1200,
        path_status={"packet_too_large_packets": 1},
    )
    assert symptom_recommendation is not None
    assert symptom_recommendation.trigger == "too_large_or_fragmentation_failed"
    assert symptom_recommendation.recommended_frame_mtu < 1200


def test_service_monitor_renders_queue_pressure_column() -> None:
    from gatherlink.cli.services import _queue_pressure_context, _render_aggregate_rows

    rows = [
        {
            "row_key": "svc::path-a",
            "service": "  path path-a",
            "state": "path",
            "tx_packets": 1,
            "tx_bytes": 128,
            "tx_speed_bytes_per_second": 0,
            "rx_packets": 0,
            "rx_bytes": 0,
            "rx_speed_bytes_per_second": 0,
            "missed": 0,
            "expected_duplicate_packets": 0,
            "duplicate_packets": 0,
            "send_failed_packets": 0,
            "fanout_send_failed_packets": 0,
            "queue_pressure": "p=3/b=4.0KiB/a=2.5ms",
            "scheduler_health": "deg/s0/w1:queue_pressure",
            "reordered": 0,
            "reorder_needed": 0,
            "row_type": "path",
            "parent": "svc",
            "path": "path-a",
            "control_metadata": {},
            "system_time": "-",
            "gatherlink_time": "-",
            "ntp": "-",
            "extra": "parent=svc",
        }
    ]

    rendered = _render_aggregate_rows(
        rows,
        refreshed_at="12:00:00",
        human_units=True,
        speed_bits=True,
        decimal_units=False,
        interactive=False,
    )

    assert (
        _queue_pressure_context({"queue_depth_packets": 3, "queue_depth_bytes": 4096, "queue_oldest_age_us": 2500})
        == "p=3/b=4.0KiB/a=2.5ms"
    )
    assert "queue" in rendered.splitlines()[2]
    assert "sch" in rendered.splitlines()[2]
    assert "deg/s0/w1:queue_pressure" in rendered
    assert "p=3/b=4.0KiB/a=2.5ms" in rendered
    assert "queue    scheduler-visible queue pressure" in rendered
    assert "sch      Python scheduler health summary" in rendered


def test_service_monitor_renders_python_owned_service_policy_table() -> None:
    from gatherlink.cli.services import _render_aggregate_rows

    rows = [
        {
            "row_key": "core",
            "service": "core",
            "state": "running",
            "tx_packets": 1,
            "tx_bytes": 128,
            "tx_speed_bytes_per_second": 0,
            "rx_packets": 1,
            "rx_bytes": 128,
            "rx_speed_bytes_per_second": 0,
            "missed": 0,
            "expected_duplicate_packets": 0,
            "duplicate_packets": 0,
            "send_failed_packets": 0,
            "fanout_send_failed_packets": 0,
            "queue_pressure": "not_reported",
            "scheduler_health": "not_reported",
            "reordered": 0,
            "reorder_needed": 0,
            "row_type": "service",
            "parent": "",
            "path": "",
            "control_metadata": {},
            "service_config": [
                {
                    "name": "wireguard-stable",
                    "priority": "high",
                    "traffic_class": "tcp_ordered",
                    "listen": "127.0.0.1:55180",
                    "target": "127.0.0.1:51820",
                },
                {
                    "name": "wireguard-fast",
                    "priority": "bulk",
                    "traffic_class": "udp_bulk",
                    "listen": "127.0.0.1:55181",
                    "target": "127.0.0.1:51821",
                },
            ],
            "service_budget": {
                "active": True,
                "reason": "bulk service dominated high-priority traffic for 3 samples",
                "packet_budget_overrides": {"wireguard-fast": 128},
                "byte_budget_overrides": {"wireguard-fast": 98304},
                "samples": [
                    {
                        "service": "wireguard-stable",
                        "priority": "high",
                        "traffic_class": "tcp_ordered",
                        "tx_packets_per_second": 500,
                        "tx_bytes_per_second": 250000,
                    },
                    {
                        "service": "wireguard-fast",
                        "priority": "bulk",
                        "traffic_class": "udp_bulk",
                        "tx_packets_per_second": 4000,
                        "tx_bytes_per_second": 3000000,
                    },
                ],
            },
            "auth_crypto_messages": [
                {
                    "type": "rekey_initiation",
                    "peer": "peer-a",
                    "sender_node_id": "peer-a-node",
                    "peer_node_id": "local-node",
                    "topology_generation": 7,
                    "current_receiver_index": 42,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2026-01-01T00:00:30+00:00",
                    "reason": None,
                    "has_noise": True,
                    "path_id": 1,
                    "sequence": 99,
                }
            ],
            "system_time": "-",
            "gatherlink_time": "-",
            "ntp": "-",
            "extra": "target=127.0.0.1:51820",
        }
    ]

    rendered = _render_aggregate_rows(
        rows,
        refreshed_at="12:00:00",
        human_units=True,
        speed_bits=True,
        decimal_units=False,
        interactive=False,
    )

    assert "service policy" in rendered
    assert "runner service          class       prio" in rendered
    assert "core   wireguard-stable tcp_ordered high" in rendered
    assert "core   wireguard-fast   udp_bulk    bulk" in rendered
    assert "service budget" in rendered
    assert "core   yes    wireguard-fast=128" in rendered
    assert "wireguard-stable:250000B/s/500pps" in rendered
    assert "auth/rekey control" in rendered
    assert "core   rekey_initiation peer-a" in rendered
    assert "auth     operator-safe reserved auth/crypto facts" in rendered
    assert "class    Python-owned service traffic class" in rendered
    assert "budget  Python-owned service budget/QoS status" in rendered
