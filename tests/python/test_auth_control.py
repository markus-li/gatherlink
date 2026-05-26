from __future__ import annotations

from datetime import UTC, datetime

from gatherlink.control.auth import decode_auth_crypto_event, handle_auth_crypto_event
from gatherlink.control.reserved import ReservedServicePayload
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.security.rekey_control import RekeyControlMessage


def test_auth_crypto_decoder_returns_structured_rekey_message() -> None:
    message = RekeyControlMessage(
        message_type="rekey_reject",
        sender_node_id="peer-node",
        peer_node_id="local-node",
        topology_generation=3,
        current_receiver_index=99,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        reason="operator rejected",
    )
    payload = ReservedServicePayload(
        service_id=8,
        path_id=2,
        sequence=40,
        payload=message.encode(),
        frame_bytes=len(message.encode()),
    )
    bus = DiagnosticsBus()

    result = decode_auth_crypto_event(payload, diagnostics=bus)

    assert result.accepted is True
    assert result.message == message
    assert bus._events[0].code == "rekey.rejected"
    assert bus._events[0].details["type"] == "rekey_reject"


def test_auth_crypto_boolean_wrapper_preserves_reserved_dispatch_contract() -> None:
    payload = ReservedServicePayload(service_id=8, path_id=2, sequence=40, payload=b"not-json", frame_bytes=8)
    bus = DiagnosticsBus()

    assert handle_auth_crypto_event(payload, diagnostics=bus) is False
    assert bus._events[0].code == "rekey.rejected"
    assert "invalid rekey control payload" in bus._events[0].details["reason"]
