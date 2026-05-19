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
    assert result.exit_code == 0, result.output
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


def test_doctor_reports_multi_session_return_warning(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    key = "ERERERERERERERERERERERERERERERERERERERERERE="
    config_path = tmp_path / "ambiguous-shared-sink.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node": "shared-sink",
                "role": "server",
                "services": [{"name": "udp-main", "target": "127.0.0.1:51820", "return_mode": "fixed"}],
                "security": {
                    "mode": "static",
                    "sessions": [
                        {
                            "local_receiver_index": 201,
                            "remote_receiver_index": 101,
                            "send_key": key,
                            "receive_key": key,
                            "services": ["udp-main"],
                        },
                        {
                            "local_receiver_index": 202,
                            "remote_receiver_index": 102,
                            "send_key": key,
                            "receive_key": key,
                            "services": ["udp-main"],
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--config",
            str(config_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--service-registry",
            str(tmp_path / "services"),
            "--json",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    config_check = next(check for check in payload["checks"] if check["name"] == "config.validate")
    assert config_check["ok"] is True
    assert any("multiple authenticated sessions" in warning for warning in config_check["details"]["warnings"])
