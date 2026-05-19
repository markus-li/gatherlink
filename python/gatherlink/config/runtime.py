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

from gatherlink.config.models import NodeRole, PathSchedulerState, SecurityMode, ServicePriority
from gatherlink.shared.models import GatherlinkBaseModel

SchedulerMode = Literal["round_robin"]


class RuntimePathSchedulerConfig(GatherlinkBaseModel):
    """Compiled per-path scheduler state consumed by Rust."""

    path_id: int
    route_id: int = 0
    enabled: bool = True
    state: PathSchedulerState = "active"
    weight: int = 1
    mtu: int = 1200


class RuntimeSchedulerConfig(GatherlinkBaseModel):
    """Compiled scheduler runtime mode and path state."""

    mode: SchedulerMode = "round_robin"
    paths: list[RuntimePathSchedulerConfig] = Field(default_factory=list)


class RuntimePathConfig(GatherlinkBaseModel):
    """A physical path after user config has been expanded for runtime use."""

    name: str
    interface: str
    source_ip: str | None = None
    gateway: str | None = None
    scheduler: RuntimePathSchedulerConfig


class RuntimeServiceConfig(GatherlinkBaseModel):
    """A UDP service with all runtime-visible endpoints made explicit."""

    name: str
    protocol: Literal["udp"] = "udp"
    target: str
    listen: str | None = None
    priority: ServicePriority = "normal"
    priority_value: int = 100


class RuntimeSecurityConfig(GatherlinkBaseModel):
    """Runtime-visible transport security mode."""

    mode: SecurityMode = "none"


class RuntimeWireGuardHelperConfig(GatherlinkBaseModel):
    """Runtime-ready WireGuard helper settings outside the core runner."""

    kind: Literal["wireguard"] = "wireguard"
    enabled: bool = True
    service: str
    service_target: str
    service_listen: str | None = None


class RuntimeDnsHelperConfig(GatherlinkBaseModel):
    """Runtime-ready DNS helper settings outside the core runner."""

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
    security: RuntimeSecurityConfig = Field(default_factory=RuntimeSecurityConfig)
    paths: list[RuntimePathConfig] = Field(default_factory=list)
    services: list[RuntimeServiceConfig] = Field(default_factory=list)
    scheduler: RuntimeSchedulerConfig = Field(default_factory=RuntimeSchedulerConfig)
    # Helpers are kept in the runtime contract for helper supervisors to consume,
    # but the core runner must ignore them. Tunneling, DNS assistance, and other
    # integrations are not part of the core userland UDP transport.
    helpers: list[RuntimeHelperConfig] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
