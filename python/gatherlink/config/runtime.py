"""
Explicit runtime configuration models.

The user-facing config models are intentionally small. These runtime models are
the next boundary: every default, helper reference, and derived value that later
orchestration code needs should be made explicit here before anything starts
services or talks to the Rust dataplane.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from gatherlink.config.models import NodeRole
from gatherlink.shared.models import GatherlinkBaseModel


class RuntimePathConfig(GatherlinkBaseModel):
    """A physical path after user config has been expanded for runtime use."""

    name: str
    interface: str
    source_ip: str | None = None
    gateway: str | None = None


class RuntimeServiceConfig(GatherlinkBaseModel):
    """A UDP service with all runtime-visible endpoints made explicit."""

    name: str
    protocol: Literal["udp"] = "udp"
    target: str
    listen: str | None = None


class RuntimeWireGuardHelperConfig(GatherlinkBaseModel):
    """Runtime-ready WireGuard helper settings."""

    kind: Literal["wireguard"] = "wireguard"
    enabled: bool = True
    service: str
    service_target: str
    service_listen: str | None = None


class RuntimeDnsHelperConfig(GatherlinkBaseModel):
    """Runtime-ready DNS helper settings."""

    kind: Literal["dns"] = "dns"
    enabled: bool = True
    listen: str
    strategy: str


RuntimeHelperConfig = RuntimeWireGuardHelperConfig | RuntimeDnsHelperConfig


class RuntimeConfig(GatherlinkBaseModel):
    """The explicit runtime contract produced from a validated Gatherlink config."""

    schema_version: int
    node: str
    role: NodeRole
    peer: str | None = None
    paths: list[RuntimePathConfig] = Field(default_factory=list)
    services: list[RuntimeServiceConfig] = Field(default_factory=list)
    helpers: list[RuntimeHelperConfig] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
