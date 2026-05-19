"""Session key derivation for Python-compiled transport crypto state."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.security.keys import NodeIdentity, x25519_shared_secret

StaticSessionRole = Literal["initiator", "responder"]
STATIC_SESSION_TRANSCRIPT_DOMAIN = b"GATHERLINK_STATIC_SESSION_V1"


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

    def export_config(self) -> dict[str, int | str]:
        """Return a Gatherlink config-compatible static security mapping."""
        return {
            "mode": "static",
            "receiver_index": self.receiver_index,
            "send_key": b64encode(self.send_key).decode("ascii"),
            "receive_key": b64encode(self.receive_key).decode("ascii"),
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
