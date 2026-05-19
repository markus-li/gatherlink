from __future__ import annotations

import json
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
