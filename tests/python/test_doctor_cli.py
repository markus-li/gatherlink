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
    assert payload["schema_version"] == 1
    assert payload["tool"] == "gatherlink doctor"
    assert payload["ok"] is True
    assert payload["check_count"] == len(payload["checks"])
    assert {check["name"] for check in payload["checks"]} >= {
        "python.runtime",
        "rust.dataplane_binding",
        "package.versions",
        "state.layout",
        "service.registry",
        "release.hygiene",
        "operator.optional_tools",
        "config.validate",
        "diagnostics.jsonl",
    }
    config_check = next(check for check in payload["checks"] if check["name"] == "config.validate")
    assert config_check["details"]["security_mode"] == "none"
    assert config_check["details"]["operator_facts"]["default_gatherlink_udp_port"] == 53820
    diagnostics_check = next(check for check in payload["checks"] if check["name"] == "diagnostics.jsonl")
    assert diagnostics_check["details"]["events"] == 1


def test_doctor_reports_optional_tools_without_failing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    monkeypatch.setattr(doctor_cli.shutil, "which", lambda name: "/usr/bin/wg" if name == "wg" else None)

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--state-dir",
            str(tmp_path / "state"),
            "--service-registry",
            str(tmp_path / "services"),
            "--json",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    tool_check = next(check for check in payload["checks"] if check["name"] == "operator.optional_tools")
    assert tool_check["ok"] is True
    assert tool_check["details"]["tools"]["wg"]["path"] == "/usr/bin/wg"
    assert set(tool_check["details"]["missing"]) == {"traefik", "wg-quick"}


def test_doctor_reports_package_version_mismatches(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "crates" / "protocol").mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text('[project]\nversion = "0.9.2"\n', encoding="utf-8")
    (repo_root / "crates" / "protocol" / "Cargo.toml").write_text(
        '[package]\nversion = "0.9.1"\n',
        encoding="utf-8",
    )

    check = doctor_cli._check_package_versions(repo_root)

    assert check.ok is False
    assert "manifest versions differ" in check.details["problems"][0]


def test_doctor_release_hygiene_checks_tracked_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_tracked_repo_files",
        lambda _repo_root: [
            ".gatherlink/private-state.json",
            "tools/private-key.json",
            "docs/reports/hyperv-acceptance/run.jsonl",
            "docs/security.md",
        ],
    )

    check = doctor_cli._check_release_hygiene(tmp_path)

    assert check.ok is False
    assert any("tracked local state path" in problem for problem in check.details["problems"])
    assert any("secret-like tracked path" in problem for problem in check.details["problems"])
    assert any("generated report appears tracked" in problem for problem in check.details["problems"])


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


def test_doctor_reports_duplicate_and_wireguard_port_warnings(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    config_path = tmp_path / "port-warning.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node": "port-warning",
                "role": "client",
                "peer": "peer",
                "paths": [
                    {"name": "path-a", "interface": "lo", "transport_bind": "127.0.0.1:51820"},
                    {"name": "path-b", "interface": "lo", "transport_bind": "127.0.0.1:51820"},
                ],
                "services": [{"name": "udp-main", "target": "127.0.0.1:51821", "listen": "127.0.0.1:55180"}],
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
    warnings = config_check["details"]["warnings"]
    assert any("duplicate local endpoint" in warning for warning in warnings)
    assert any("commonly belongs to WireGuard" in warning for warning in warnings)


def test_doctor_reports_standard_carrier_supervision_facts(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    config_path = tmp_path / "quic-carrier.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node": "carrier-node",
                "role": "client",
                "peer": "peer",
                "paths": [
                    {
                        "name": "quic-a",
                        "interface": "lo",
                        "carrier": "quic-datagram",
                        "transport_bind": "127.0.0.1:53820",
                        "transport_remote": "127.0.0.1:53821",
                        "carrier_max_datagram_size": 1180,
                        "scheduler": {"mtu": 1200},
                    }
                ],
                "services": [{"name": "udp-main", "target": "127.0.0.1:51820"}],
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
    path_fact = config_check["details"]["operator_facts"]["path_binds"][0]
    assert path_fact["requires_python_carrier_supervision"] is True
    assert path_fact["effective_datagram_mtu"] == 1180
    assert any("Python carrier supervision" in warning for warning in config_check["details"]["warnings"])


def test_doctor_reports_invalid_standard_carrier_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    config_path = tmp_path / "broken-carrier.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node": "carrier-node",
                "role": "client",
                "peer": "peer",
                "paths": [{"name": "h3-a", "interface": "lo", "carrier": "http3-datagram"}],
                "services": [{"name": "udp-main", "target": "127.0.0.1:51820"}],
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
    warnings = config_check["details"]["warnings"]
    assert any("has no transport_bind" in warning for warning in warnings)
    assert any("need transport_remote" in warning for warning in warnings)


def test_doctor_validates_release_artifact_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    release_dir = tmp_path / "dist"
    (release_dir / "python-wheel").mkdir(parents=True)
    (release_dir / "rust-binaries").mkdir()
    (release_dir / "wiki-user-docs").mkdir()
    source = release_dir / "gatherlink-0.9.1-source.tar.gz"
    wheel = release_dir / "python-wheel" / "gatherlink-0.9.1-py3-none-any.whl"
    rust_binary = release_dir / "rust-binaries" / "gatherlink-time-helper"
    wiki_readme = release_dir / "wiki-user-docs" / "README.md"
    for artifact in [source, wheel, rust_binary, wiki_readme]:
        artifact.write_text("artifact\n", encoding="utf-8")
    (release_dir / "SHA256SUMS").write_text(
        "\n".join(
            [
                f"0  {source.name}",
                f"0  {wheel.name}",
                f"0  {rust_binary.name}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--release-artifacts",
            str(release_dir),
            "--state-dir",
            str(tmp_path / "state"),
            "--service-registry",
            str(tmp_path / "services"),
            "--json",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    artifact_check = next(check for check in payload["checks"] if check["name"] == "release.artifacts")
    assert artifact_check["ok"] is True
    assert artifact_check["details"]["rust_binaries"] == ["gatherlink-time-helper"]


def test_doctor_fails_on_incomplete_release_artifact_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_check_rust_binding",
        lambda: doctor_cli.DoctorCheck("rust.dataplane_binding", True, "stub binding ready"),
    )
    release_dir = tmp_path / "dist"
    release_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--release-artifacts",
            str(release_dir),
            "--state-dir",
            str(tmp_path / "state"),
            "--service-registry",
            str(tmp_path / "services"),
            "--json",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 1
    artifact_check = next(check for check in payload["checks"] if check["name"] == "release.artifacts")
    assert artifact_check["ok"] is False
    assert "missing Rust gatherlink-time-helper binary" in artifact_check["details"]["problems"]
