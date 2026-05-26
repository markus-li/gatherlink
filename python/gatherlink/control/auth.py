"""Reserved auth/crypto service handling owned by the Python control plane."""

from __future__ import annotations

from dataclasses import dataclass

from gatherlink.control.reserved import ReservedServicePayload
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.security.rekey_control import RekeyControlMessage


@dataclass(frozen=True)
class AuthCryptoHandleResult:
    """Decoded reserved auth/crypto fact for Python-owned session orchestration."""

    accepted: bool
    message: RekeyControlMessage | None = None
    reason: str | None = None


def handle_auth_crypto_event(
    event: ReservedServicePayload,
    *,
    diagnostics: DiagnosticsBus | None = None,
    peer_name: str | None = None,
) -> bool:
    """
    Decode one reserved auth/crypto payload forwarded by Rust.

    Rust only routes the reserved service id and payload bytes to Python. This
    handler gives in-band rekey control messages a production runner landing
    zone without moving Noise, topology, or receiver-index policy into Rust.
    """
    return decode_auth_crypto_event(event, diagnostics=diagnostics, peer_name=peer_name).accepted


def decode_auth_crypto_event(
    event: ReservedServicePayload,
    *,
    diagnostics: DiagnosticsBus | None = None,
    peer_name: str | None = None,
) -> AuthCryptoHandleResult:
    """
    Decode one reserved auth/crypto payload into structured Python facts.

    The bool-returning wrapper remains for generic reserved-service dispatch.
    Live rekey orchestration can use this richer result to decide whether to
    accept, reject, respond, or hot-apply a session without teaching Rust any
    auth/crypto semantics.
    """
    try:
        message = RekeyControlMessage.decode(event.payload)
    except ValueError as exc:
        reason = str(exc)
        _publish(
            diagnostics,
            DiagnosticEvent.rekey_event(
                code="rekey.rejected",
                message="invalid rekey control payload",
                peer=peer_name,
                severity="warning",
                details={
                    "path_id": event.path_id,
                    "sequence": event.sequence,
                    "frame_bytes": event.frame_bytes,
                    "reason": reason,
                },
            ),
        )
        return AuthCryptoHandleResult(accepted=False, reason=reason)

    code = "rekey.rejected" if message.message_type == "rekey_reject" else "rekey.started"
    severity = "warning" if message.message_type == "rekey_reject" else "info"
    _publish(
        diagnostics,
        DiagnosticEvent.rekey_event(
            code=code,
            message=f"{message.message_type.replace('_', ' ')} received",
            peer=peer_name or message.sender_node_id,
            severity=severity,
            details={
                "type": message.message_type,
                "sender_node_id": message.sender_node_id,
                "peer_node_id": message.peer_node_id,
                "topology_generation": message.topology_generation,
                "current_receiver_index": message.current_receiver_index,
                "created_at": message.created_at.isoformat(),
                "expires_at": message.expires_at.isoformat() if message.expires_at else None,
                "reason": message.reason,
                "path_id": event.path_id,
                "sequence": event.sequence,
                "frame_bytes": event.frame_bytes,
            },
        ),
    )
    return AuthCryptoHandleResult(accepted=True, message=message)


def _publish(diagnostics: DiagnosticsBus | None, event: DiagnosticEvent) -> None:
    """Publish if a diagnostics bus exists; reserved control must stay optional."""
    if diagnostics is not None:
        diagnostics.publish(event)
