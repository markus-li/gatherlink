from __future__ import annotations

import json
from pathlib import Path

from gatherlink.cli.main import app
from gatherlink.config import detect_config_format, load_config_dict, supported_schema_versions, validate_config_file
from typer.testing import CliRunner

EXAMPLES = Path("configs/examples")


def test_all_example_configs_validate() -> None:
    for path in sorted(EXAMPLES.glob("*.json")):
        config = validate_config_file(path)
        assert config.schema_version == 1
        assert config.helpers is not None


def test_supported_schema_versions_are_registered() -> None:
    assert supported_schema_versions() == (1,)


def test_example_format_detection() -> None:
    assert detect_config_format(load_config_dict(EXAMPLES / "minimal-client.json")) == "minimal-client"
    assert detect_config_format(load_config_dict(EXAMPLES / "minimal-server.json")) == "minimal-server"
    assert detect_config_format(load_config_dict(EXAMPLES / "wireguard-client.json")) == "wireguard-client"
    assert detect_config_format(load_config_dict(EXAMPLES / "wireguard-server.json")) == "wireguard-server"
    assert detect_config_format(load_config_dict(EXAMPLES / "dns-helper.json")) == "dns-helper"
    assert detect_config_format(load_config_dict(EXAMPLES / "minimal-ipv6-client.json")) == "minimal-client"


def test_schema_version_rejects_future_versions() -> None:
    config_path = EXAMPLES / "minimal-client.json"
    data = load_config_dict(config_path)
    data["schema_version"] = 999

    try:
        from gatherlink.config.validation import validate_config_dict

        validate_config_dict(data)
    except ValueError as exc:
        assert any("schema_version" in detail.message for detail in exc.details)
    else:
        raise AssertionError("expected unsupported schema_version")


def test_schema_version_is_required() -> None:
    config_path = EXAMPLES / "minimal-client.json"
    data = load_config_dict(config_path)
    data.pop("schema_version")

    try:
        from gatherlink.config.validation import validate_config_dict

        validate_config_dict(data)
    except ValueError as exc:
        assert any("schema_version" in ".".join(detail.location) for detail in exc.details)
    else:
        raise AssertionError("expected missing schema_version error")


def test_wireguard_helper_must_reference_existing_service() -> None:
    config_path = EXAMPLES / "wireguard-client.json"
    data = load_config_dict(config_path)
    data["helpers"]["wireguard"]["service"] = "missing"

    try:
        from gatherlink.config.validation import validate_config_dict

        validate_config_dict(data)
    except ValueError as exc:
        assert any("wireguard helper service" in detail.message for detail in exc.details)
    else:
        raise AssertionError("expected invalid wireguard helper service")


def test_config_detect_cli_prints_format() -> None:
    result = CliRunner().invoke(app, ["config", "detect", str(EXAMPLES / "wireguard-client.json")])

    assert result.exit_code == 0
    assert result.output.strip() == "wireguard-client"


def test_config_validate_cli_accepts_example_config() -> None:
    result = CliRunner().invoke(app, ["config", "validate", str(EXAMPLES / "minimal-client.json")])

    assert result.exit_code == 0
    assert "valid:" in result.output
    assert "schema v1" in result.output


def test_config_validate_cli_can_emit_json() -> None:
    result = CliRunner().invoke(app, ["config", "validate", "--json", str(EXAMPLES / "minimal-client.json")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert payload["source_format"] == "minimal-client"
    assert payload["schema_version"] == 1


def test_config_show_cli_prints_runtime_json_by_default() -> None:
    result = CliRunner().invoke(app, ["config", "show", str(EXAMPLES / "dns-helper.json")])

    assert result.exit_code == 0
    assert '"kind": "dns"' in result.output
    assert '"runtime_model": "RuntimeConfig"' in result.output
    assert '"schema_version": 1' in result.output


def test_config_show_cli_can_print_canonical_json() -> None:
    result = CliRunner().invoke(app, ["config", "show", "--canonical", str(EXAMPLES / "dns-helper.json")])

    assert result.exit_code == 0
    assert '"dns"' in result.output
    assert '"runtime_model"' not in result.output
    assert '"schema_version": 1' in result.output


def test_ipv6_example_config_preserves_bracketed_udp_endpoints() -> None:
    config = validate_config_file(EXAMPLES / "minimal-ipv6-client.json")

    assert config.paths[0].source_ip == "2001:db8::10"
    assert config.paths[0].gateway == "2001:db8::1"
    assert config.services[0].listen == "[::1]:55180"
    assert config.services[0].target == "[::1]:51820"
