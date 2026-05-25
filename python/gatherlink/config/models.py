"""
User-facing Pydantic configuration models.

These models represent the canonical Gatherlink config shape after any supported
input format has been normalized.
"""

from __future__ import annotations

from base64 import b64decode
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from gatherlink.shared.models import FieldTransform, GatherlinkBaseModel

NodeRole = Literal["client", "server"]
SecurityMode = Literal["none", "static", "authenticated"]
CarrierKind = Literal["udp", "quic-datagram", "http3-datagram"]
ConfigFormat = Literal[
    "minimal-client",
    "minimal-server",
    "wireguard-client",
    "wireguard-server",
    "dns-helper",
    "socks5-helper",
    "tcp-forward-helper",
]
PathSchedulerState = Literal["active", "busy", "drain", "disabled"]
SchedulerTrafficBias = Literal["auto", "tcp", "udp"]
ServiceSchedulerPathPolicy = Literal["inherit", "single_best_path", "weighted_round_robin"]
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
    "ordered_multipath",
    "ordered_multipath_capacity_aware",
    "single_best_path",
    "arrival_guarded_capacity",
    "flowlet_adaptive",
    "latency_guarded_capacity",
    "balanced",
    "adaptive",
    "coordinated_adaptive",
]
ServicePriority = Literal["bulk", "normal", "high", "critical"]
ServiceTrafficClass = Literal["unknown", "tcp_ordered", "udp_bulk", "latency_sensitive", "control"]
ServiceReturnMode = Literal["fixed", "learned-single-source", "peer-scoped-source"]
DnsUpstreamKind = Literal["direct", "tunnel", "doh"]
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
    pacing_budget_bps: int = Field(default=0, ge=0)


class PathRelayHopConfig(GatherlinkBaseModel):
    """Optional outer relay-hop wrapping facts for one path."""

    # TODO(relay-provisioning): This is a low-level compiled shape for manual
    # VM acceptance and early signed-topology output. Normal configs should
    # receive these facts from Python provisioning rather than hand-authored
    # base64 keys.
    relay_receiver_index: int = Field(ge=0, le=2**32 - 1)
    send_key: str

    @model_validator(mode="after")
    def validate_relay_key(self) -> PathRelayHopConfig:
        """Ensure relay-hop key material is present and exactly one AEAD key."""
        try:
            decoded = b64decode(self.send_key, validate=True)
        except ValueError as exc:
            raise ValueError("path relay send_key must be base64") from exc
        if len(decoded) != 32:
            raise ValueError("path relay send_key must decode to 32 bytes")
        return self


class SchedulerConfig(GatherlinkBaseModel):
    """User-visible scheduler policy selected by Python and executed by Rust."""

    # TODO(adaptive-scheduler): Keep this policy label human-sized. The complex
    # decisions belong in Python scoring modules and should compile down to path
    # weights, states, and primitive limits before Rust sees them.
    mode: SchedulerPolicy = "round_robin"
    # This is intentionally a coarse operator/service hint. Python may bias a
    # meta-policy such as coordinated_adaptive toward TCP-like order stability
    # or UDP-like aggregation, while Rust still receives only primitive path
    # state and weights.
    traffic_bias: SchedulerTrafficBias = "auto"


class PathConfig(GatherlinkBaseModel):
    """A declared physical path that may produce one or more logical paths."""

    name: str
    interface: str
    source_ip: str | None = None
    gateway: str | None = None
    # TODO(path-transport-discovery): These endpoints are explicit for the first
    # Rust-backed lab and manual deployments. Later carrier discovery can fill
    # them from validated interface/link facts without changing Rust policy.
    carrier: CarrierKind = "udp"
    transport_bind: str | None = None
    transport_remote: str | None = None
    carrier_max_datagram_size: int | None = Field(default=None, ge=256, le=65535)
    scheduler: PathSchedulerConfig = Field(default_factory=PathSchedulerConfig)
    relay: PathRelayHopConfig | None = None


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
    # TODO(service-traffic-class): Python owns service meaning. Helpers or
    # operator config may classify traffic before encryption so schedulers can
    # choose conservative TCP-like or bulk UDP-like primitives without Rust
    # inspecting payloads or learning helper semantics.
    traffic_class: ServiceTrafficClass = "unknown"
    # TODO(service-scheduler-policy): These are Rust-executed primitives, not
    # user-facing policy. Python should map names like duplicate_small into
    # fanout values after it has considered service intent and path telemetry.
    # fanout=0 means every eligible path. fanout_below_bytes=0 means fanout
    # applies to every payload; otherwise larger payloads use one scheduled path.
    scheduler_fanout: int = Field(default=1, ge=0, le=65535)
    scheduler_fanout_below_bytes: int = Field(default=0, ge=0)
    scheduler_flowlet_idle_us: int = Field(default=0, ge=0)
    scheduler_flowlet_max_hold_us: int = Field(default=0, ge=0)
    # TODO(service-qos): This is a Python-owned service budget primitive. It
    # limits how many packets one service may drain per Rust bridge slot; zero
    # keeps the global runner batch. Use it for automatic mixed-service tuning
    # before adding heavier Rust-side queue policy.
    scheduler_poll_batch_packets: int = Field(default=0, ge=0)
    scheduler_path_run_datagrams: int = Field(default=0, ge=0)
    # TODO(per-service-scheduler): This is still a compiled primitive, not a
    # policy engine in Rust. Python decides when a service needs a path policy
    # different from the node-wide scheduler, then Rust only executes this small
    # selector. It is mainly used by dual WireGuard profiles where the stable
    # service should stay conservative while the fast service can inherit an
    # aggregation-friendly node policy.
    scheduler_path_policy: ServiceSchedulerPathPolicy = "inherit"
    # TODO(per-service-scheduler): This is an eligibility primitive compiled by
    # Python. Use path names here so operator config stays readable; expansion
    # converts them to compact path ids before Rust sees them.
    scheduler_allowed_paths: list[str] = Field(default_factory=list)
    # TODO(per-service-scheduler): These weights let Python compile an
    # independent per-service path plan after considering both service intent
    # and aggregate path pressure. Rust only executes the resulting path-id
    # weights; it does not decide why one service prefers a different split.
    scheduler_path_weights: dict[str, int] = Field(default_factory=dict)
    # TODO(service-return-policy): Fixed return ports are the common production
    # shape. The peer-scoped mode is for server-like helpers, including
    # WireGuard, that need one local app-facing UDP source per authenticated
    # Gatherlink peer. The learned mode exists for simple UDP tools and early
    # manual tests, not as a general UDP NAT/VPN feature.
    return_mode: ServiceReturnMode = "fixed"


class SecuritySessionConfig(GatherlinkBaseModel):
    """One explicit peer session for shared sink carrier sockets."""

    # TODO(shared-sink-provisioning): Prefer signed/authenticated provisioning
    # to hand-authored static sessions. Explicit static sessions remain useful
    # for labs that prove several authenticated peers can share one sink port.
    name: str | None = None
    local_receiver_index: int = Field(ge=0, le=2**32 - 1)
    remote_receiver_index: int = Field(ge=0, le=2**32 - 1)
    send_key: str
    receive_key: str
    services: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_session_keys(self) -> SecuritySessionConfig:
        """Ensure explicit static session keys are complete and well sized."""
        for field_name, value in {"send_key": self.send_key, "receive_key": self.receive_key}.items():
            try:
                decoded = b64decode(value, validate=True)
            except ValueError as exc:
                raise ValueError(f"security.sessions[].{field_name} must be base64") from exc
            if len(decoded) != 32:
                raise ValueError(f"security.sessions[].{field_name} must decode to 32 bytes")
        return self


class SecurityConfig(GatherlinkBaseModel):
    """Transport security mode selected by the Python control plane."""

    # Noise IK provisioning is the normal v0.9 path for producing authenticated
    # config-facing AEAD facts. Python verifies identities and compiles
    # short-lived session material; Rust only executes those facts at packet
    # rate. Static remains explicit lab/manual provisioning.
    mode: SecurityMode = "none"
    receiver_index: int = Field(default=1, ge=0, le=2**32 - 1)
    local_receiver_index: int | None = Field(default=None, ge=0, le=2**32 - 1)
    remote_receiver_index: int | None = Field(default=None, ge=0, le=2**32 - 1)
    send_key: str | None = None
    receive_key: str | None = None
    sessions: list[SecuritySessionConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_security_material(self) -> SecurityConfig:
        """Ensure explicit static crypto material is complete and well sized."""
        if self.mode == "none":
            return self
        if self.sessions:
            receiver_indexes = [session.local_receiver_index for session in self.sessions]
            if len(set(receiver_indexes)) != len(receiver_indexes):
                raise ValueError("security.sessions local_receiver_index values must be unique")
            return self
        if self.send_key is None or self.receive_key is None:
            raise ValueError(f"security.mode={self.mode} requires send_key and receive_key")
        for field_name, value in {"send_key": self.send_key, "receive_key": self.receive_key}.items():
            try:
                decoded = b64decode(value, validate=True)
            except ValueError as exc:
                raise ValueError(f"security.{field_name} must be base64") from exc
            if len(decoded) != 32:
                raise ValueError(f"security.{field_name} must decode to 32 bytes")
        return self


def _security_or_default(value: dict | None) -> dict:
    """Return explicit security config or the current plaintext lab default."""
    return value or {}


def _paths_or_empty(value: list[dict] | None) -> list[dict]:
    """Preserve server path transports when provided, otherwise use no paths."""
    return value or []


def _scheduler_or_default(value: dict | None) -> dict:
    """Preserve an explicit top-level scheduler policy, otherwise use defaults."""
    return value or {}


class WireGuardHelperConfig(GatherlinkBaseModel):
    """Optional WireGuard helper configuration."""

    enabled: bool = True
    # The default helper shape stays a single WireGuard-over-Gatherlink service.
    # The dual profile is an advanced performance optimization for operators who
    # intentionally split TCP/default and UDP/high-throughput traffic before it
    # enters WireGuard.
    mode: Literal["single", "dual_profile"] = "single"
    service: str | None = None
    stable_service: str | None = None
    fast_service: str | None = None

    @model_validator(mode="after")
    def validate_service_references(self) -> WireGuardHelperConfig:
        """Require the service names needed by the selected WireGuard helper mode."""
        if not self.enabled:
            return self
        if self.mode == "single" and not self.service:
            raise ValueError("wireguard helper service is required in single mode")
        if self.mode == "dual_profile" and (not self.stable_service or not self.fast_service):
            raise ValueError("wireguard helper dual_profile requires stable_service and fast_service")
        if self.stable_service and self.fast_service and self.stable_service == self.fast_service:
            raise ValueError("wireguard helper stable_service and fast_service must be different")
        return self


class DnsHelperUpstreamConfig(GatherlinkBaseModel):
    """One DNS helper upstream endpoint selected by Python policy."""

    name: str
    address: str
    port: int = Field(default=53, ge=1, le=65535)
    kind: DnsUpstreamKind = "direct"
    timeout_seconds: float = Field(default=1.0, ge=0.1)


class DnsHelperConfig(GatherlinkBaseModel):
    """Optional DNS helper configuration."""

    enabled: bool = True
    listen: str = "127.0.0.1:5353"
    strategy: str = "race_first_valid"
    upstreams: list[DnsHelperUpstreamConfig] = Field(default_factory=list)


class Socks5HelperConfig(GatherlinkBaseModel):
    """Optional SOCKS5 helper configuration."""

    enabled: bool = True
    service: str
    listen: str = "127.0.0.1:1080"
    allow_hosts: list[str] = Field(default_factory=list)
    allow_ports: list[int] = Field(default_factory=list)
    connection_timeout_seconds: float = Field(default=10.0, ge=0.1)

    @model_validator(mode="after")
    def validate_allow_lists(self) -> Socks5HelperConfig:
        """Keep SOCKS5 helper config fail-closed by requiring explicit allow-lists."""
        if self.enabled and (not self.allow_hosts or not self.allow_ports):
            raise ValueError("socks5 helper requires allow_hosts and allow_ports when enabled")
        return self


class TcpForwardHelperConfig(GatherlinkBaseModel):
    """Optional one-to-one TCP forwarding helper configuration."""

    enabled: bool = True
    service: str
    listen: str
    target: str
    connect_timeout_seconds: float = Field(default=10.0, ge=0.1)
    idle_timeout_seconds: float = Field(default=300.0, ge=1.0)


class HelpersConfig(GatherlinkBaseModel):
    """Optional helper configuration block."""

    wireguard: WireGuardHelperConfig | None = None
    dns: DnsHelperConfig | None = None
    socks5: Socks5HelperConfig | None = None
    tcp_forward: TcpForwardHelperConfig | None = None


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
    # TODO(config-schema-migration): Add schema_version migration before accepting persisted appliance configs.
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
            "scheduler": FieldTransform(_scheduler_or_default, source="scheduler"),
            "helpers": FieldTransform({}),
        },
        "minimal-server": {
            "schema_version": "schema_version",
            "node": "node",
            "role": "role",
            "peer": FieldTransform(None),
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": FieldTransform(_paths_or_empty, source="paths"),
            "services": "services",
            "scheduler": FieldTransform(_scheduler_or_default, source="scheduler"),
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
            "scheduler": FieldTransform(_scheduler_or_default, source="scheduler"),
            "helpers": "helpers",
        },
        "wireguard-server": {
            "schema_version": "schema_version",
            "node": "node",
            "role": "role",
            "peer": FieldTransform(None),
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": FieldTransform(_paths_or_empty, source="paths"),
            "services": "services",
            "scheduler": FieldTransform(_scheduler_or_default, source="scheduler"),
            "helpers": FieldTransform({}),
        },
        "dns-helper": {
            "schema_version": "schema_version",
            "node": FieldTransform("local"),
            "role": FieldTransform("client"),
            "peer": FieldTransform(None),
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": FieldTransform(_paths_or_empty, source="paths"),
            "services": FieldTransform([]),
            "scheduler": FieldTransform(_scheduler_or_default, source="scheduler"),
            "helpers": "helpers",
        },
        "socks5-helper": {
            "schema_version": "schema_version",
            "node": "node",
            "role": FieldTransform("client"),
            "peer": "peer",
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": "paths",
            "services": "services",
            "scheduler": FieldTransform(_scheduler_or_default, source="scheduler"),
            "helpers": "helpers",
        },
        "tcp-forward-helper": {
            "schema_version": "schema_version",
            "node": "node",
            "role": FieldTransform("client"),
            "peer": "peer",
            "security": FieldTransform(_security_or_default, source="security"),
            "paths": "paths",
            "services": "services",
            "scheduler": FieldTransform(_scheduler_or_default, source="scheduler"),
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

        if self.helpers.wireguard:
            wireguard_services = [
                service
                for service in (
                    self.helpers.wireguard.service,
                    self.helpers.wireguard.stable_service,
                    self.helpers.wireguard.fast_service,
                )
                if service is not None
            ]
            unknown_wireguard_services = sorted(set(wireguard_services) - service_names)
            if unknown_wireguard_services:
                raise ValueError(
                    "wireguard helper services must reference existing services: "
                    + ", ".join(unknown_wireguard_services)
                )
        if self.helpers.socks5 and self.helpers.socks5.service not in service_names:
            raise ValueError("socks5 helper service must reference an existing service")
        if self.helpers.tcp_forward and self.helpers.tcp_forward.service not in service_names:
            raise ValueError("tcp_forward helper service must reference an existing service")
        for session in self.security.sessions:
            unknown_services = sorted(set(session.services) - service_names)
            if unknown_services:
                raise ValueError(
                    "security session services must reference existing services: " + ", ".join(unknown_services)
                )

        return self
