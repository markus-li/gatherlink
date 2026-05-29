"""
Versioned config migration engine.

The production config schema is still v1, but the migration path is real: each
version hop is an explicit transform, and larger moves are chained through the
intermediary versions. Tests can register synthetic future versions without
changing the active schema.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from gatherlink.config.errors import ConfigErrorDetail, ConfigValidationError
from gatherlink.config.models import ConfigFormat
from gatherlink.config.versions import CURRENT_SCHEMA_VERSION, require_config_schema, supported_schema_versions
from gatherlink.shared.models import GatherlinkBaseModel

MigrationDirection = Literal["identity", "upgrade", "downgrade"]
MigrationTransform = Callable[[dict[str, Any]], dict[str, Any]]


class ConfigMigrationStepReport(GatherlinkBaseModel):
    """Operator-visible report for one schema transform hop."""

    from_version: int
    to_version: int
    direction: MigrationDirection
    description: str


class ConfigMigrationResult(GatherlinkBaseModel):
    """Pydantic result model returned by config migration commands and tests."""

    source_version: int
    target_version: int
    changed: bool
    steps: list[ConfigMigrationStepReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    config: dict[str, Any]


@dataclass(frozen=True)
class ConfigMigrationStep:
    """One explicit version-to-version config transform."""

    from_version: int
    to_version: int
    description: str
    transform: MigrationTransform

    @property
    def direction(self) -> MigrationDirection:
        if self.from_version == self.to_version:
            return "identity"
        return "upgrade" if self.to_version > self.from_version else "downgrade"

    def apply(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply this hop and require the result to declare the target version."""
        migrated = self.transform(dict(data))
        if migrated.get("schema_version") != self.to_version:
            raise ValueError(
                f"migration {self.from_version}->{self.to_version} did not produce schema_version "
                f"{self.to_version}"
            )
        return migrated


class ConfigMigrationRegistry:
    """Explicit config migration graph supporting upgrade and downgrade chains."""

    def __init__(self, *, supported_versions: set[int] | None = None) -> None:
        self.supported_versions = set(supported_versions or supported_schema_versions())
        self._steps: dict[tuple[int, int], ConfigMigrationStep] = {}

    def register(self, step: ConfigMigrationStep) -> None:
        """Register one migration hop."""
        self.supported_versions.add(step.from_version)
        self.supported_versions.add(step.to_version)
        self._steps[(step.from_version, step.to_version)] = step

    def migrate(
        self,
        data: dict[str, Any],
        *,
        target_version: int = CURRENT_SCHEMA_VERSION,
        source_format: ConfigFormat,
    ) -> ConfigMigrationResult:
        """Migrate a raw config dictionary to the requested schema version."""
        source_version = _read_schema_version(data, source_format=source_format)
        if target_version not in self.supported_versions:
            raise ConfigValidationError(
                f"unsupported target config schema_version {target_version}",
                source_format=source_format,
                details=[
                    ConfigErrorDetail(
                        f"target schema_version must be one of: {_supported_display(self.supported_versions)}",
                        location=("schema_version",),
                    )
                ],
            )
        if source_version == target_version:
            # Identity still validates the current production source schema when
            # it is one of the registered runtime schemas. Custom test-only
            # versions can use custom registries without touching runtime
            # validation.
            if source_version in set(supported_schema_versions()):
                require_config_schema(data, source_format=source_format)
            return ConfigMigrationResult(
                source_version=source_version,
                target_version=target_version,
                changed=False,
                config=dict(data),
            )

        path = self._find_path(source_version, target_version)
        if path is None:
            raise ConfigValidationError(
                f"no config migration path from schema_version {source_version} to {target_version}",
                source_format=source_format,
                details=[
                    ConfigErrorDetail(
                        "add explicit version-to-version transforms instead of ad hoc migration",
                        location=("schema_version",),
                    )
                ],
            )

        current = dict(data)
        reports: list[ConfigMigrationStepReport] = []
        warnings: list[str] = []
        for step in path:
            current = step.apply(current)
            reports.append(
                ConfigMigrationStepReport(
                    from_version=step.from_version,
                    to_version=step.to_version,
                    direction=step.direction,
                    description=step.description,
                )
            )
            if step.direction == "downgrade":
                warnings.append(
                    f"downgraded schema_version {step.from_version} to {step.to_version}; "
                    "review output before replacing newer configs"
                )

        return ConfigMigrationResult(
            source_version=source_version,
            target_version=target_version,
            changed=True,
            steps=reports,
            warnings=warnings,
            config=current,
        )

    def _find_path(self, source_version: int, target_version: int) -> list[ConfigMigrationStep] | None:
        queue: deque[tuple[int, list[ConfigMigrationStep]]] = deque([(source_version, [])])
        seen = {source_version}
        while queue:
            version, path = queue.popleft()
            for (step_source, step_target), step in sorted(self._steps.items()):
                if step_source != version or step_target in seen:
                    continue
                next_path = [*path, step]
                if step_target == target_version:
                    return next_path
                seen.add(step_target)
                queue.append((step_target, next_path))
        return None


def default_migration_registry() -> ConfigMigrationRegistry:
    """Return the production migration registry."""
    return ConfigMigrationRegistry()


def migrate_config_dict(
    data: dict[str, Any],
    *,
    target_version: int = CURRENT_SCHEMA_VERSION,
    source_format: ConfigFormat,
    registry: ConfigMigrationRegistry | None = None,
) -> ConfigMigrationResult:
    """Migrate a raw config dictionary through the configured registry."""
    return (registry or default_migration_registry()).migrate(
        data,
        target_version=target_version,
        source_format=source_format,
    )


def _read_schema_version(data: dict[str, Any], *, source_format: ConfigFormat) -> int:
    raw_version = data.get("schema_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ConfigValidationError(
            "config schema_version must be an integer",
            source_format=source_format,
            details=[ConfigErrorDetail("expected an integer schema version", location=("schema_version",))],
        )
    return raw_version


def _supported_display(versions: set[int]) -> str:
    return ", ".join(str(version) for version in sorted(versions))
