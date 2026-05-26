"""
Explicit runtime configuration models.

The user-facing config models are intentionally small. These runtime models are
the next boundary: every default, helper reference, and derived value that later
orchestration code needs should be made explicit here before anything starts
services or talks to the Rust dataplane.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_serializer

from gatherlink.config.models import (
    CarrierKind,
    DnsUpstreamKind,
    NodeRole,
    PathSchedulerState,
    SecurityMode,
    SecuritySessionRole,
    ServicePriority,
    ServiceReturnMode,
    ServiceSchedulerPathPolicy,
    ServiceTrafficClass,
)
from gatherlink.shared.models import GatherlinkBaseModel

RuntimeSecurityExecutionMode = Literal["none", "static"]
SchedulerMode = Literal[
    "round_robin",
    "weighted_round_robin",
    "lowest_latency",
    "loss_aware",
    "capacity_aware",
    "least_queue",
    "earliest_completion_first",
    "blocking_estimation",
    "ordered_multipath",
    "balanced",
    "adaptive",
]


class RuntimePathSchedulerConfig(GatherlinkBaseModel):
    """Compiled per-path scheduler state consumed by Rust."""

    path_id: int
    enabled: bool = True
    state: PathSchedulerState = "active"
    weight: int = 1
    mtu: int = 1200
    tx_capacity_bps: int | None = None
    rx_capacity_bps: int | None = None
    latency_us: int | None = None
    loss_ppm: int = Field(default=0, ge=0, le=1_000_000)
    reorder_hold_us: int = Field(default=0, ge=0)
    max_in_flight_packets: int = Field(default=0, ge=0, le=65535)
    max_in_flight_bytes: int = Field(default=0, ge=0)
    pacing_budget_bps: int = Field(default=0, ge=0)
    queue_depth_packets: int = Field(default=0, ge=0)
    queue_depth_bytes: int = Field(default=0, ge=0)
    queue_oldest_age_us: int = Field(default=0, ge=0)


class RuntimePathRelayHopConfig(GatherlinkBaseModel):
    """Compiled outer relay-hop wrapping facts for one path."""

    relay_receiver_index: int
    send_key: bytes

    @field_serializer("send_key", when_used="json")
    def _serialize_relay_key(self, value: bytes) -> str:
        """Redact relay-hop key bytes from operator-facing runtime JSON."""
        return f"[redacted:{len(value)} bytes]"


class RuntimeSchedulerConfig(GatherlinkBaseModel):
    """Compiled scheduler runtime mode and path state."""

    mode: SchedulerMode = "round_robin"
    paths: list[RuntimePathSchedulerConfig] = Field(default_factory=list)


class RuntimePathConfig(GatherlinkBaseModel):
    """A physical path after user config has been expanded for runtime use."""

    name: str
    interface: str
    carrier: CarrierKind = "udp"
    source_ip: str | None = None
    gateway: str | None = None
    transport_bind: str | None = None
    transport_remote: str | None = None
    carrier_max_datagram_size: int | None = None
    scheduler: RuntimePathSchedulerConfig
    relay: RuntimePathRelayHopConfig | None = None


class RuntimeServiceConfig(GatherlinkBaseModel):
    """A UDP service with all runtime-visible endpoints made explicit."""

    service_id: int
    service_id_explicit: bool = False
    name: str
    protocol: Literal["udp"] = "udp"
    target: str
    listen: str | None = None
    priority: ServicePriority = "normal"
    priority_value: int = 100
    traffic_class: ServiceTrafficClass = "unknown"
    return_mode: ServiceReturnMode = "fixed"
    scheduler_fanout: int = 1
    scheduler_fanout_below_bytes: int = 0
    scheduler_flowlet_idle_us: int = 0
    scheduler_flowlet_max_hold_us: int = 0
    scheduler_poll_batch_packets: int = Field(default=0, ge=0)
    scheduler_path_run_datagrams: int = 0
    scheduler_path_policy: ServiceSchedulerPathPolicy = "inherit"
    scheduler_allowed_path_ids: list[int] = Field(default_factory=list)
    scheduler_path_weights: list[tuple[int, int]] = Field(default_factory=list)


class RuntimeSecuritySessionConfig(GatherlinkBaseModel):
    """One runtime static session compiled by Python for Rust execution."""

    name: str | None = None
    local_receiver_index: int
    remote_receiver_index: int
    send_key: bytes
    receive_key: bytes
    service_ids: list[int] = Field(default_factory=list)

    @field_serializer("send_key", "receive_key", when_used="json")
    def _serialize_session_secret_key(self, value: bytes) -> str:
        """Redact per-peer runtime key bytes in operator-facing JSON."""
        return f"[redacted:{len(value)} bytes]"


class RuntimeSecurityConfig(GatherlinkBaseModel):
    """Runtime-visible transport security mode."""

    mode: RuntimeSecurityExecutionMode = "none"
    source_mode: SecurityMode = "none"
    receiver_index: int = 1
    local_receiver_index: int = 1
    remote_receiver_index: int = 1
    send_key: bytes | None = None
    receive_key: bytes | None = None
    local_node_id: str | None = None
    peer_node_id: str | None = None
    topology_generation: int | None = None
    session_role: SecuritySessionRole | None = None
    session_created_at: datetime | None = None
    session_expires_at: datetime | None = None
    rekey_after_packets: int | None = None
    rekey_after_bytes: int | None = None
    sessions: list[RuntimeSecuritySessionConfig] = Field(default_factory=list)

    @field_serializer("send_key", "receive_key", when_used="json")
    def _serialize_secret_key(self, value: bytes | None) -> str | None:
        """
        Redact runtime key bytes in operator-facing JSON.

        The runtime object must keep raw bytes for the Rust bridge, but `config
        show --runtime --json` is introspection, not secret export. Showing the
        byte length is enough to prove static material compiled successfully
        without leaking the key.
        """
        if value is None:
            return None
        return f"[redacted:{len(value)} bytes]"

    @property
    def packet_overhead(self) -> int:
        """Return bytes added outside the encoded Gatherlink frame."""
        return 29 if self.mode == "static" else 0


class RuntimeWireGuardHelperConfig(GatherlinkBaseModel):
    """Runtime-ready WireGuard helper settings outside the core runner."""

    kind: Literal["wireguard"] = "wireguard"
    enabled: bool = True
    profile: Literal["single", "dual_profile"] = "single"
    traffic_class: Literal["single", "stable", "fast"] = "single"
    service: str
    service_target: str
    service_listen: str | None = None


class RuntimeDnsUpstreamConfig(GatherlinkBaseModel):
    """Runtime-ready DNS upstream endpoint for the helper process."""

    name: str
    address: str
    port: int = 53
    kind: DnsUpstreamKind = "direct"
    timeout_seconds: float = 1.0


class RuntimeDnsHelperConfig(GatherlinkBaseModel):
    """Runtime-ready DNS helper settings outside the core runner."""

    kind: Literal["dns"] = "dns"
    enabled: bool = True
    listen: str
    strategy: str
    upstreams: list[RuntimeDnsUpstreamConfig] = Field(default_factory=list)


class RuntimeSocks5HelperConfig(GatherlinkBaseModel):
    """Runtime-ready SOCKS5 helper settings outside the core runner."""

    kind: Literal["socks5"] = "socks5"
    enabled: bool = True
    service: str
    service_target: str
    service_listen: str | None = None
    listen: str
    allow_hosts: list[str] = Field(default_factory=list)
    allow_ports: list[int] = Field(default_factory=list)
    connection_timeout_seconds: float = 10.0


class RuntimeTcpForwardHelperConfig(GatherlinkBaseModel):
    """Runtime-ready one-to-one TCP forwarding helper settings outside the core runner."""

    kind: Literal["tcp_forward"] = "tcp_forward"
    enabled: bool = True
    service: str
    service_target: str
    service_listen: str | None = None
    listen: str
    target: str
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 300.0


RuntimeHelperConfig = (
    RuntimeWireGuardHelperConfig | RuntimeDnsHelperConfig | RuntimeSocks5HelperConfig | RuntimeTcpForwardHelperConfig
)


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
