"""Session key derivation for Python-compiled transport crypto state."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import randbelow
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import TopologyBundleBody
from gatherlink.security.keys import NodeIdentity, x25519_shared_secret

StaticSessionRole = Literal["initiator", "responder"]
STATIC_SESSION_TRANSCRIPT_DOMAIN = b"GATHERLINK_STATIC_SESSION_V1"
AUTHENTICATED_SESSION_TRANSCRIPT_DOMAIN = b"GATHERLINK_AUTH_SESSION_CONTEXT_V1"
DEFAULT_SESSION_LIFETIME_SECONDS = 120
DEFAULT_REKEY_AFTER_PACKETS = 2**48
DEFAULT_REKEY_AFTER_BYTES = 1 << 50
MIN_RECEIVER_INDEX = 1
MAX_RECEIVER_INDEX = 2**32 - 1


@dataclass(frozen=True)
class DirectionalSessionKeys:
    """Directional AEAD keys for one authenticated peer session."""

    initiator_to_responder: bytes
    responder_to_initiator: bytes


@dataclass(frozen=True)
class StaticTransportSecurityMaterial:
    """Runtime security block derived by Python from identity-authenticated peers."""

    receiver_index: int
    send_key: bytes
    receive_key: bytes
    local_receiver_index: int | None = None
    remote_receiver_index: int | None = None

    def __post_init__(self) -> None:
        """Use one shared receiver index unless a handshake supplies distinct indexes."""
        if self.local_receiver_index is None:
            object.__setattr__(self, "local_receiver_index", self.receiver_index)
        if self.remote_receiver_index is None:
            object.__setattr__(self, "remote_receiver_index", self.receiver_index)

    def export_config(self, *, mode: Literal["static", "authenticated"] = "static") -> dict[str, int | str]:
        """Return a Gatherlink config-compatible transport security mapping."""
        return {
            "mode": mode,
            "receiver_index": self.receiver_index,
            "local_receiver_index": self.local_receiver_index,
            "remote_receiver_index": self.remote_receiver_index,
            "send_key": b64encode(self.send_key).decode("ascii"),
            "receive_key": b64encode(self.receive_key).decode("ascii"),
        }


@dataclass(frozen=True)
class AuthenticatedSessionPlan:
    """
    Python-owned authenticated session plan compiled from verified topology.

    This is the control-plane state that a Noise IK implementation will consume
    and later replace with live handshake outputs. For the current static AEAD
    bridge, it still derives deterministic directional keys so Rust can execute
    the existing encrypted dataplane without learning topology policy.
    """

    local_node_id: str
    peer_node_id: str
    topology_generation: int
    receiver_index: int
    role: StaticSessionRole
    created_at: datetime
    expires_at: datetime
    rekey_after_packets: int = DEFAULT_REKEY_AFTER_PACKETS
    rekey_after_bytes: int = DEFAULT_REKEY_AFTER_BYTES
    security: StaticTransportSecurityMaterial | None = None

    def needs_rekey(self, *, now: datetime, packets: int = 0, bytes_transferred: int = 0) -> bool:
        """Return whether time or volume says this session should be replaced."""
        return (
            now >= self.expires_at or packets >= self.rekey_after_packets or bytes_transferred >= self.rekey_after_bytes
        )

    def export_public_summary(self) -> dict[str, int | str | bool]:
        """Return operator-safe session facts without traffic keys."""
        return {
            "local_node_id": self.local_node_id,
            "peer_node_id": self.peer_node_id,
            "topology_generation": self.topology_generation,
            "receiver_index": self.receiver_index,
            "role": self.role,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "rekey_after_packets": self.rekey_after_packets,
            "rekey_after_bytes": self.rekey_after_bytes,
            "has_compiled_security": self.security is not None,
        }


def derive_directional_session_keys(shared_secret: bytes, transcript_hash: bytes) -> DirectionalSessionKeys:
    """Derive two 32-byte traffic keys from an authenticated handshake secret."""
    if len(shared_secret) != 32:
        raise ValueError("shared_secret must be 32 bytes")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=transcript_hash,
        info=b"GATHERLINK_TRANSPORT_KEYS_V1",
    ).derive(shared_secret)
    return DirectionalSessionKeys(
        initiator_to_responder=derived[:32],
        responder_to_initiator=derived[32:],
    )


def generate_receiver_index() -> int:
    """Return an opaque non-zero receiver index for an authenticated receive session."""
    return randbelow(MAX_RECEIVER_INDEX) + MIN_RECEIVER_INDEX


def derive_static_transport_security(
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    *,
    role: StaticSessionRole,
    receiver_index: int,
    context: bytes = b"",
) -> StaticTransportSecurityMaterial:
    """
    Derive static lab/manual traffic keys from authenticated identity material.

    This is not the final Noise handshake. It is Python-owned provisioning glue
    that lets labs and manual configs use the production AEAD packet path without
    hand-copying raw symmetric keys. The transcript is deterministic and ordered
    by role so both peers derive inverse send/receive keys.
    """
    if receiver_index < 0 or receiver_index >= 2**32:
        raise ValueError("receiver_index must fit u32")
    peer_node_id, _peer_ed25519_public, peer_x25519_public = peer_identity.public_bytes()
    if peer_node_id == local_identity.node_id:
        raise ValueError("peer identity must not be the local identity")

    if role == "initiator":
        initiator_node_id = local_identity.node_id
        responder_node_id = peer_node_id
        initiator_x25519_public = local_identity.x25519_public
        responder_x25519_public = peer_x25519_public
        send_direction = "initiator_to_responder"
    elif role == "responder":
        initiator_node_id = peer_node_id
        responder_node_id = local_identity.node_id
        initiator_x25519_public = peer_x25519_public
        responder_x25519_public = local_identity.x25519_public
        send_direction = "responder_to_initiator"
    else:
        raise ValueError("role must be initiator or responder")

    transcript_hash = static_session_transcript_hash(
        initiator_node_id=initiator_node_id,
        responder_node_id=responder_node_id,
        initiator_x25519_public=initiator_x25519_public,
        responder_x25519_public=responder_x25519_public,
        context=context,
    )
    shared_secret = x25519_shared_secret(local_identity.x25519_private, peer_x25519_public)
    keys = derive_directional_session_keys(shared_secret, transcript_hash)
    if send_direction == "initiator_to_responder":
        send_key = keys.initiator_to_responder
        receive_key = keys.responder_to_initiator
    else:
        send_key = keys.responder_to_initiator
        receive_key = keys.initiator_to_responder
    return StaticTransportSecurityMaterial(
        receiver_index=receiver_index,
        send_key=send_key,
        receive_key=receive_key,
    )


def plan_authenticated_static_session(
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    topology: TopologyBundleBody,
    *,
    role: StaticSessionRole,
    receiver_index: int,
    now: datetime | None = None,
    lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS,
) -> AuthenticatedSessionPlan:
    """
    Compile a short-lived authenticated session plan from verified topology.

    The returned security block uses the existing static AEAD bridge, but the
    transcript is bound to the signed topology generation and peer identities.
    This keeps the current encrypted lab path useful while preserving the future
    boundary: replacing static derivation with Noise should not affect Rust's
    packet execution contract.
    """
    created_at = now or datetime.now(UTC)
    if lifetime_seconds <= 0:
        raise ValueError("session lifetime must be positive")
    local_public = IdentityPublicRecord.from_identity(local_identity)
    _require_topology_member(topology, local_public)
    _require_topology_member(topology, peer_identity)
    if role == "initiator":
        initiator_node_id = local_public.node_id
        responder_node_id = peer_identity.node_id
    else:
        initiator_node_id = peer_identity.node_id
        responder_node_id = local_public.node_id
    context = authenticated_session_context(
        topology=topology,
        initiator_node_id=initiator_node_id,
        responder_node_id=responder_node_id,
    )
    security = derive_static_transport_security(
        local_identity,
        peer_identity,
        role=role,
        receiver_index=receiver_index,
        context=context,
    )
    return AuthenticatedSessionPlan(
        local_node_id=local_public.node_id,
        peer_node_id=peer_identity.node_id,
        topology_generation=topology.generation,
        receiver_index=receiver_index,
        role=role,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=lifetime_seconds),
        security=security,
    )


def authenticated_session_context(
    *,
    topology: TopologyBundleBody,
    initiator_node_id: str,
    responder_node_id: str,
) -> bytes:
    """Return the topology-bound transcript context for session setup."""
    return sha256(
        AUTHENTICATED_SESSION_TRANSCRIPT_DOMAIN
        + str(topology.generation).encode("ascii")
        + topology.issuer_node_id.encode("ascii")
        + initiator_node_id.encode("ascii")
        + responder_node_id.encode("ascii")
    ).digest()


def static_session_transcript_hash(
    *,
    initiator_node_id: bytes,
    responder_node_id: bytes,
    initiator_x25519_public: bytes,
    responder_x25519_public: bytes,
    context: bytes = b"",
) -> bytes:
    """Return the deterministic transcript hash for static AEAD lab sessions."""
    for field_name, value in {
        "initiator_node_id": initiator_node_id,
        "responder_node_id": responder_node_id,
        "initiator_x25519_public": initiator_x25519_public,
        "responder_x25519_public": responder_x25519_public,
    }.items():
        if len(value) != 32:
            raise ValueError(f"{field_name} must be 32 bytes")
    return sha256(
        STATIC_SESSION_TRANSCRIPT_DOMAIN
        + initiator_node_id
        + responder_node_id
        + initiator_x25519_public
        + responder_x25519_public
        + context
    ).digest()


def _require_topology_member(topology: TopologyBundleBody, identity: IdentityPublicRecord) -> None:
    """Raise when a public identity is not authorized by the verified topology."""
    if identity.node_id in topology.revoked_node_ids:
        raise ValueError("identity is revoked by topology")
    if not any(node.identity.node_id == identity.node_id for node in topology.nodes):
        raise ValueError("identity is not present in topology")
