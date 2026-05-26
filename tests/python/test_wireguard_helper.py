from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gatherlink.cli.main import app
from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.helpers.traffic_split import TrafficSplitPlan, execute_traffic_split_commands, render_commands
from gatherlink.helpers.wireguard import (
    WireGuardSetupRequest,
    derive_public_key,
    generate_wireguard_setup,
    render_peer_endpoint_snippet,
    wireguard_tool_status,
    wireguard_transport_plans,
)
from gatherlink.helpers.wireguard.config import WireGuardTransportPlan
from gatherlink.helpers.wireguard.keys import generate_private_key
from gatherlink.helpers.wireguard.setup import default_local_paths, parse_setup_path
from typer.testing import CliRunner

EXAMPLES = Path("configs/examples")


def test_wireguard_transport_plan_uses_gatherlink_service_mapping() -> None:
    runtime = expand_config(validate_config_file(EXAMPLES / "wireguard-client.json"))

    plan = wireguard_transport_plans(runtime)[0]

    assert plan.service == "wireguard-main"
    assert plan.wireguard_target == "127.0.0.1:51820"
    assert plan.gatherlink_listen == "127.0.0.1:55180"


def test_wireguard_peer_endpoint_snippet_points_wireguard_at_gatherlink() -> None:
    plan = WireGuardTransportPlan(
        service="wireguard-main",
        wireguard_target="127.0.0.1:51820",
        gatherlink_listen="127.0.0.1:55180",
    )

    snippet = render_peer_endpoint_snippet(plan, peer_public_key="abc")

    assert "PublicKey = abc" in snippet
    assert "Endpoint = 127.0.0.1:55180" in snippet
    assert "AllowedIPs = <wireguard-owned-routes>" in snippet


def test_wireguard_key_helpers_delegate_to_wg_tool(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("input")))
        return subprocess.CompletedProcess(command, 0, stdout="key\n", stderr="")

    monkeypatch.setattr("gatherlink.helpers.wireguard.keys.subprocess.run", fake_run)

    assert generate_private_key() == "key"
    assert derive_public_key("private") == "key"
    assert calls[0][0] == ["wg", "genkey"]
    assert calls[1][0] == ["wg", "pubkey"]
    assert calls[1][1] == "private\n"


def test_wireguard_tool_status_is_diagnostic_only(monkeypatch) -> None:
    monkeypatch.setattr("gatherlink.helpers.wireguard.manager.shutil.which", lambda name: f"/usr/bin/{name}")

    status = wireguard_tool_status()

    assert status["wg"] == "/usr/bin/wg"
    assert status["wg_quick"] == "/usr/bin/wg-quick"
    assert status["ready_for_key_ops"] is True


def test_wireguard_plan_cli_renders_mapping() -> None:
    result = CliRunner().invoke(app, ["helpers", "wireguard-plan", str(EXAMPLES / "wireguard-client.json")])

    assert result.exit_code == 0
    assert "service: wireguard-main" in result.output
    assert "wireguard local listen: 127.0.0.1:51820" in result.output
    assert "Endpoint = 127.0.0.1:55180" in result.output


def test_wireguard_plan_cli_emits_structured_diagnostics(tmp_path) -> None:
    diagnostics_path = tmp_path / "wireguard.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "wireguard-plan",
            str(EXAMPLES / "wireguard-client.json"),
            "--diagnostics-jsonl",
            str(diagnostics_path),
        ],
    )

    assert result.exit_code == 0
    event = json.loads(diagnostics_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["code"] == "helper.wireguard.plan"
    assert event["helper"] == "wireguard"
    assert event["service"] == "wireguard-main"
    assert event["details"]["plan"]["wireguard_local_listen"] == "127.0.0.1:51820"
    assert "peer-public-key" not in json.dumps(event)


def test_wireguard_dual_profile_expands_to_two_transport_plans() -> None:
    runtime = expand_config(validate_config_file(EXAMPLES / "wireguard-dual-profile-client.json"))

    plans = wireguard_transport_plans(runtime)

    assert [plan.service for plan in plans] == ["wireguard-stable", "wireguard-fast"]
    assert [plan.traffic_class for plan in plans] == ["stable", "fast"]
    assert plans[0].profile == "dual_profile"
    assert plans[0].scheduler_guidance.startswith("flowlet_adaptive")
    assert plans[1].scheduler_guidance.startswith("capacity_aware")


def test_wireguard_plan_cli_renders_dual_profile_guidance() -> None:
    result = CliRunner().invoke(
        app, ["helpers", "wireguard-plan", str(EXAMPLES / "wireguard-dual-profile-client.json")]
    )

    assert result.exit_code == 0
    assert "service: wireguard-stable" in result.output
    assert "profile: dual_profile traffic_class: stable" in result.output
    assert "service: wireguard-fast" in result.output
    assert "profile: dual_profile traffic_class: fast" in result.output
    assert "Endpoint = 127.0.0.1:55180 # stable profile" in result.output
    assert "Endpoint = 127.0.0.1:55181 # fast profile" in result.output


def test_wireguard_setup_generates_valid_split_multipath_configs() -> None:
    setup = generate_wireguard_setup(
        WireGuardSetupRequest(model="split", paths=default_local_paths(3), security="static", local_only=True)
    )

    assert sorted(setup.files) == [
        "README.md",
        "gatherlink-client.json",
        "gatherlink-server.json",
        "traffic-split-plan.sh",
        "wireguard-fast-client.conf",
        "wireguard-fast-server.conf",
        "wireguard-stable-client.conf",
        "wireguard-stable-server.conf",
    ]
    client = json.loads(setup.files["gatherlink-client.json"])
    server = json.loads(setup.files["gatherlink-server.json"])
    assert [path["name"] for path in client["paths"]] == ["path-a", "path-b", "path-c"]
    assert [path["name"] for path in server["paths"]] == ["path-a", "path-b", "path-c"]
    assert client["helpers"]["wireguard"]["mode"] == "dual_profile"
    assert {service["name"] for service in client["services"]} == {"wireguard-stable", "wireguard-fast"}
    assert client["security"]["send_key"] == server["security"]["receive_key"]
    assert client["security"]["receive_key"] == server["security"]["send_key"]


def test_wireguard_setup_cli_writes_valid_local_files(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "wireguard-setup",
            "--non-interactive",
            "--local-only",
            "--path-count",
            "2",
            "--output",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "model: split" in result.output
    assert "paths: 2" in result.output
    assert (tmp_path / "gatherlink-client.json").exists()
    assert validate_config_file(tmp_path / "gatherlink-client.json").node == "node-a"
    assert validate_config_file(tmp_path / "gatherlink-server.json").role == "server"


def test_wireguard_setup_cli_accepts_explicit_paths(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "wireguard-setup",
            "--non-interactive",
            "--model",
            "single",
            "--path",
            "wan1=eth1,client_bind=10.0.1.2:56001,server_bind=10.0.1.1:57001,tx=1000,rx=2000",
            "--output",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (tmp_path / "wireguard-client.conf").exists()
    config = json.loads((tmp_path / "gatherlink-client.json").read_text(encoding="utf-8"))
    assert config["paths"][0]["transport_bind"] == "10.0.1.2:56001"
    assert config["paths"][0]["scheduler"]["tx_capacity_bps"] == 1000


def test_wireguard_setup_cli_prompts_for_localhost_defaults(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        ["helpers", "wireguard-setup", "--force"],
        input=f"\n\n\n1\n{tmp_path}\n",
    )

    assert result.exit_code == 0
    assert "Gatherlink WireGuard setup wizard" in result.output
    assert "paths: 1" in result.output
    assert validate_config_file(tmp_path / "gatherlink-server.json").role == "server"


def test_wireguard_setup_path_parser_derives_remote_endpoints() -> None:
    path = parse_setup_path("wan1=eth1,client_bind=10.0.1.2:56001,server_bind=10.0.1.1:57001,mtu=1380")

    assert path.name == "wan1"
    assert path.interface == "eth1"
    assert path.client_remote == "10.0.1.1:57001"
    assert path.server_remote == "10.0.1.2:56001"
    assert path.mtu == 1380


def test_traffic_split_plan_is_reviewable_and_reversible() -> None:
    plan = TrafficSplitPlan(stable_interface="wg-stable", fast_interface="wg-fast")

    apply_text = render_commands(plan.apply_commands())
    revert_text = render_commands(plan.revert_commands())
    apply_commands = plan.apply_commands()

    assert "nft add table inet gatherlink_split" in apply_text
    assert "meta l4proto udp meta mark set 0x5182" in apply_text
    assert apply_commands[3][-10:-2] == ["meta", "l4proto", "!=", "udp", "meta", "mark", "set", "0x5181"]
    assert "gatherlink dual-wireguard split: udp-fast" in apply_text
    assert "gatherlink dual-wireguard split: stable-default" in apply_text
    assert "ip route replace default dev wg-stable table 51881" in apply_text
    assert "nft delete table inet gatherlink_split" in revert_text


def test_traffic_split_cli_defaults_to_dry_run() -> None:
    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "traffic-split",
            "--stable-interface",
            "wg-stable",
            "--fast-interface",
            "wg-fast",
        ],
    )

    assert result.exit_code == 0
    assert "sudo nft add table inet gatherlink_split" in result.output
    assert "traffic split commands executed" not in result.output


def test_traffic_split_execute_uses_debian_backend_runner() -> None:
    class FakeRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def run(self, command, *, check=True):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    class FakeBackend:
        def __init__(self) -> None:
            self.runner = FakeRunner()

        def command_runner(self):
            return self.runner

    backend = FakeBackend()
    commands = [["sudo", "true"]]

    results = execute_traffic_split_commands(commands, backend=backend)

    assert results[0].returncode == 0
    assert backend.runner.commands == commands
