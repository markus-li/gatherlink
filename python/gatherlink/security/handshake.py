"""Authenticated session exchange compiled by the Python control plane."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from gatherlink.secrets.bundles import SignedDocument, canonical_cbor, sign_document
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import TopologyBundleBody
from gatherlink.security.keys import NodeIdentity, x25519_shared_secret
from gatherlink.security.sessions import (
    DEFAULT_SESSION_LIFETIME_SECONDS,
    AuthenticatedSessionPlan,
    StaticSessionRole,
    StaticTransportSecurityMaterial,
    derive_directional_session_keys,
    generate_receiver_index,
)

HANDSHAKE_INIT_PACKET_TYPE = 0x10
HANDSHAKE_RESPONSE_PACKET_TYPE = 0x11
HANDSHAKE_COOKIE_REPLY_PACKET_TYPE = 0x12
HANDSHAKE_MAC_LEN = 16
HANDSHAKE_COOKIE_NONCE_LEN = 24
HANDSHAKE_COOKIE_CIPHERTEXT_LEN = 32
HANDSHAKE_COOKIE_REPLY_LEN = 1 + HANDSHAKE_MAC_LEN + HANDSHAKE_COOKIE_NONCE_LEN + HANDSHAKE_COOKIE_CIPHERTEXT_LEN
HANDSHAKE_INIT_DOMAIN = "GATHERLINK_HANDSHAKE_INIT_V1"
HANDSHAKE_RESPONSE_DOMAIN = "GATHERLINK_HANDSHAKE_RESPONSE_V1"
HANDSHAKE_SECRET_DOMAIN = b"GATHERLINK_AUTH_HANDSHAKE_SECRET_V1"
HANDSHAKE_TRANSCRIPT_DOMAIN = b"GATHERLINK_AUTH_HANDSHAKE_TRANSCRIPT_V1"
DEFAULT_HANDSHAKE_LIFETIME_SECONDS = 30


@dataclass(frozen=True)
class PendingHandshakeInitiation:
    """Local initiator state kept only until the signed response arrives."""

    document: SignedDocument
    ephemeral_private: bytes
    created_at: datetime
    expires_at: datetime

    def export_secret_dict(self) -> dict[str, Any]:
        """
        Return a JSON-friendly pending-handshake record containing secret state.

        This file is the initiator's temporary equivalent of a handshake state
        machine. It includes the ephemeral private key and must be written with
        owner-only permissions by callers.
        """
        return {
            "schema_version": 1,
            "document": self.document.export_dict(),
            "ephemeral_private": _b64(self.ephemeral_private),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_secret_dict(cls, data: dict[str, Any]) -> PendingHandshakeInitiation:
        """Load a pending initiator handshake state from an owner-only JSON record."""
        if int(data.get("schema_version", 0)) != 1:
            raise ValueError("pending handshake schema_version must be 1")
        return cls(
            document=SignedDocument.from_dict(dict(data["document"])),
            ephemeral_private=b64decode(str(data["ephemeral_private"])),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
        )


@dataclass(frozen=True)
class AcceptedHandshake:
    """Responder output after accepting one authenticated initiation."""

    response: SignedDocument
    session: AuthenticatedSessionPlan


@dataclass(frozen=True)
class HandshakePacket:
    """Version-stable handshake packet shape before full cookie enforcement."""

    packet_type: int
    noise_message: bytes
    mac1: bytes
    mac2: bytes


@dataclass(frozen=True)
class CookieReplyPacket:
    """Fixed cookie-reply packet shape reserved for anti-DoS retry tokens."""

    request_mac1: bytes
    nonce: bytes
    encrypted_cookie: bytes


def encode_handshake_packet(packet_type: int, noise_message: bytes, *, mac1: bytes, mac2: bytes | None = None) -> bytes:
    """
    Encode a v0.9.2-compatible handshake packet with a fixed MAC trailer.

    Full cookie enforcement remains future work, but the wire shape is fixed now
    so a future anti-DoS rollout does not need a migration of the handshake
    packet envelope.
    """
    if packet_type not in {HANDSHAKE_INIT_PACKET_TYPE, HANDSHAKE_RESPONSE_PACKET_TYPE}:
        raise ValueError("handshake packet_type must be 0x10 or 0x11")
    _validate_fixed_bytes("mac1", mac1, HANDSHAKE_MAC_LEN)
    mac2 = mac2 if mac2 is not None else bytes(HANDSHAKE_MAC_LEN)
    _validate_fixed_bytes("mac2", mac2, HANDSHAKE_MAC_LEN)
    return bytes([packet_type]) + noise_message + mac1 + mac2


def decode_handshake_packet(packet: bytes) -> HandshakePacket:
    """Decode a v0.9.2-compatible initiation or response packet shape."""
    if len(packet) < 1 + (HANDSHAKE_MAC_LEN * 2):
        raise ValueError("handshake packet is too short")
    packet_type = packet[0]
    if packet_type not in {HANDSHAKE_INIT_PACKET_TYPE, HANDSHAKE_RESPONSE_PACKET_TYPE}:
        raise ValueError("unsupported handshake packet_type")
    noise_end = len(packet) - (HANDSHAKE_MAC_LEN * 2)
    return HandshakePacket(
        packet_type=packet_type,
        noise_message=packet[1:noise_end],
        mac1=packet[noise_end : noise_end + HANDSHAKE_MAC_LEN],
        mac2=packet[noise_end + HANDSHAKE_MAC_LEN :],
    )


def encode_cookie_reply_packet(request_mac1: bytes, nonce: bytes, encrypted_cookie: bytes) -> bytes:
    """Encode the reserved fixed cookie-reply packet shape."""
    _validate_fixed_bytes("request_mac1", request_mac1, HANDSHAKE_MAC_LEN)
    _validate_fixed_bytes("nonce", nonce, HANDSHAKE_COOKIE_NONCE_LEN)
    _validate_fixed_bytes("encrypted_cookie", encrypted_cookie, HANDSHAKE_COOKIE_CIPHERTEXT_LEN)
    return bytes([HANDSHAKE_COOKIE_REPLY_PACKET_TYPE]) + request_mac1 + nonce + encrypted_cookie


def decode_cookie_reply_packet(packet: bytes) -> CookieReplyPacket:
    """Decode the reserved fixed cookie-reply packet shape."""
    if len(packet) != HANDSHAKE_COOKIE_REPLY_LEN or packet[:1] != bytes([HANDSHAKE_COOKIE_REPLY_PACKET_TYPE]):
        raise ValueError("invalid cookie reply packet")
    return CookieReplyPacket(
        request_mac1=packet[1 : 1 + HANDSHAKE_MAC_LEN],
        nonce=packet[1 + HANDSHAKE_MAC_LEN : 1 + HANDSHAKE_MAC_LEN + HANDSHAKE_COOKIE_NONCE_LEN],
        encrypted_cookie=packet[-HANDSHAKE_COOKIE_CIPHERTEXT_LEN:],
    )


def create_handshake_initiation(
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    topology: TopologyBundleBody,
    *,
    receiver_index: int | None = None,
    now: datetime | None = None,
    lifetime_seconds: int = DEFAULT_HANDSHAKE_LIFETIME_SECONDS,
    capabilities: list[str] | None = None,
) -> PendingHandshakeInitiation:
    """Create a signed IK-style initiation for a topology-authorized peer."""
    created_at = now or datetime.now(UTC)
    expires_at = created_at + timedelta(seconds=lifetime_seconds)
    _validate_handshake_lifetime(lifetime_seconds)
    receiver_index = receiver_index if receiver_index is not None else generate_receiver_index()
    _validate_receiver_index(receiver_index)
    local_public = IdentityPublicRecord.from_identity(local_identity)
    _require_topology_member(topology, local_public)
    _require_topology_member(topology, peer_identity)
    ephemeral_private, ephemeral_public = _generate_x25519_ephemeral()
    body = {
        "schema_version": 1,
        "topology_generation": topology.generation,
        "initiator_node_id": local_public.node_id,
        "responder_node_id": peer_identity.node_id,
        "initiator_receiver_index": receiver_index,
        "initiator_ephemeral_x25519": _b64(ephemeral_public),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "capabilities": capabilities or ["compact-v2", "chacha20poly1305"],
    }
    return PendingHandshakeInitiation(
        document=sign_document(local_identity, HANDSHAKE_INIT_DOMAIN, body),
        ephemeral_private=ephemeral_private,
        created_at=created_at,
        expires_at=expires_at,
    )


def accept_handshake_initiation(
    responder_identity: NodeIdentity,
    initiation: SignedDocument,
    topology: TopologyBundleBody,
    *,
    receiver_index: int | None = None,
    now: datetime | None = None,
    session_lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS,
) -> AcceptedHandshake:
    """Verify a signed initiation and return a signed response plus compiled session."""
    accepted_at = now or datetime.now(UTC)
    receiver_index = receiver_index if receiver_index is not None else generate_receiver_index()
    _validate_receiver_index(receiver_index)
    responder_public = IdentityPublicRecord.from_identity(responder_identity)
    initiation_body = _verified_initiation_body(initiation, topology, responder_public, now=accepted_at)
    initiator_public = _topology_identity_by_node_id(topology, str(initiation_body["initiator_node_id"]))
    responder_ephemeral_private, responder_ephemeral_public = _generate_x25519_ephemeral()
    response_body = {
        "schema_version": 1,
        "topology_generation": topology.generation,
        "initiator_node_id": initiator_public.node_id,
        "responder_node_id": responder_public.node_id,
        "initiation_hash": _b64(_document_body_hash(initiation)),
        "responder_ephemeral_x25519": _b64(responder_ephemeral_public),
        "receiver_index": receiver_index,
        "responder_receiver_index": receiver_index,
        "created_at": accepted_at.isoformat(),
        "expires_at": (accepted_at + timedelta(seconds=session_lifetime_seconds)).isoformat(),
    }
    response = sign_document(responder_identity, HANDSHAKE_RESPONSE_DOMAIN, response_body)
    security = _compile_handshake_security(
        local_identity=responder_identity,
        peer_identity=initiator_public,
        role="responder",
        local_receiver_index=receiver_index,
        remote_receiver_index=int(initiation_body["initiator_receiver_index"]),
        initiator_ephemeral_public=b64decode(str(initiation_body["initiator_ephemeral_x25519"])),
        responder_ephemeral_private=responder_ephemeral_private,
        responder_ephemeral_public=responder_ephemeral_public,
        initiation_body=initiation.body,
        response_body=response.body,
    )
    return AcceptedHandshake(
        response=response,
        session=_session_plan_from_security(
            local_identity=responder_public,
            peer_identity=initiator_public,
            topology=topology,
            role="responder",
            receiver_index=receiver_index,
            created_at=accepted_at,
            lifetime_seconds=session_lifetime_seconds,
            security=security,
        ),
    )


def complete_handshake_initiator(
    local_identity: NodeIdentity,
    pending: PendingHandshakeInitiation,
    response: SignedDocument,
    topology: TopologyBundleBody,
    *,
    now: datetime | None = None,
) -> AuthenticatedSessionPlan:
    """Verify a signed response and compile the initiator-side session."""
    completed_at = now or datetime.now(UTC)
    local_public = IdentityPublicRecord.from_identity(local_identity)
    response_body = _verified_response_body(response, pending, topology, local_public, now=completed_at)
    responder_public = _topology_identity_by_node_id(topology, str(response_body["responder_node_id"]))
    remote_receiver_index = int(response_body["responder_receiver_index"])
    local_receiver_index = int(pending.document.body["initiator_receiver_index"])
    security = _compile_handshake_security(
        local_identity=local_identity,
        peer_identity=responder_public,
        role="initiator",
        local_receiver_index=local_receiver_index,
        remote_receiver_index=remote_receiver_index,
        initiator_ephemeral_private=pending.ephemeral_private,
        initiator_ephemeral_public=b64decode(str(pending.document.body["initiator_ephemeral_x25519"])),
        responder_ephemeral_public=b64decode(str(response_body["responder_ephemeral_x25519"])),
        initiation_body=pending.document.body,
        response_body=response.body,
    )
    expires_at = datetime.fromisoformat(str(response_body["expires_at"]))
    return AuthenticatedSessionPlan(
        local_node_id=local_public.node_id,
        peer_node_id=responder_public.node_id,
        topology_generation=topology.generation,
        receiver_index=remote_receiver_index,
        role="initiator",
        created_at=completed_at,
        expires_at=expires_at,
        security=security,
    )


def _compile_handshake_security(
    *,
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    role: StaticSessionRole,
    local_receiver_index: int,
    remote_receiver_index: int,
    initiation_body: dict,
    response_body: dict,
    initiator_ephemeral_public: bytes,
    responder_ephemeral_public: bytes,
    initiator_ephemeral_private: bytes | None = None,
    responder_ephemeral_private: bytes | None = None,
) -> StaticTransportSecurityMaterial:
    """Compile the signed ephemeral exchange into Rust-ready AEAD facts."""
    _validate_receiver_index(local_receiver_index)
    _validate_receiver_index(remote_receiver_index)
    _peer_node_id, _peer_ed25519, peer_x25519_public = peer_identity.public_bytes()
    if role == "initiator":
        if initiator_ephemeral_private is None:
            raise ValueError("initiator ephemeral private key is required")
        dh1 = x25519_shared_secret(initiator_ephemeral_private, peer_x25519_public)
        dh2 = x25519_shared_secret(local_identity.x25519_private, responder_ephemeral_public)
        send_direction = "initiator_to_responder"
    else:
        if responder_ephemeral_private is None:
            raise ValueError("responder ephemeral private key is required")
        dh1 = x25519_shared_secret(local_identity.x25519_private, initiator_ephemeral_public)
        dh2 = x25519_shared_secret(responder_ephemeral_private, peer_x25519_public)
        send_direction = "responder_to_initiator"
    handshake_secret = sha256(HANDSHAKE_SECRET_DOMAIN + dh1 + dh2).digest()
    keys = derive_directional_session_keys(
        handshake_secret,
        _handshake_transcript_hash(initiation_body=initiation_body, response_body=response_body),
    )
    if send_direction == "initiator_to_responder":
        send_key = keys.initiator_to_responder
        receive_key = keys.responder_to_initiator
    else:
        send_key = keys.responder_to_initiator
        receive_key = keys.initiator_to_responder
    return StaticTransportSecurityMaterial(
        receiver_index=remote_receiver_index,
        local_receiver_index=local_receiver_index,
        remote_receiver_index=remote_receiver_index,
        send_key=send_key,
        receive_key=receive_key,
    )


def _verified_initiation_body(
    document: SignedDocument,
    topology: TopologyBundleBody,
    responder_identity: IdentityPublicRecord,
    *,
    now: datetime,
) -> dict:
    """Return a verified initiation body or raise without network-visible detail."""
    if document.domain != HANDSHAKE_INIT_DOMAIN:
        raise ValueError("packet must be silently dropped")
    document.verify()
    body = document.body
    if int(body.get("schema_version", 0)) != 1:
        raise ValueError("packet must be silently dropped")
    if int(body.get("topology_generation", 0)) != topology.generation:
        raise ValueError("packet must be silently dropped")
    if body.get("responder_node_id") != responder_identity.node_id:
        raise ValueError("packet must be silently dropped")
    initiator = _topology_identity_by_node_id(topology, str(body.get("initiator_node_id", "")))
    _verify_document_signer(document, initiator)
    _require_not_expired(body, now=now)
    return body


def _verified_response_body(
    document: SignedDocument,
    pending: PendingHandshakeInitiation,
    topology: TopologyBundleBody,
    initiator_identity: IdentityPublicRecord,
    *,
    now: datetime,
) -> dict:
    """Return a verified response body or raise without network-visible detail."""
    if document.domain != HANDSHAKE_RESPONSE_DOMAIN:
        raise ValueError("packet must be silently dropped")
    document.verify()
    body = document.body
    if int(body.get("schema_version", 0)) != 1:
        raise ValueError("packet must be silently dropped")
    if int(body.get("topology_generation", 0)) != topology.generation:
        raise ValueError("packet must be silently dropped")
    if body.get("initiator_node_id") != initiator_identity.node_id:
        raise ValueError("packet must be silently dropped")
    if body.get("initiation_hash") != _b64(_document_body_hash(pending.document)):
        raise ValueError("packet must be silently dropped")
    responder = _topology_identity_by_node_id(topology, str(body.get("responder_node_id", "")))
    _verify_document_signer(document, responder)
    _require_not_expired(body, now=now)
    return body


def _session_plan_from_security(
    *,
    local_identity: IdentityPublicRecord,
    peer_identity: IdentityPublicRecord,
    topology: TopologyBundleBody,
    role: StaticSessionRole,
    receiver_index: int,
    created_at: datetime,
    lifetime_seconds: int,
    security: StaticTransportSecurityMaterial,
) -> AuthenticatedSessionPlan:
    """Build the public session plan wrapper around compiled traffic keys."""
    return AuthenticatedSessionPlan(
        local_node_id=local_identity.node_id,
        peer_node_id=peer_identity.node_id,
        topology_generation=topology.generation,
        receiver_index=receiver_index,
        role=role,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=lifetime_seconds),
        security=security,
    )


def _require_topology_member(topology: TopologyBundleBody, identity: IdentityPublicRecord) -> None:
    """Raise when a public identity is not authorized by topology."""
    if identity.node_id in topology.revoked_node_ids:
        raise ValueError("identity is revoked by topology")
    if not any(node.identity.node_id == identity.node_id for node in topology.nodes):
        raise ValueError("identity is not present in topology")


def _topology_identity_by_node_id(topology: TopologyBundleBody, node_id: str) -> IdentityPublicRecord:
    """Return a non-revoked topology identity by node id."""
    if node_id in topology.revoked_node_ids:
        raise ValueError("packet must be silently dropped")
    for node in topology.nodes:
        if node.identity.node_id == node_id:
            return node.identity
    raise ValueError("packet must be silently dropped")


def _verify_document_signer(document: SignedDocument, identity: IdentityPublicRecord) -> None:
    """Ensure signed document metadata matches the expected public identity."""
    node_id, ed25519_public, _x25519_public = identity.public_bytes()
    if document.signer_node_id != node_id or document.signer_public_key != ed25519_public:
        raise ValueError("packet must be silently dropped")


def _require_not_expired(body: dict, *, now: datetime) -> None:
    """Validate a handshake body validity window."""
    expires_at = datetime.fromisoformat(str(body["expires_at"]))
    created_at = datetime.fromisoformat(str(body["created_at"]))
    if now < created_at or now > expires_at:
        raise ValueError("packet must be silently dropped")


def _validate_handshake_lifetime(lifetime_seconds: int) -> None:
    if lifetime_seconds <= 0:
        raise ValueError("handshake lifetime must be positive")


def _validate_receiver_index(receiver_index: int) -> None:
    if receiver_index < 0 or receiver_index >= 2**32:
        raise ValueError("receiver_index must fit u32")


def _validate_fixed_bytes(name: str, value: bytes, expected_len: int) -> None:
    if len(value) != expected_len:
        raise ValueError(f"{name} must be exactly {expected_len} bytes")


def _handshake_transcript_hash(*, initiation_body: dict, response_body: dict) -> bytes:
    return sha256(
        HANDSHAKE_TRANSCRIPT_DOMAIN + canonical_cbor(initiation_body) + canonical_cbor(response_body)
    ).digest()


def _document_body_hash(document: SignedDocument) -> bytes:
    return sha256(canonical_cbor(document.body)).digest()


def _generate_x25519_ephemeral() -> tuple[bytes, bytes]:
    private = X25519PrivateKey.generate()
    return (
        private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


def _b64(value: bytes) -> str:
    return b64encode(value).decode("ascii")
