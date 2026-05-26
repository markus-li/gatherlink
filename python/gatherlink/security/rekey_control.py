"""In-band authenticated-session rekey messages owned by Python."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import TopologyBundleBody
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.noise import (
    AcceptedNoiseHandshake,
    PendingNoiseInitiation,
    accept_noise_ik_initiation,
    complete_noise_ik_initiator,
    create_noise_ik_initiation,
)
from gatherlink.security.sessions import AuthenticatedSessionPlan

REKEY_CONTROL_SCHEMA_VERSION = 1
MAX_REKEY_CONTROL_PAYLOAD_BYTES = 8192
RekeyMessageType = Literal["rekey_initiation", "rekey_response", "rekey_reject"]


@dataclass(frozen=True)
class RekeyControlMessage:
    """Operator-safe rekey control payload carried by the reserved auth service."""

    message_type: RekeyMessageType
    sender_node_id: str
    peer_node_id: str
    topology_generation: int
    current_receiver_index: int
    created_at: datetime
    expires_at: datetime | None = None
    noise: dict[str, Any] | None = None
    reason: str | None = None

    def encode(self) -> bytes:
        """Encode this control message as compact deterministic JSON bytes."""
        payload: dict[str, Any] = {
            "schema_version": REKEY_CONTROL_SCHEMA_VERSION,
            "type": self.message_type,
            "sender_node_id": self.sender_node_id,
            "peer_node_id": self.peer_node_id,
            "topology_generation": self.topology_generation,
            "current_receiver_index": self.current_receiver_index,
            "created_at": self.created_at.isoformat(),
        }
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at.isoformat()
        if self.noise is not None:
            payload["noise"] = self.noise
        if self.reason is not None:
            payload["reason"] = self.reason
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REKEY_CONTROL_PAYLOAD_BYTES:
            raise ValueError("rekey control payload is too large")
        return encoded

    @classmethod
    def decode(cls, payload: bytes) -> RekeyControlMessage:
        """Decode and validate one rekey control message."""
        if len(payload) > MAX_REKEY_CONTROL_PAYLOAD_BYTES:
            raise ValueError("rekey control payload is too large")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid rekey control payload") from exc
        if not isinstance(decoded, dict):
            raise ValueError("invalid rekey control payload")
        if int(decoded.get("schema_version", 0)) != REKEY_CONTROL_SCHEMA_VERSION:
            raise ValueError("rekey control schema_version must be 1")
        message_type = str(decoded.get("type", ""))
        if message_type not in {"rekey_initiation", "rekey_response", "rekey_reject"}:
            raise ValueError("unsupported rekey control type")
        noise = decoded.get("noise")
        if noise is not None and not isinstance(noise, dict):
            raise ValueError("rekey control noise value must be an object")
        expires_at = decoded.get("expires_at")
        return cls(
            message_type=message_type,  # type: ignore[arg-type]
            sender_node_id=_required_str(decoded, "sender_node_id"),
            peer_node_id=_required_str(decoded, "peer_node_id"),
            topology_generation=_required_int(decoded, "topology_generation"),
            current_receiver_index=_required_int(decoded, "current_receiver_index"),
            created_at=datetime.fromisoformat(_required_str(decoded, "created_at")),
            expires_at=datetime.fromisoformat(str(expires_at)) if expires_at is not None else None,
            noise=dict(noise) if noise is not None else None,
            reason=str(decoded["reason"]) if "reason" in decoded else None,
        )


@dataclass(frozen=True)
class PendingRekeyInitiation:
    """Local initiator state plus the peer-visible rekey request payload."""

    pending_noise: PendingNoiseInitiation
    message: RekeyControlMessage

    def encode(self) -> bytes:
        """Return the bytes to send through the reserved auth/crypto service."""
        return self.message.encode()


@dataclass(frozen=True)
class AcceptedRekeyResponse:
    """Responder-side accepted rekey session plus the peer-visible response."""

    accepted_noise: AcceptedNoiseHandshake
    message: RekeyControlMessage

    @property
    def session(self) -> AuthenticatedSessionPlan:
        """Return the responder-side replacement session."""
        return self.accepted_noise.session

    def encode(self) -> bytes:
        """Return the bytes to send through the reserved auth/crypto service."""
        return self.message.encode()


def create_rekey_initiation(
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    topology: TopologyBundleBody,
    current_session: AuthenticatedSessionPlan,
    *,
    now: datetime | None = None,
    receiver_index: int | None = None,
) -> PendingRekeyInitiation:
    """
    Create a peer-visible Noise IK rekey initiation for the current session.

    This function intentionally builds only control-plane bytes. Rust still only
    forwards the reserved service payload and later receives compiled AEAD facts
    after Python validates the response and hot-applies the replacement session.
    """
    created_at = now or datetime.now(UTC)
    _validate_current_session(local_identity, peer_identity.node_id, topology, current_session)
    pending = create_noise_ik_initiation(
        local_identity,
        peer_identity,
        topology,
        receiver_index=receiver_index,
        now=created_at,
    )
    return PendingRekeyInitiation(
        pending_noise=pending,
        message=RekeyControlMessage(
            message_type="rekey_initiation",
            sender_node_id=IdentityPublicRecord.from_identity(local_identity).node_id,
            peer_node_id=peer_identity.node_id,
            topology_generation=topology.generation,
            current_receiver_index=_remote_receive_index(current_session),
            created_at=created_at,
            expires_at=pending.expires_at,
            noise=pending.message,
        ),
    )


def accept_rekey_initiation_payload(
    responder_identity: NodeIdentity,
    payload: bytes,
    topology: TopologyBundleBody,
    current_session: AuthenticatedSessionPlan,
    *,
    now: datetime | None = None,
    receiver_index: int | None = None,
) -> AcceptedRekeyResponse:
    """Validate a rekey request and compile the responder replacement session."""
    accepted_at = now or datetime.now(UTC)
    message = RekeyControlMessage.decode(payload)
    if message.message_type != "rekey_initiation":
        raise ValueError("expected rekey initiation")
    responder_public = IdentityPublicRecord.from_identity(responder_identity)
    _validate_rekey_message_for_current_session(message, responder_public.node_id, current_session)
    if message.noise is None:
        raise ValueError("rekey initiation missing Noise payload")
    accepted = accept_noise_ik_initiation(
        responder_identity,
        message.noise,
        topology,
        receiver_index=receiver_index,
        now=accepted_at,
    )
    return AcceptedRekeyResponse(
        accepted_noise=accepted,
        message=RekeyControlMessage(
            message_type="rekey_response",
            sender_node_id=responder_public.node_id,
            peer_node_id=message.sender_node_id,
            topology_generation=topology.generation,
            current_receiver_index=_remote_receive_index(current_session),
            created_at=accepted_at,
            expires_at=accepted.session.expires_at,
            noise=accepted.response,
        ),
    )


def complete_rekey_response_payload(
    local_identity: NodeIdentity,
    pending: PendingRekeyInitiation,
    payload: bytes,
    topology: TopologyBundleBody,
    current_session: AuthenticatedSessionPlan,
    *,
    now: datetime | None = None,
) -> AuthenticatedSessionPlan:
    """Validate a rekey response and compile the initiator replacement session."""
    completed_at = now or datetime.now(UTC)
    message = RekeyControlMessage.decode(payload)
    if message.message_type != "rekey_response":
        raise ValueError("expected rekey response")
    local_public = IdentityPublicRecord.from_identity(local_identity)
    _validate_rekey_message_for_current_session(message, local_public.node_id, current_session)
    if message.noise is None:
        raise ValueError("rekey response missing Noise payload")
    return complete_noise_ik_initiator(
        local_identity,
        pending.pending_noise,
        message.noise,
        topology,
        now=completed_at,
    )


def create_rekey_reject(
    local_identity: NodeIdentity,
    peer_node_id: str,
    current_session: AuthenticatedSessionPlan,
    reason: str,
    *,
    now: datetime | None = None,
) -> RekeyControlMessage:
    """Build a peer-visible rekey rejection without leaking key material."""
    created_at = now or datetime.now(UTC)
    return RekeyControlMessage(
        message_type="rekey_reject",
        sender_node_id=IdentityPublicRecord.from_identity(local_identity).node_id,
        peer_node_id=peer_node_id,
        topology_generation=current_session.topology_generation,
        current_receiver_index=_remote_receive_index(current_session),
        created_at=created_at,
        reason=reason[:255],
    )


def _validate_current_session(
    local_identity: NodeIdentity,
    peer_node_id: str,
    topology: TopologyBundleBody,
    current_session: AuthenticatedSessionPlan,
) -> None:
    local_node_id = IdentityPublicRecord.from_identity(local_identity).node_id
    if current_session.local_node_id != local_node_id:
        raise ValueError("current session local identity disagrees with rekey identity")
    if current_session.peer_node_id != peer_node_id:
        raise ValueError("current session peer identity disagrees with rekey peer")
    if current_session.topology_generation != topology.generation:
        raise ValueError("current session topology generation disagrees with topology")


def _validate_rekey_message_for_current_session(
    message: RekeyControlMessage,
    local_node_id: str,
    current_session: AuthenticatedSessionPlan,
) -> None:
    if message.peer_node_id != local_node_id:
        raise ValueError("rekey message is not addressed to this node")
    if message.sender_node_id != current_session.peer_node_id:
        raise ValueError("rekey message sender disagrees with current peer")
    if message.topology_generation != current_session.topology_generation:
        raise ValueError("rekey message topology generation is stale")
    if message.current_receiver_index != _local_receive_index(current_session):
        raise ValueError("rekey message current receiver index is stale")


def _local_receive_index(session: AuthenticatedSessionPlan) -> int:
    if session.security is None:
        return session.receiver_index
    return int(session.security.local_receiver_index or session.receiver_index)


def _remote_receive_index(session: AuthenticatedSessionPlan) -> int:
    if session.security is None:
        return session.receiver_index
    return int(session.security.remote_receiver_index or session.receiver_index)


def _required_str(decoded: dict[str, Any], field_name: str) -> str:
    value = decoded.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"rekey control field {field_name} must be a non-empty string")
    return value


def _required_int(decoded: dict[str, Any], field_name: str) -> int:
    value = decoded.get(field_name)
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"rekey control field {field_name} must be a non-negative integer")
    return value
