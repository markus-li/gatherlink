"""
User-facing Pydantic configuration models.

These models represent the canonical Gatherlink config shape after any supported
input format has been normalized.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from gatherlink.shared.models import FieldTransform, GatherlinkBaseModel

NodeRole = Literal["client", "server"]
SecurityMode = Literal["none"]
ConfigFormat = Literal["minimal-client", "minimal-server", "wireguard-client", "wireguard-server", "dns-helper"]


class PathConfig(GatherlinkBaseModel):
    """A declared physical path that may produce one or more logical paths."""

    name: str
    interface: str
    source_ip: str | None = None
    gateway: str | None = None


class ServiceConfig(GatherlinkBaseModel):
    """A virtual UDP service exposed through the Gatherlink fabric."""

    name: str
    target: str
    listen: str | None = None


class SecurityConfig(GatherlinkBaseModel):
    """Transport security mode selected by the Python control plane."""

    # TODO: Add authenticated modes here when packet crypto lands. Keeping the
    # plaintext mode explicit now lets the local lab run while making the unsafe
    # boundary visible in runtime plans and logs.
    mode: SecurityMode = "none"


def _security_or_default(value: dict | None) -> dict:
    """Return explicit security config or the current plaintext lab default."""
    return value or {}


class WireGuardHelperConfig(GatherlinkBaseModel):
    """Optional WireGuard helper configuration."""

    enabled: bool = True
    service: str


class DnsHelperConfig(GatherlinkBaseModel):
    """Optional DNS helper configuration."""

    enabled: bool = True
    listen: str = "127.0.0.1:5353"
    strategy: str = "race_first_valid"


class HelpersConfig(GatherlinkBaseModel):
    """Optional helper configuration block."""

    wireguard: WireGuardHelperConfig | None = None
    dns: DnsHelperConfig | None = None


class GatherlinkConfig(GatherlinkBaseModel):
    """Canonical user configuration consumed by the Python control plane."""

    schema_version: int
    node: str = "local"
    role: NodeRole = "client"
    peer: str | None = None
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    paths: list[PathConfig] = Field(default_factory=list)
    services: list[ServiceConfig] = Field(default_factory=list)
    helpers: HelpersConfig = Field(default_factory=HelpersConfig)

    # These maps keep example input formats explicit. The detector in validation.py
    # only chooses a source format; this canonical model still performs the real
    # normalization and cross-field validation for every input path.
    # TODO: Add schema_version migration before accepting persisted appliance configs.
    # Version 1 is intentionally explicit in all examples so upgrades never depend
    # on guessing what a versionless file meant.
    __field_maps__: ClassVar = {
        "minimal-client": {
            "schema_version": "schema_version",
            "node": "node",
            "role": FieldTransform("client"),
            "peer": "peer",
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": "paths",
            "services": "services",
            "helpers": FieldTransform({}),
        },
        "minimal-server": {
            "schema_version": "schema_version",
            "node": "node",
            "role": "role",
            "peer": FieldTransform(None),
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": FieldTransform([]),
            "services": "services",
            "helpers": FieldTransform({}),
        },
        "wireguard-client": {
            "schema_version": "schema_version",
            "node": "node",
            "role": FieldTransform("client"),
            "peer": "peer",
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": "paths",
            "services": "services",
            "helpers": "helpers",
        },
        "wireguard-server": {
            "schema_version": "schema_version",
            "node": "node",
            "role": "role",
            "peer": FieldTransform(None),
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": FieldTransform([]),
            "services": "services",
            "helpers": FieldTransform({}),
        },
        "dns-helper": {
            "schema_version": "schema_version",
            "node": FieldTransform("local"),
            "role": FieldTransform("client"),
            "peer": FieldTransform(None),
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": FieldTransform([]),
            "services": FieldTransform([]),
            "helpers": "helpers",
        },
    }

    @model_validator(mode="after")
    def validate_config_relationships(self) -> GatherlinkConfig:
        """Validate relationships that depend on multiple config sections."""
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")

        if self.role == "client" and self.services and not self.peer:
            raise ValueError("client configs with services must define peer")

        service_names = {service.name for service in self.services}
        if len(service_names) != len(self.services):
            raise ValueError("service names must be unique")

        path_names = {path.name for path in self.paths}
        if len(path_names) != len(self.paths):
            raise ValueError("path names must be unique")

        if self.helpers.wireguard and self.helpers.wireguard.service not in service_names:
            raise ValueError("wireguard helper service must reference an existing service")

        return self
