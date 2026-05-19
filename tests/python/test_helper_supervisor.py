from __future__ import annotations

from pathlib import Path

from gatherlink.config import expand_config, validate_config_file
from gatherlink.runtime.helper_supervisor import build_helper_launch_plans

EXAMPLES = Path("configs/examples")


def test_helper_launch_plan_builds_socks5_process_command(tmp_path) -> None:
    runtime = expand_config(validate_config_file(EXAMPLES / "socks5-helper.json"))

    plan = build_helper_launch_plans(runtime, registry_dir=tmp_path / "services")[0]

    assert plan.name == "helper.socks5-client.socks5"
    assert plan.kind == "helper:socks5"
    assert plan.metadata["service"] == "socks5-stream"
    assert "--gatherlink-service" in plan.command
    assert "127.0.0.1:55190" in plan.command
    assert "--allow-host" in plan.command
    assert "--diagnostics-jsonl" in plan.command


def test_helper_launch_plan_builds_tcp_forward_process_command(tmp_path) -> None:
    runtime = expand_config(validate_config_file(EXAMPLES / "tcp-forward-helper.json"))

    plan = build_helper_launch_plans(runtime, registry_dir=tmp_path / "services")[0]

    assert plan.name == "helper.tcp-forward-client.tcp-forward"
    assert plan.kind == "helper:tcp_forward"
    assert plan.metadata["service"] == "tcp-forward-stream"
    assert "--target" in plan.command
    assert "10.0.0.10:80" in plan.command
    assert "127.0.0.1:55191" in plan.command
    assert "--diagnostics-jsonl" in plan.command


def test_helper_launch_plan_builds_dns_process_command_with_diagnostics(tmp_path) -> None:
    runtime = expand_config(validate_config_file(EXAMPLES / "dns-helper.json"))

    plan = build_helper_launch_plans(runtime, registry_dir=tmp_path / "services")[0]

    assert plan.name == "helper.local.dns"
    assert plan.kind == "helper:dns"
    assert "--upstream" in plan.command
    assert "cloudflare=1.1.1.1:53,timeout=1" in plan.command
    assert "--diagnostics-jsonl" in plan.command
    assert plan.diagnostics_jsonl == tmp_path / "services" / "helper.local.dns" / "diagnostics.jsonl"


def test_helper_launch_plan_skips_wireguard_planning_helper(tmp_path) -> None:
    runtime = expand_config(validate_config_file(EXAMPLES / "wireguard-client.json"))

    assert build_helper_launch_plans(runtime, registry_dir=tmp_path / "services") == []
