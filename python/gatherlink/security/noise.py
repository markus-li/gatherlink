"""Noise IK style authenticated session setup owned by the Python control plane."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import TopologyBundleBody
from gatherlink.security.keys import NodeIdentity, x25519_shared_secret
from gatherlink.security.sessions import (
    DEFAULT_SESSION_LIFETIME_SECONDS,
    AuthenticatedSessionPlan,
    StaticTransportSecurityMaterial,
    generate_receiver_index,
)

NOISE_IK_PROTOCOL_NAME = b"Noise_IK_25519_ChaChaPoly_SHA256_GatherlinkV1"
NOISE_SESSION_KEY_INFO = b"GATHERLINK_NOISE_IK_TRANSPORT_KEYS_V1"
DEFAULT_NOISE_HANDSHAKE_LIFETIME_SECONDS = 30
NOISE_NONCE_ZERO = b"\x00" * 12


@dataclass(frozen=True)
class PendingNoiseInitiation:
    """Local initiator state kept until a Noise IK response arrives."""

    message: dict[str, Any]
    ephemeral_private: bytes
    created_at: datetime
    expires_at: datetime

    def export_secret_dict(self) -> dict[str, Any]:
        """Return owner-only pending Noise state for CLI/file-copy workflows."""
        return {
            "schema_version": 1,
            "protocol": "noise-ik",
            "message": self.message,
            "ephemeral_private": _b64(self.ephemeral_private),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_secret_dict(cls, data: dict[str, Any]) -> PendingNoiseInitiation:
        """Load pending initiator state from an owner-only secret record."""
        if int(data.get("schema_version", 0)) != 1:
            raise ValueError("pending noise schema_version must be 1")
        if data.get("protocol") != "noise-ik":
            raise ValueError("pending noise protocol must be noise-ik")
        return cls(
            message=dict(data["message"]),
            ephemeral_private=b64decode(str(data["ephemeral_private"])),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
        )


@dataclass(frozen=True)
class AcceptedNoiseHandshake:
    """Responder output after accepting a Noise IK initiation."""

    response: dict[str, Any]
    session: AuthenticatedSessionPlan


def create_noise_ik_initiation(
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    topology: TopologyBundleBody,
    *,
    receiver_index: int | None = None,
    now: datetime | None = None,
    lifetime_seconds: int = DEFAULT_NOISE_HANDSHAKE_LIFETIME_SECONDS,
) -> PendingNoiseInitiation:
    """
    Create the first Noise IK-style message for a topology-authorized peer.

    The responder static X25519 public key is the IK pre-message. The initiator
    static X25519 public key is encrypted after the `es` DH, so unauthenticated
    observers do not learn the endpoint identity from the public UDP packet.
    """
    created_at = now or datetime.now(UTC)
    expires_at = created_at + timedelta(seconds=lifetime_seconds)
    _validate_lifetime(lifetime_seconds, "handshake lifetime")
    receiver_index = receiver_index if receiver_index is not None else generate_receiver_index()
    _validate_receiver_index(receiver_index)
    local_public = IdentityPublicRecord.from_identity(local_identity)
    _require_topology_member(topology, local_public)
    _require_topology_member(topology, peer_identity)
    _peer_node_id, _peer_ed25519, peer_static_public = peer_identity.public_bytes()

    ephemeral_private, ephemeral_public = _generate_x25519_ephemeral()
    state = _NoiseState.for_pair(
        topology, initiator_node_id=local_public.node_id, responder_node_id=peer_identity.node_id
    )
    state.mix_hash(peer_static_public)
    state.mix_hash(ephemeral_public)
    k = state.mix_key(x25519_shared_secret(ephemeral_private, peer_static_public))
    encrypted_static = state.encrypt_and_hash(k, local_identity.x25519_public)
    state.mix_key(x25519_shared_secret(local_identity.x25519_private, peer_static_public))

    message = {
        "schema_version": 1,
        "protocol": "noise-ik",
        "topology_generation": topology.generation,
        "initiator_node_id": local_public.node_id,
        "responder_node_id": peer_identity.node_id,
        "initiator_receiver_index": receiver_index,
        "initiator_ephemeral_x25519": _b64(ephemeral_public),
        "encrypted_initiator_static_x25519": _b64(encrypted_static),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    return PendingNoiseInitiation(
        message=message,
        ephemeral_private=ephemeral_private,
        created_at=created_at,
        expires_at=expires_at,
    )


def accept_noise_ik_initiation(
    responder_identity: NodeIdentity,
    initiation: dict[str, Any],
    topology: TopologyBundleBody,
    *,
    receiver_index: int | None = None,
    now: datetime | None = None,
    session_lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS,
) -> AcceptedNoiseHandshake:
    """Verify a Noise IK initiation and compile responder-side AEAD facts."""
    accepted_at = now or datetime.now(UTC)
    receiver_index = receiver_index if receiver_index is not None else generate_receiver_index()
    _validate_receiver_index(receiver_index)
    _validate_lifetime(session_lifetime_seconds, "session lifetime")
    responder_public = IdentityPublicRecord.from_identity(responder_identity)
    _verified_noise_initiation_header(initiation, topology, responder_public, now=accepted_at)
    initiator_ephemeral_public = b64decode(str(initiation["initiator_ephemeral_x25519"]))
    if len(initiator_ephemeral_public) != 32:
        raise ValueError("packet must be silently dropped")

    state = _NoiseState.for_pair(
        topology,
        initiator_node_id=str(initiation["initiator_node_id"]),
        responder_node_id=responder_public.node_id,
    )
    state.mix_hash(responder_identity.x25519_public)
    state.mix_hash(initiator_ephemeral_public)
    k = state.mix_key(x25519_shared_secret(responder_identity.x25519_private, initiator_ephemeral_public))
    initiator_static_public = state.decrypt_and_hash(k, b64decode(str(initiation["encrypted_initiator_static_x25519"])))
    initiator_public = _topology_identity_by_x25519(topology, initiator_static_public)
    if initiator_public.node_id != initiation["initiator_node_id"]:
        raise ValueError("packet must be silently dropped")
    state.mix_key(x25519_shared_secret(responder_identity.x25519_private, initiator_static_public))

    responder_ephemeral_private, responder_ephemeral_public = _generate_x25519_ephemeral()
    state.mix_hash(responder_ephemeral_public)
    state.mix_key(x25519_shared_secret(responder_ephemeral_private, initiator_ephemeral_public))
    state.mix_key(x25519_shared_secret(responder_identity.x25519_private, initiator_ephemeral_public))
    receive_key, send_key = _derive_transport_keys(state)
    created_at = accepted_at
    expires_at = created_at + timedelta(seconds=session_lifetime_seconds)
    response = {
        "schema_version": 1,
        "protocol": "noise-ik",
        "topology_generation": topology.generation,
        "initiator_node_id": initiator_public.node_id,
        "responder_node_id": responder_public.node_id,
        "initiation_hash": _b64(_message_hash(initiation)),
        "responder_ephemeral_x25519": _b64(responder_ephemeral_public),
        "receiver_index": receiver_index,
        "responder_receiver_index": receiver_index,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    security = StaticTransportSecurityMaterial(
        receiver_index=receiver_index,
        local_receiver_index=receiver_index,
        remote_receiver_index=int(initiation["initiator_receiver_index"]),
        send_key=send_key,
        receive_key=receive_key,
    )
    return AcceptedNoiseHandshake(
        response=response,
        session=AuthenticatedSessionPlan(
            local_node_id=responder_public.node_id,
            peer_node_id=initiator_public.node_id,
            topology_generation=topology.generation,
            receiver_index=receiver_index,
            role="responder",
            created_at=created_at,
            expires_at=expires_at,
            security=security,
        ),
    )


def complete_noise_ik_initiator(
    local_identity: NodeIdentity,
    pending: PendingNoiseInitiation,
    response: dict[str, Any],
    topology: TopologyBundleBody,
    *,
    now: datetime | None = None,
) -> AuthenticatedSessionPlan:
    """Verify a Noise IK response and compile initiator-side AEAD facts."""
    completed_at = now or datetime.now(UTC)
    local_public = IdentityPublicRecord.from_identity(local_identity)
    _verified_noise_response(response, pending, topology, local_public, now=completed_at)
    responder_public = _topology_identity_by_node_id(topology, str(response["responder_node_id"]))
    initiator_ephemeral_public = b64decode(str(pending.message["initiator_ephemeral_x25519"]))
    responder_ephemeral_public = b64decode(str(response["responder_ephemeral_x25519"]))
    if len(responder_ephemeral_public) != 32:
        raise ValueError("packet must be silently dropped")

    state = _NoiseState.for_pair(
        topology,
        initiator_node_id=local_public.node_id,
        responder_node_id=responder_public.node_id,
    )
    _node_id, _ed25519_public, responder_static_public = responder_public.public_bytes()
    state.mix_hash(responder_static_public)
    state.mix_hash(initiator_ephemeral_public)
    k = state.mix_key(x25519_shared_secret(pending.ephemeral_private, responder_static_public))
    encrypted_static = state.encrypt_and_hash(k, local_identity.x25519_public)
    if encrypted_static != b64decode(str(pending.message["encrypted_initiator_static_x25519"])):
        raise ValueError("packet must be silently dropped")
    state.mix_key(x25519_shared_secret(local_identity.x25519_private, responder_static_public))
    state.mix_hash(responder_ephemeral_public)
    state.mix_key(x25519_shared_secret(pending.ephemeral_private, responder_ephemeral_public))
    state.mix_key(x25519_shared_secret(pending.ephemeral_private, responder_static_public))
    send_key, receive_key = _derive_transport_keys(state)
    remote_receiver_index = int(response["responder_receiver_index"])
    local_receiver_index = int(pending.message["initiator_receiver_index"])
    security = StaticTransportSecurityMaterial(
        receiver_index=remote_receiver_index,
        local_receiver_index=local_receiver_index,
        remote_receiver_index=remote_receiver_index,
        send_key=send_key,
        receive_key=receive_key,
    )
    return AuthenticatedSessionPlan(
        local_node_id=local_public.node_id,
        peer_node_id=responder_public.node_id,
        topology_generation=topology.generation,
        receiver_index=remote_receiver_index,
        role="initiator",
        created_at=completed_at,
        expires_at=datetime.fromisoformat(str(response["expires_at"])),
        security=security,
    )


@dataclass
class _NoiseState:
    ck: bytes
    h: bytes

    @classmethod
    def for_pair(cls, topology: TopologyBundleBody, *, initiator_node_id: str, responder_node_id: str) -> _NoiseState:
        """Initialize chaining/transcript state from the authenticated topology context."""
        h = sha256(NOISE_IK_PROTOCOL_NAME).digest()
        prologue = (
            b"GATHERLINK_NOISE_IK_PROLOGUE_V1"
            + str(topology.generation).encode("ascii")
            + topology.issuer_node_id.encode("ascii")
            + initiator_node_id.encode("ascii")
            + responder_node_id.encode("ascii")
        )
        state = cls(ck=h, h=h)
        state.mix_hash(prologue)
        return state

    def mix_hash(self, value: bytes) -> None:
        self.h = sha256(self.h + value).digest()

    def mix_key(self, input_key_material: bytes) -> bytes:
        output = HKDF(algorithm=hashes.SHA256(), length=64, salt=self.ck, info=b"noise-mix-key").derive(
            input_key_material
        )
        self.ck = output[:32]
        return output[32:]

    def encrypt_and_hash(self, key: bytes, plaintext: bytes) -> bytes:
        ciphertext = ChaCha20Poly1305(key).encrypt(NOISE_NONCE_ZERO, plaintext, self.h)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, key: bytes, ciphertext: bytes) -> bytes:
        try:
            plaintext = ChaCha20Poly1305(key).decrypt(NOISE_NONCE_ZERO, ciphertext, self.h)
        except Exception as exc:
            raise ValueError("packet must be silently dropped") from exc
        self.mix_hash(ciphertext)
        return plaintext


def _derive_transport_keys(state: _NoiseState) -> tuple[bytes, bytes]:
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=state.ck,
        info=NOISE_SESSION_KEY_INFO + state.h,
    ).derive(b"")
    return material[:32], material[32:]


def _verified_noise_initiation_header(
    message: dict[str, Any],
    topology: TopologyBundleBody,
    responder_identity: IdentityPublicRecord,
    *,
    now: datetime,
) -> None:
    if message.get("protocol") != "noise-ik" or int(message.get("schema_version", 0)) != 1:
        raise ValueError("packet must be silently dropped")
    if int(message.get("topology_generation", 0)) != topology.generation:
        raise ValueError("packet must be silently dropped")
    if message.get("responder_node_id") != responder_identity.node_id:
        raise ValueError("packet must be silently dropped")
    _validate_receiver_index(int(message.get("initiator_receiver_index", -1)))
    _require_not_expired(message, now=now)
    _topology_identity_by_node_id(topology, str(message.get("initiator_node_id", "")))


def _verified_noise_response(
    message: dict[str, Any],
    pending: PendingNoiseInitiation,
    topology: TopologyBundleBody,
    initiator_identity: IdentityPublicRecord,
    *,
    now: datetime,
) -> None:
    if message.get("protocol") != "noise-ik" or int(message.get("schema_version", 0)) != 1:
        raise ValueError("packet must be silently dropped")
    if int(message.get("topology_generation", 0)) != topology.generation:
        raise ValueError("packet must be silently dropped")
    if message.get("initiator_node_id") != initiator_identity.node_id:
        raise ValueError("packet must be silently dropped")
    if message.get("initiation_hash") != _b64(_message_hash(pending.message)):
        raise ValueError("packet must be silently dropped")
    _validate_receiver_index(int(message.get("responder_receiver_index", -1)))
    _require_not_expired(message, now=now)
    _topology_identity_by_node_id(topology, str(message.get("responder_node_id", "")))


def _require_topology_member(topology: TopologyBundleBody, identity: IdentityPublicRecord) -> None:
    if identity.node_id in topology.revoked_node_ids:
        raise ValueError("identity is revoked by topology")
    if not any(node.identity.node_id == identity.node_id for node in topology.nodes):
        raise ValueError("identity is not present in topology")


def _topology_identity_by_node_id(topology: TopologyBundleBody, node_id: str) -> IdentityPublicRecord:
    if node_id in topology.revoked_node_ids:
        raise ValueError("packet must be silently dropped")
    for node in topology.nodes:
        if node.identity.node_id == node_id:
            return node.identity
    raise ValueError("packet must be silently dropped")


def _topology_identity_by_x25519(topology: TopologyBundleBody, x25519_public: bytes) -> IdentityPublicRecord:
    for node in topology.nodes:
        if node.identity.node_id in topology.revoked_node_ids:
            continue
        _node_id, _ed_public, candidate_x25519 = node.identity.public_bytes()
        if candidate_x25519 == x25519_public:
            return node.identity
    raise ValueError("packet must be silently dropped")


def _require_not_expired(body: dict[str, Any], *, now: datetime) -> None:
    created_at = datetime.fromisoformat(str(body["created_at"]))
    expires_at = datetime.fromisoformat(str(body["expires_at"]))
    if now < created_at or now > expires_at:
        raise ValueError("packet must be silently dropped")


def _validate_lifetime(lifetime_seconds: int, field_name: str) -> None:
    if lifetime_seconds <= 0:
        raise ValueError(f"{field_name} must be positive")


def _validate_receiver_index(receiver_index: int) -> None:
    if receiver_index < 0 or receiver_index >= 2**32:
        raise ValueError("receiver_index must fit u32")


def _message_hash(message: dict[str, Any]) -> bytes:
    return sha256(json_canonical_bytes(message)).digest()


def json_canonical_bytes(message: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes for public Noise message hashes."""
    import json

    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _generate_x25519_ephemeral() -> tuple[bytes, bytes]:
    private = X25519PrivateKey.generate()
    return (
        private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


def _b64(value: bytes) -> str:
    return b64encode(value).decode("ascii")
