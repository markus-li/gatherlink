"""
Configuration error types.

Config errors are intentionally normalized before they reach the CLI. That keeps
human output and future JSON/API output consistent even as the Pydantic model tree
grows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConfigErrorDetail:
    """A single structured configuration validation problem."""

    message: str
    location: tuple[str, ...] = ()

    def export_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation of this error detail."""
        return {"message": self.message, "location": list(self.location)}


class ConfigValidationError(ValueError):
    """Raised when a config cannot be loaded, detected, or validated."""

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        source_format: str | None = None,
        details: list[ConfigErrorDetail] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.source_format = source_format
        self.details = details or []

    def export_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation suitable for CLI and API output."""
        return {
            "valid": False,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "source_format": self.source_format,
            "errors": [detail.export_dict() for detail in self.details],
        }
