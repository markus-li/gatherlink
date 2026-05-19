"""
Schema-version routing for Gatherlink configuration files.

This module is the intentional upgrade point for future config schemas. The rest
of the config layer should ask this module which schema handler to use instead
of sprinkling version checks across loaders, CLI commands, and Pydantic models.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from gatherlink.config.errors import ConfigErrorDetail, ConfigValidationError
from gatherlink.config.models import ConfigFormat

CURRENT_SCHEMA_VERSION: Final = 1

SchemaNormalizer = Callable[[dict[str, Any], ConfigFormat], dict[str, Any]]


@dataclass(frozen=True)
class ConfigSchema:
    """A registered config schema version and its raw-data normalizer."""

    version: int
    description: str
    normalize: SchemaNormalizer


def _normalize_v1_config(data: dict[str, Any], source_format: ConfigFormat) -> dict[str, Any]:
    """Return v1 config data in the canonical shape expected by GatherlinkConfig."""
    # Version 1 already uses the canonical field names. Keeping this as a real
    # function makes the v2 path obvious: add a v2 normalizer here, register it
    # below, and migrate old/new raw shapes before Pydantic validation runs.
    return dict(data)


SUPPORTED_CONFIG_SCHEMAS: Final[dict[int, ConfigSchema]] = {
    1: ConfigSchema(
        version=1,
        description="initial Gatherlink JSON config schema",
        normalize=_normalize_v1_config,
    ),
}


def supported_schema_versions() -> tuple[int, ...]:
    """Return supported schema versions in display order."""
    return tuple(sorted(SUPPORTED_CONFIG_SCHEMAS))


def require_config_schema(data: dict[str, Any], *, source_format: ConfigFormat) -> ConfigSchema:
    """Return the registered schema handler for a raw config dictionary."""
    if "schema_version" not in data:
        raise ConfigValidationError(
            "config schema_version is required",
            source_format=source_format,
            details=[
                ConfigErrorDetail(
                    "add schema_version so future migrations can choose the correct parser",
                    location=("schema_version",),
                )
            ],
        )

    raw_version = data["schema_version"]
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ConfigValidationError(
            "config schema_version must be an integer",
            source_format=source_format,
            details=[ConfigErrorDetail("expected an integer schema version", location=("schema_version",))],
        )

    schema = SUPPORTED_CONFIG_SCHEMAS.get(raw_version)
    if schema is None:
        supported = ", ".join(str(version) for version in supported_schema_versions())
        raise ConfigValidationError(
            f"unsupported config schema_version {raw_version}",
            source_format=source_format,
            details=[
                ConfigErrorDetail(
                    f"schema_version must be one of: {supported}",
                    location=("schema_version",),
                )
            ],
        )

    return schema


def normalize_config_for_schema(data: dict[str, Any], *, source_format: ConfigFormat) -> dict[str, Any]:
    """Normalize raw config data through the registered schema handler."""
    schema = require_config_schema(data, source_format=source_format)
    return schema.normalize(data, source_format)
