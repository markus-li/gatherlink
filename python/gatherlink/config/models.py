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
PathSchedulerState = Literal["active", "busy", "drain", "disabled"]
SchedulerPolicy = Literal[
    "round_robin",
    "weighted_round_robin",
    "srtt",
    "lowest_latency",
    "loss_aware",
    "capacity_aware",
    "least_queue",
    "earliest_completion_first",
    "blocking_estimation",
    "balanced",
    "adaptive",
]
ServicePriority = Literal["bulk", "normal", "high", "critical"]
ServiceReturnMode = Literal["fixed", "learned-single-source"]
RESERVED_SERVICE_ID_END = 255
USER_SERVICE_ID_START = RESERVED_SERVICE_ID_END + 1


class PathSchedulerConfig(GatherlinkBaseModel):
    """Python-owned scheduler hints for one configured path."""

    # TODO(scheduler-telemetry): Treat these fields as startup hints and policy
    # overrides. Live capacity, loss, latency, and queue pressure should keep
    # flowing through the scheduler telemetry pipeline so Python can recompile
    # this into Rust's small execution primitives during hot reapply.
    enabled: bool = True
    state: PathSchedulerState = "active"
    weight: int = Field(default=1, ge=1, le=65535)
    mtu: int = Field(default=1200, ge=64, le=65535)
    tx_capacity_bps: int | None = Field(default=None, ge=1)
    rx_capacity_bps: int | None = Field(default=None, ge=1)
    latency_us: int | None = Field(default=None, ge=0)
    loss_ppm: int = Field(default=0, ge=0, le=1_000_000)
    reorder_hold_us: int = Field(default=0, ge=0)
    max_in_flight_packets: int = Field(default=0, ge=0, le=65535)
    max_in_flight_bytes: int = Field(default=0, ge=0)


class SchedulerConfig(GatherlinkBaseModel):
    """User-visible scheduler policy selected by Python and executed by Rust."""

    # TODO(adaptive-scheduler): Keep this policy label human-sized. The complex
    # decisions belong in Python scoring modules and should compile down to path
    # weights, states, and primitive limits before Rust sees them.
    mode: SchedulerPolicy = "round_robin"


class PathConfig(GatherlinkBaseModel):
    """A declared physical path that may produce one or more logical paths."""

    name: str
    interface: str
    source_ip: str | None = None
    gateway: str | None = None
    # TODO(path-transport-discovery): These endpoints are explicit for the first
    # Rust-backed lab and manual deployments. Later carrier discovery can fill
    # them from validated interface/link facts without changing Rust policy.
    transport_bind: str | None = None
    transport_remote: str | None = None
    scheduler: PathSchedulerConfig = Field(default_factory=PathSchedulerConfig)


class ServiceConfig(GatherlinkBaseModel):
    """A virtual UDP service exposed through the Gatherlink fabric."""

    name: str
    target: str
    # TODO(service-id-registry): Keep user/application ids in the explicit
    # 256..65535 range. Low ids are Gatherlink internals such as control
    # metadata, time sync, diagnostics, and future auth/crypto handshakes.
    service_id: int | None = Field(default=None, ge=USER_SERVICE_ID_START, le=65535)
    listen: str | None = None
    priority: ServicePriority = "normal"
    # TODO(service-scheduler-policy): These are Rust-executed primitives, not
    # user-facing policy. Python should map names like duplicate_small into
    # fanout values after it has considered service intent and path telemetry.
    # fanout=0 means every eligible path. fanout_below_bytes=0 means fanout
    # applies to every payload; otherwise larger payloads use one scheduled path.
    scheduler_fanout: int = Field(default=1, ge=0, le=65535)
    scheduler_fanout_below_bytes: int = Field(default=0, ge=0)
    # TODO(service-return-policy): Fixed return ports are the production shape
    # and match WireGuard/helper usage. The learned mode exists for simple UDP
    # tools and early manual tests, not as a general UDP NAT/VPN feature.
    return_mode: ServiceReturnMode = "fixed"


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
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
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
        if len(self.services) > 65535 - USER_SERVICE_ID_START + 1:
            raise ValueError("too many services for the u16 user service id range")

        service_ids = [service.service_id for service in self.services if service.service_id is not None]
        if len(set(service_ids)) != len(service_ids):
            raise ValueError("service ids must be unique")

        service_listens = [service.listen for service in self.services if service.listen is not None]
        if len(set(service_listens)) != len(service_listens):
            raise ValueError("service listen addresses must be unique")

        path_names = {path.name for path in self.paths}
        if len(path_names) != len(self.paths):
            raise ValueError("path names must be unique")

        if self.helpers.wireguard and self.helpers.wireguard.service not in service_names:
            raise ValueError("wireguard helper service must reference an existing service")

        return self
