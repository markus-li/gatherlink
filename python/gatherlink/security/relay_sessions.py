"""Python-owned secure relay session authorization models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import TopologyBundleBody
from gatherlink.shared.models import GatherlinkBaseModel

RelayDirection = Literal["upstream_to_downstream", "downstream_to_upstream"]
RelayPacketType = Literal["encrypted_data"]
DEFAULT_RELAY_SESSION_LIFETIME_SECONDS = 120


class RelayNextHop(GatherlinkBaseModel):
    """Explicit relay next-hop session target selected by Python policy."""

    peer_node_id: str
    endpoint: str
    receiver_index: int = Field(ge=0, le=2**32 - 1)


class RelaySessionAuthorization(GatherlinkBaseModel):
    """
    Explicit secure relay-hop authorization compiled from topology/control state.

    This object contains no endpoint service id, path id, route label, or
    plaintext routing name. Rust may later receive a compact DTO derived from
    this state, but all semantic authorization stays in Python.
    """

    relay_receiver_index: int = Field(ge=0, le=2**32 - 1)
    upstream_peer_node_id: str
    relay_node_id: str
    next_hop: RelayNextHop
    direction: RelayDirection
    topology_generation: int = Field(ge=1)
    expires_at: datetime
    allowed_packet_type: RelayPacketType = "encrypted_data"
    max_packet_size: int | None = Field(default=None, ge=1)
    max_packets: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_distinct_hops(self) -> RelaySessionAuthorization:
        """Reject self-looping relay state before it reaches runtime."""
        if self.upstream_peer_node_id == self.relay_node_id:
            raise ValueError("relay upstream peer must differ from relay node")
        if self.next_hop.peer_node_id == self.relay_node_id:
            raise ValueError("relay next hop must differ from relay node")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return whether this relay authorization is expired."""
        return (now or datetime.now(UTC)) >= self.expires_at

    def allows_packet(self, *, packet_size: int, now: datetime | None = None) -> bool:
        """Check cheap packet-level authorization limits."""
        if self.is_expired(now):
            return False
        return self.max_packet_size is None or packet_size <= self.max_packet_size

    def export_executor_config(self) -> dict[str, int | str | None]:
        """
        Return compact relay facts suitable for the Rust executor.

        Python has already checked topology, roles, identity membership, expiry,
        and next-hop policy. The exported shape intentionally contains no
        plaintext service id, path id, route label, endpoint payload meaning, or
        topology object.
        """
        return RelayExecutorConfig.from_authorization(self).export_dict()


class RelayExecutorConfig(GatherlinkBaseModel):
    """
    Socket-ready relay facts compiled by Python for the Rust relay executor.

    This is still a Python-owned policy artifact: it is derived only after
    topology, role, revocation, expiry, and next-hop authorization checks pass.
    Rust should receive this as compact execution data, not as permission to
    interpret service ids, path ids, endpoint labels, or topology.
    """

    relay_receiver_index: int = Field(ge=0, le=2**32 - 1)
    next_hop_transport: Literal["udp"] = "udp"
    next_hop_address: str
    next_hop_receiver_index: int = Field(ge=0, le=2**32 - 1)
    direction: RelayDirection
    topology_generation: int = Field(ge=1)
    expires_at_unix_us: int = Field(ge=0)
    max_packet_size: int | None = Field(default=None, ge=1)
    max_packets: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)

    @classmethod
    def from_authorization(cls, authorization: RelaySessionAuthorization) -> RelayExecutorConfig:
        """Compile one validated authorization into socket-ready executor facts."""
        transport, address = _parse_next_hop_endpoint(authorization.next_hop.endpoint)
        return cls(
            relay_receiver_index=authorization.relay_receiver_index,
            next_hop_transport=transport,
            next_hop_address=address,
            next_hop_receiver_index=authorization.next_hop.receiver_index,
            direction=authorization.direction,
            topology_generation=authorization.topology_generation,
            expires_at_unix_us=int(authorization.expires_at.timestamp() * 1_000_000),
            max_packet_size=authorization.max_packet_size,
            max_packets=authorization.max_packets,
            max_bytes=authorization.max_bytes,
        )


def compile_relay_executor_configs(
    authorizations: list[RelaySessionAuthorization],
    *,
    now: datetime | None = None,
) -> list[RelayExecutorConfig]:
    """
    Compile non-expired relay authorizations for the Rust executor.

    Expired relay sessions fail closed by being omitted from the compiled set;
    callers should emit structured diagnostics for omitted records if they are
    supervising a long-running relay service.
    """
    current = now or datetime.now(UTC)
    return [RelayExecutorConfig.from_authorization(item) for item in authorizations if not item.is_expired(current)]


def authorize_relay_session(
    *,
    topology: TopologyBundleBody,
    upstream_peer: IdentityPublicRecord,
    relay: IdentityPublicRecord,
    next_hop: RelayNextHop,
    direction: RelayDirection,
    relay_receiver_index: int,
    now: datetime | None = None,
    lifetime_seconds: int = DEFAULT_RELAY_SESSION_LIFETIME_SECONDS,
    max_packet_size: int | None = None,
) -> RelaySessionAuthorization:
    """Build relay-hop authorization after checking signed topology membership and role."""
    created_at = now or datetime.now(UTC)
    if lifetime_seconds <= 0:
        raise ValueError("relay session lifetime must be positive")
    _require_node_role(topology, relay.node_id, "relay")
    _require_node(topology, upstream_peer.node_id)
    _require_node(topology, next_hop.peer_node_id)
    return RelaySessionAuthorization(
        relay_receiver_index=relay_receiver_index,
        upstream_peer_node_id=upstream_peer.node_id,
        relay_node_id=relay.node_id,
        next_hop=next_hop,
        direction=direction,
        topology_generation=topology.generation,
        expires_at=created_at + timedelta(seconds=lifetime_seconds),
        max_packet_size=max_packet_size,
    )


def _require_node(topology: TopologyBundleBody, node_id: str) -> None:
    if node_id in topology.revoked_node_ids:
        raise ValueError("relay session references a revoked node")
    if not any(node.identity.node_id == node_id for node in topology.nodes):
        raise ValueError("relay session references a node outside topology")


def _require_node_role(topology: TopologyBundleBody, node_id: str, role: str) -> None:
    _require_node(topology, node_id)
    for node in topology.nodes:
        if node.identity.node_id == node_id and role in node.roles:
            return
    raise ValueError(f"node is not authorized for {role} role")


def _parse_next_hop_endpoint(endpoint: str) -> tuple[Literal["udp"], str]:
    """Return transport and socket address from an explicit relay next-hop endpoint."""
    transport, separator, address = endpoint.partition(":")
    if not separator or transport != "udp" or not address:
        raise ValueError("relay next-hop endpoint must use udp:host:port or udp:[ipv6]:port")
    return "udp", address
