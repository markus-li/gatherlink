from __future__ import annotations

import json

import pytest
from gatherlink.cli.main import app
from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.migration import ConfigMigrationRegistry, ConfigMigrationStep, migrate_config_dict
from typer.testing import CliRunner


def test_config_migration_identity_keeps_v1_config() -> None:
    data = {
        "schema_version": 1,
        "node": "node-a",
        "peer": "node-b",
        "paths": [],
        "services": [],
    }

    result = migrate_config_dict(data, source_format="minimal-client")

    assert result.changed is False
    assert result.source_version == 1
    assert result.target_version == 1
    assert result.steps == []
    assert result.config == data


def test_config_migration_chains_up_and_down_through_intermediary_versions() -> None:
    registry = ConfigMigrationRegistry(supported_versions={1})
    registry.register(
        ConfigMigrationStep(
            from_version=1,
            to_version=2,
            description="move node to identity.name",
            transform=lambda data: {
                **{key: value for key, value in data.items() if key != "node"},
                "schema_version": 2,
                "identity": {"name": data["node"]},
            },
        )
    )
    registry.register(
        ConfigMigrationStep(
            from_version=2,
            to_version=3,
            description="rename services to user_services",
            transform=lambda data: {
                **{key: value for key, value in data.items() if key != "services"},
                "schema_version": 3,
                "user_services": data.get("services", []),
            },
        )
    )
    registry.register(
        ConfigMigrationStep(
            from_version=3,
            to_version=2,
            description="rename user_services to services",
            transform=lambda data: {
                **{key: value for key, value in data.items() if key != "user_services"},
                "schema_version": 2,
                "services": data.get("user_services", []),
            },
        )
    )
    registry.register(
        ConfigMigrationStep(
            from_version=2,
            to_version=1,
            description="move identity.name to node",
            transform=lambda data: {
                **{key: value for key, value in data.items() if key != "identity"},
                "schema_version": 1,
                "node": data["identity"]["name"],
            },
        )
    )
    v1 = {"schema_version": 1, "node": "node-a", "services": [{"name": "svc", "target": "127.0.0.1:1"}]}

    v3 = migrate_config_dict(v1, target_version=3, source_format="minimal-client", registry=registry)
    back_to_v1 = migrate_config_dict(v3.config, target_version=1, source_format="minimal-client", registry=registry)

    assert [step.description for step in v3.steps] == [
        "move node to identity.name",
        "rename services to user_services",
    ]
    assert v3.config["identity"] == {"name": "node-a"}
    assert v3.config["user_services"] == [{"name": "svc", "target": "127.0.0.1:1"}]
    assert [step.direction for step in back_to_v1.steps] == ["downgrade", "downgrade"]
    assert len(back_to_v1.warnings) == 2
    assert back_to_v1.config["node"] == "node-a"
    assert back_to_v1.config["services"] == [{"name": "svc", "target": "127.0.0.1:1"}]


def test_config_migration_fails_without_explicit_path() -> None:
    registry = ConfigMigrationRegistry(supported_versions={1, 2})
    data = {"schema_version": 1, "node": "node-a"}

    with pytest.raises(ConfigValidationError, match="no config migration path"):
        migrate_config_dict(data, target_version=2, source_format="minimal-client", registry=registry)


def test_config_migrate_cli_dry_run_outputs_identity_result(tmp_path) -> None:
    path = tmp_path / "minimal-client.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node": "node-a",
                "peer": "node-b",
                "paths": [],
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["config", "migrate", str(path)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["changed"] is False
    assert payload["target_version"] == 1
    assert payload["config"]["node"] == "node-a"
