from __future__ import annotations

from gatherlink.cli.main import app
from gatherlink.lab.helper_smoke import run_all_helper_smokes
from typer.testing import CliRunner


def test_helper_smoke_lab_runs_all_active_helpers() -> None:
    results = run_all_helper_smokes()

    assert {result.helper for result in results} == {
        "time",
        "dns",
        "dns-negative",
        "gatherlink-udp-stream",
        "tcp-forward",
        "socks5",
        "wireguard",
        "relay-fabric",
        "relay-fabric-negative",
        "transport-boundary",
    }
    assert all(result.ok for result in results)


def test_helper_smoke_cli_reports_each_helper() -> None:
    result = CliRunner().invoke(app, ["lab", "helpers-smoke"])

    assert result.exit_code == 0
    assert "dns: ok" in result.output
    assert "dns-negative: ok" in result.output
    assert "gatherlink-udp-stream: ok" in result.output
    assert "socks5: ok" in result.output
    assert "relay-fabric: ok" in result.output
    assert "transport-boundary: ok" in result.output
