from __future__ import annotations

import json

from gatherlink.cli.main import app
from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.loader import load_config_dict
from typer.testing import CliRunner


def test_loader_reports_invalid_json(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")

    try:
        load_config_dict(path)
    except ConfigValidationError as exc:
        assert exc.path == path
        assert exc.details[0].location == ("line 1", "column 2")
    else:
        raise AssertionError("expected invalid JSON error")


def test_validate_json_error_output_for_invalid_config(tmp_path) -> None:
    path = tmp_path / "bad-config.json"
    path.write_text(json.dumps({"node": "client", "services": [{"name": "svc", "target": "127.0.0.1:1"}]}))

    result = CliRunner().invoke(app, ["config", "validate", "--json", str(path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["valid"] is False
    assert payload["path"] == str(path)
    assert payload["source_format"] == "minimal-client"
    assert payload["errors"]


def test_validate_text_error_output_for_missing_file() -> None:
    result = CliRunner().invoke(app, ["config", "validate", "missing.json"])

    assert result.exit_code == 1
    assert "invalid:" in result.stderr
    assert "file" in result.stderr
