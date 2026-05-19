"""
Load Gatherlink configuration files from disk.
"""

from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from gatherlink.config.errors import ConfigErrorDetail, ConfigValidationError


def load_config_dict(path: Path) -> dict[str, Any]:
    """Load a JSON config file as a dictionary."""
    try:
        with path.open("r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except OSError as exc:
        raise ConfigValidationError(
            f"could not read config file: {exc}",
            path=path,
            details=[ConfigErrorDetail(str(exc), location=("file",))],
        ) from exc
    except JSONDecodeError as exc:
        raise ConfigValidationError(
            f"invalid JSON: {exc.msg}",
            path=path,
            details=[ConfigErrorDetail(exc.msg, location=(f"line {exc.lineno}", f"column {exc.colno}"))],
        ) from exc

    if not isinstance(data, dict):
        raise ConfigValidationError(
            "config must contain a JSON object",
            path=path,
            details=[ConfigErrorDetail("top-level JSON value must be an object")],
        )

    return data
