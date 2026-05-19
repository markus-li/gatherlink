"""Temporary read-only remote status over the reserved Gatherlink service lane."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gatherlink.protocol import SERVICE_ID_REMOTE_STATUS

REMOTE_STATUS_REQUEST_INTERVAL_SECONDS = 2.0
REMOTE_STATUS_REQUEST_TTL_SECONDS = 120.0
REMOTE_STATUS_CACHE_TTL_SECONDS = 10.0

REQUEST_TYPE = "status_request"
RESPONSE_TYPE = "status_response"


@dataclass(frozen=True)
class RemoteStatusMessage:
    """Decoded remote-status message carried on reserved service id 8."""

    type: str
    request_id: int
    created_at: str
    ttl_seconds: float | None = None
    status: dict[str, object] | None = None


@dataclass
class RemoteStatusState:
    """
    Mutable local state for explicit remote status requests and cached replies.

    Discovery is sparse and continuous. Remote status is intentionally louder:
    an operator-facing command enables it temporarily, the requester refreshes
    before the timeout, and the cache becomes stale automatically when replies
    stop arriving. This state contains no mutation command path.
    """

    enabled_until_monotonic: float | None = None
    next_request_at_monotonic: float = 0.0
    next_request_id: int = 1
    cache: dict[str, dict[str, object]] = field(default_factory=dict)

    def request(self, *, ttl_seconds: float = REMOTE_STATUS_REQUEST_TTL_SECONDS) -> dict[str, object]:
        """Enable temporary remote-status requests."""
        ttl_seconds = max(float(ttl_seconds), 1.0)
        self.enabled_until_monotonic = time.monotonic() + ttl_seconds
        self.next_request_at_monotonic = min(self.next_request_at_monotonic, time.monotonic())
        return self.status()

    def should_request(self) -> bool:
        """Return whether a request should be sent now."""
        if not self.is_enabled():
            return False
        return time.monotonic() >= self.next_request_at_monotonic

    def next_request_payload(self) -> tuple[int, bytes]:
        """Return the next request id and encoded payload, then schedule the next send."""
        request_id = self.next_request_id
        self.next_request_id += 1
        self.next_request_at_monotonic = time.monotonic() + REMOTE_STATUS_REQUEST_INTERVAL_SECONDS
        ttl_seconds = self.remaining_ttl_seconds()
        return request_id, encode_request(request_id, ttl_seconds=ttl_seconds)

    def store_response(self, message: RemoteStatusMessage, *, peer_name: str, source_path_id: int) -> None:
        """Cache one peer status response for service monitor/list consumers."""
        if message.status is None:
            return
        self.cache[peer_name] = {
            "received_at": datetime.now(UTC).isoformat(),
            "request_id": message.request_id,
            "source_path_id": source_path_id,
            "stale": False,
            "status": message.status,
        }

    def status(self) -> dict[str, object]:
        """Return the IPC status shape for local monitor rendering."""
        self._mark_stale_entries()
        return {
            "enabled": self.is_enabled(),
            "ttl_seconds": self.remaining_ttl_seconds(),
            "cache": self.cache,
        }

    def is_enabled(self) -> bool:
        """Return whether temporary remote status is still requested."""
        if self.enabled_until_monotonic is None:
            return False
        if time.monotonic() >= self.enabled_until_monotonic:
            self.enabled_until_monotonic = None
            return False
        return True

    def remaining_ttl_seconds(self) -> float | None:
        """Return seconds until remote status expires, if active."""
        if not self.is_enabled() or self.enabled_until_monotonic is None:
            return None
        return max(self.enabled_until_monotonic - time.monotonic(), 0.0)

    def _mark_stale_entries(self) -> None:
        now = datetime.now(UTC)
        for envelope in self.cache.values():
            received_at = envelope.get("received_at")
            if not isinstance(received_at, str):
                envelope["stale"] = True
                continue
            try:
                received = datetime.fromisoformat(received_at)
            except ValueError:
                envelope["stale"] = True
                continue
            envelope["stale"] = (now - received).total_seconds() > REMOTE_STATUS_CACHE_TTL_SECONDS


def encode_request(request_id: int, *, ttl_seconds: float | None = None) -> bytes:
    """Encode a read-only remote-status request."""
    return _encode(
        {
            "type": REQUEST_TYPE,
            "request_id": int(request_id),
            "created_at": datetime.now(UTC).isoformat(),
            **({"ttl_seconds": float(ttl_seconds)} if ttl_seconds is not None else {}),
        }
    )


def encode_response(request_id: int, status: dict[str, object]) -> bytes:
    """Encode a read-only remote-status response."""
    return _encode(
        {
            "type": RESPONSE_TYPE,
            "request_id": int(request_id),
            "created_at": datetime.now(UTC).isoformat(),
            "status": status,
        }
    )


def decode_message(payload: bytes) -> RemoteStatusMessage | None:
    """Decode one remote-status payload, returning ``None`` for invalid data."""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    message_type = raw.get("type")
    if message_type not in {REQUEST_TYPE, RESPONSE_TYPE}:
        return None
    request_id = raw.get("request_id")
    try:
        request_id_int = int(request_id)
    except (TypeError, ValueError):
        return None
    created_at = raw.get("created_at")
    if not isinstance(created_at, str):
        created_at = ""
    ttl_seconds = raw.get("ttl_seconds")
    try:
        ttl_seconds_float = float(ttl_seconds) if ttl_seconds is not None else None
    except (TypeError, ValueError):
        ttl_seconds_float = None
    status = raw.get("status")
    if message_type == RESPONSE_TYPE and not isinstance(status, dict):
        return None
    return RemoteStatusMessage(
        type=str(message_type),
        request_id=request_id_int,
        created_at=created_at,
        ttl_seconds=ttl_seconds_float,
        status=status if isinstance(status, dict) else None,
    )


def handle_event(
    event: Any,
    *,
    dataplane: Any,
    state: RemoteStatusState,
    peer_name: str,
    status_provider: Any,
    local_can_respond: bool = True,
    logger: Any | None = None,
) -> bool:
    """
    Handle one reserved service id 8 event.

    Python owns decoding and meaning. Rust only forwards reserved payload bytes
    to Python and frames the response payload Python asks it to transmit.
    """
    message = decode_message(event.payload)
    if message is None:
        _log(logger, f"invalid remote-status payload on path {event.path_id}; dropping {len(event.payload)}B")
        return False
    if message.type == REQUEST_TYPE:
        if not local_can_respond:
            _log(logger, "remote-status request dropped because this service is not configured to respond")
            return False
        try:
            _transmit_response(dataplane, event, encode_response(message.request_id, status_provider()))
        except RuntimeError as exc:
            _log(logger, f"remote-status response skipped: {exc}")
            return False
        return True
    if message.type == RESPONSE_TYPE:
        state.store_response(message, peer_name=peer_name, source_path_id=int(event.path_id))
        return True
    return False


def send_request_if_due(
    dataplane: Any,
    state: RemoteStatusState,
    *,
    peer_scopes: dict[int, str] | None = None,
) -> bool:
    """Send temporary remote-status requests if the local IPC command enabled them."""
    if not state.should_request():
        return False
    _request_id, payload = state.next_request_payload()
    if peer_scopes:
        scoped_transmit = getattr(dataplane, "transmit_service_payload_to_peer", None)
        if not callable(scoped_transmit):
            raise RuntimeError("dataplane does not support scoped remote-status requests")
        for peer_scope in peer_scopes:
            scoped_transmit(SERVICE_ID_REMOTE_STATUS, payload, int(peer_scope))
        return True
    dataplane.transmit_service_payload(SERVICE_ID_REMOTE_STATUS, payload)
    return True


def _encode(message: dict[str, object]) -> bytes:
    return json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _transmit_response(dataplane: Any, event: Any, payload: bytes) -> int:
    peer_scope = getattr(event, "peer_scope", None)
    scoped_transmit = getattr(dataplane, "transmit_service_payload_to_peer", None)
    if peer_scope is not None and callable(scoped_transmit):
        return int(scoped_transmit(SERVICE_ID_REMOTE_STATUS, payload, int(peer_scope)))
    return int(dataplane.transmit_service_payload(SERVICE_ID_REMOTE_STATUS, payload))


def _log(logger: Any | None, message: str) -> None:
    if logger is None:
        return
    logger(message)
