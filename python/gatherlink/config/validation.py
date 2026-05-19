"""
Validate raw Gatherlink configuration into canonical models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gatherlink.config.errors import ConfigErrorDetail, ConfigValidationError
from gatherlink.config.loader import load_config_dict
from gatherlink.config.models import ConfigFormat, GatherlinkConfig
from gatherlink.config.versions import normalize_config_for_schema


def detect_config_format(data: dict[str, Any]) -> ConfigFormat:
    """Detect the current supported config example format."""
    helpers = data.get("helpers") or {}
    services = data.get("services") or []
    role = data.get("role")

    # Format detection intentionally stays shallow. Deep validation belongs in
    # GatherlinkConfig so every source format goes through the same relationship
    # checks once it is normalized.
    if helpers.get("dns") and not data.get("services"):
        return "dns-helper"
    if helpers.get("socks5"):
        return "socks5-helper"
    if helpers.get("tcp_forward"):
        return "tcp-forward-helper"

    has_wireguard = bool(helpers.get("wireguard")) or any(
        isinstance(service, dict) and str(service.get("name", "")).startswith("wireguard") for service in services
    )
    if role == "server":
        return "wireguard-server" if has_wireguard else "minimal-server"

    return "wireguard-client" if has_wireguard else "minimal-client"


def _pydantic_details(error: ValidationError) -> list[ConfigErrorDetail]:
    """Convert Pydantic's rich error list into stable Gatherlink error details."""
    return [
        ConfigErrorDetail(
            message=str(item.get("msg", "invalid value")), location=tuple(str(part) for part in item["loc"])
        )
        for item in error.errors()
    ]


def validate_config_dict(data: dict[str, Any], *, source_format: ConfigFormat | None = None) -> GatherlinkConfig:
    """Validate a raw config dictionary into a canonical GatherlinkConfig."""
    config_format = source_format or detect_config_format(data)
    try:
        normalized_data = normalize_config_for_schema(data, source_format=config_format)
        return GatherlinkConfig.from_mapping(normalized_data, source_format=config_format)
    except ValidationError as exc:
        raise ConfigValidationError(
            "config validation failed",
            source_format=config_format,
            details=_pydantic_details(exc),
        ) from exc
    except ConfigValidationError:
        raise
    except ValueError as exc:
        raise ConfigValidationError(str(exc), source_format=config_format) from exc


def validate_config_file(path: Path, *, source_format: ConfigFormat | None = None) -> GatherlinkConfig:
    """Load and validate a config file."""
    try:
        data = load_config_dict(path)
        return validate_config_dict(data, source_format=source_format)
    except ConfigValidationError as exc:
        if exc.path is None:
            exc.path = path
        raise
