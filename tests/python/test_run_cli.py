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
    assert payload["steps"][1]["action"] == "bind_udp_listener"
