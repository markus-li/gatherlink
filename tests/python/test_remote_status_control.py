from __future__ import annotations

from dataclasses import dataclass

from gatherlink.control.remote_status import (
    RemoteStatusState,
    decode_message,
    encode_request,
    encode_response,
    handle_event,
    send_request_if_due,
)
from gatherlink.protocol import SERVICE_ID_REMOTE_STATUS


@dataclass(frozen=True)
class FakeReservedEvent:
    service_id: int
    path_id: int
    sequence: int
    payload: bytes
    frame_bytes: int = 14
    peer_scope: int | None = None


class FakeDataplane:
    def __init__(self) -> None:
        self.transmitted: list[tuple[int, bytes]] = []
        self.transmitted_to_peer: list[tuple[int, bytes, int]] = []

    def transmit_service_payload(self, service_id: int, payload: bytes) -> int:
        self.transmitted.append((service_id, payload))
        return 1

    def transmit_service_payload_to_peer(self, service_id: int, payload: bytes, peer_scope: int) -> int:
        self.transmitted_to_peer.append((service_id, payload, peer_scope))
        return 1


def test_remote_status_request_and_response_round_trip() -> None:
    request = decode_message(encode_request(42, ttl_seconds=30))
    response = decode_message(encode_response(42, {"running": True, "node": "sink"}))

    assert request is not None
    assert request.type == "status_request"
    assert request.request_id == 42
    assert request.ttl_seconds == 30
    assert response is not None
    assert response.type == "status_response"
    assert response.status == {"running": True, "node": "sink"}


def test_remote_status_handler_replies_to_read_only_request() -> None:
    dataplane = FakeDataplane()
    state = RemoteStatusState()
    event = FakeReservedEvent(SERVICE_ID_REMOTE_STATUS, 3, 9, encode_request(7))

    handled = handle_event(
        event,
        dataplane=dataplane,
        state=state,
        peer_name="peer",
        status_provider=lambda: {"running": True},
    )

    assert handled
    assert dataplane.transmitted[0][0] == SERVICE_ID_REMOTE_STATUS
    assert decode_message(dataplane.transmitted[0][1]).status == {"running": True}


def test_remote_status_response_uses_peer_scope_when_available() -> None:
    dataplane = FakeDataplane()
    state = RemoteStatusState()
    event = FakeReservedEvent(SERVICE_ID_REMOTE_STATUS, 3, 9, encode_request(7), peer_scope=201)

    handled = handle_event(
        event,
        dataplane=dataplane,
        state=state,
        peer_name="peer",
        status_provider=lambda: {"running": True},
    )

    assert handled
    assert dataplane.transmitted == []
    assert dataplane.transmitted_to_peer[0][2] == 201


def test_remote_status_handler_caches_response_for_monitor() -> None:
    dataplane = FakeDataplane()
    state = RemoteStatusState()
    event = FakeReservedEvent(SERVICE_ID_REMOTE_STATUS, 4, 9, encode_response(8, {"running": True}))

    handled = handle_event(
        event,
        dataplane=dataplane,
        state=state,
        peer_name="sink",
        status_provider=lambda: {},
    )

    assert handled
    assert state.cache["sink"]["request_id"] == 8
    assert state.cache["sink"]["source_path_id"] == 4
    assert state.cache["sink"]["status"] == {"running": True}


def test_remote_status_request_sends_only_when_enabled() -> None:
    dataplane = FakeDataplane()
    state = RemoteStatusState()

    assert not send_request_if_due(dataplane, state)
    state.request(ttl_seconds=30)
    assert send_request_if_due(dataplane, state)
    assert dataplane.transmitted[0][0] == SERVICE_ID_REMOTE_STATUS


def test_remote_status_request_can_target_each_authenticated_peer_scope() -> None:
    dataplane = FakeDataplane()
    state = RemoteStatusState()
    state.request(ttl_seconds=30)

    assert send_request_if_due(dataplane, state, peer_scopes={201: "source-a", 202: "source-c"})

    assert dataplane.transmitted == []
    assert [(service_id, peer_scope) for service_id, _payload, peer_scope in dataplane.transmitted_to_peer] == [
        (SERVICE_ID_REMOTE_STATUS, 201),
        (SERVICE_ID_REMOTE_STATUS, 202),
    ]
