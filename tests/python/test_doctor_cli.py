from __future__ import annotations

import json
from pathlib import Path

from gatherlink.cli import doctor as doctor_cli
from gatherlink.cli.main import app
from gatherlink.diagnostics.events import DiagnosticEvent
from typer.testing import CliRunner

EXAMPLES = Path("configs/examples")


def test_doctor_validates_config_and_diagnostics_jsonl(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    diagnostics_path = tmp_path / "diagnostics.jsonl"
    diagnostics_path.write_text(DiagnosticEvent.warning("doctor test").model_dump_json() + "\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--config",
            str(EXAMPLES / "minimal-client.json"),
            "--diagnostics-jsonl",
            str(diagnostics_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--service-registry",
            str(tmp_path / "services"),
            "--json",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} >= {
        "python.runtime",
        "rust.dataplane_binding",
        "state.layout",
        "service.registry",
        "config.validate",
        "diagnostics.jsonl",
    }
    config_check = next(check for check in payload["checks"] if check["name"] == "config.validate")
    assert config_check["details"]["security_mode"] == "none"
    diagnostics_check = next(check for check in payload["checks"] if check["name"] == "diagnostics.jsonl")
    assert diagnostics_check["details"]["events"] == 1


def test_doctor_fails_on_invalid_diagnostics_jsonl(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    diagnostics_path = tmp_path / "bad.jsonl"
    diagnostics_path.write_text("{not-json}\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--diagnostics-jsonl",
            str(diagnostics_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--service-registry",
            str(tmp_path / "services"),
            "--json",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["ok"] is False
    diagnostics_check = next(check for check in payload["checks"] if check["name"] == "diagnostics.jsonl")
    assert diagnostics_check["ok"] is False
    assert diagnostics_check["details"]["invalid_rows"][0]["line"] == 1


def test_doctor_fails_on_invalid_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    invalid_config = tmp_path / "invalid.json"
    invalid_config.write_text(
        '{"schema_version": 1, "role": "client", "services": [{"name": "broken"}]}', encoding="utf-8"
    )

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--config",
            str(invalid_config),
            "--state-dir",
            str(tmp_path / "state"),
            "--service-registry",
            str(tmp_path / "services"),
            "--json",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["ok"] is False
    config_check = next(check for check in payload["checks"] if check["name"] == "config.validate")
    assert config_check["ok"] is False
    assert config_check["details"]["path"] == str(invalid_config)
