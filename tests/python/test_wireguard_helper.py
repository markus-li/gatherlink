from __future__ import annotations

import subprocess
from pathlib import Path

from gatherlink.cli.main import app
from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.helpers.wireguard import (
    derive_public_key,
    render_peer_endpoint_snippet,
    wireguard_tool_status,
    wireguard_transport_plans,
)
from gatherlink.helpers.wireguard.config import WireGuardTransportPlan
from gatherlink.helpers.wireguard.keys import generate_private_key
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
