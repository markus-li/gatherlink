"""
Foreground Rust-backed secure relay-hop runner.

Relay orchestration is Python-owned: Python validates topology/session facts,
loads hop keys, owns diagnostics, and supervises lifecycle. Rust receives only
compiled socket/key/limit facts and executes one cheap hop at packet speed.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import Field

from gatherlink.dataplane.rust_backend import RustDataplaneUnavailableError, _load_bindings
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.security.relay_sessions import RelayExecutorConfig
from gatherlink.shared.models import GatherlinkBaseModel

RELAY_IDLE_SLEEP_SECONDS = 0.001


class RelayHopKeys(GatherlinkBaseModel):
    """Base64 encoded hop AEAD keys compiled by Python provisioning."""

    send_key: str
    receive_key: str

    def decoded_send_key(self) -> bytes:
        """Return the raw 32-byte key used to seal packets to the next hop."""
        return _decode_key(self.send_key, "send_key")

    def decoded_receive_key(self) -> bytes:
        """Return the raw 32-byte key used to open packets from the previous hop."""
        return _decode_key(self.receive_key, "receive_key")


class RelayRuntimeConfig(GatherlinkBaseModel):
    """
    Runtime config for one relay-hop foreground runner.

    This is not topology policy. It is the already-compiled execution shape used
    after Python has authorized a relay session and selected the UDP next hop.
    """

    schema_version: int = 1
    name: str
    listen: str
    executor: RelayExecutorConfig
    keys: RelayHopKeys
    poll_sleep_seconds: float = Field(default=RELAY_IDLE_SLEEP_SECONDS, ge=0.0, le=1.0)

    @classmethod
    def load(cls, path: Path) -> RelayRuntimeConfig:
        """Load relay runtime config from JSON."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class RelayRunnerResult:
    """Summary from a bounded relay runner invocation."""

    iterations: int
    forwarded_packets: int
    dropped_packets: int
    emitted_packets: int
    forwarded_bytes: int
    emitted_bytes: int


@dataclass
class RelayRunnerState:
    """Live relay counters and stop signal exposed through service IPC."""

    name: str
    listen: str
    next_hop: str
    direction: str
    stop_event: Event
    running: bool = False
    iterations: int = 0
    forwarded_packets: int = 0
    dropped_packets: int = 0
    emitted_packets: int = 0
    forwarded_bytes: int = 0
    emitted_bytes: int = 0

    def snapshot(self) -> dict[str, object]:
        """Return a service-monitor-friendly relay status payload."""
        return {
            "running": self.running and not self.stop_event.is_set(),
            "kind": "relay",
            "name": self.name,
            "listen": self.listen,
            "next_hop": self.next_hop,
            "direction": self.direction,
            "iterations": self.iterations,
            "forwarded_packets": self.forwarded_packets,
            "dropped_packets": self.dropped_packets,
            "emitted_packets": self.emitted_packets,
            "forwarded_bytes": self.forwarded_bytes,
            "emitted_bytes": self.emitted_bytes,
        }

    def stop(self) -> None:
        """Request a graceful relay stop through IPC."""
        self.stop_event.set()


RelayForwarderFactory = Callable[[RelayRuntimeConfig], Any]


def bind_relay_hop_forwarder(config: RelayRuntimeConfig) -> Any:
    """Bind the narrow Rust relay-hop executor from compiled Python facts."""
    bindings = _load_bindings()
    return bindings.RelayHopForwarder.bind(
        config.listen,
        config.executor.next_hop_address,
        config.executor.relay_receiver_index,
        config.executor.next_hop_receiver_index,
        config.keys.decoded_send_key(),
        config.keys.decoded_receive_key(),
        config.executor.expires_at_unix_us,
        config.executor.max_packet_size,
        config.executor.max_packets,
        config.executor.max_bytes,
    )


def run_relay_service(
    config: RelayRuntimeConfig,
    *,
    forwarder_factory: RelayForwarderFactory = bind_relay_hop_forwarder,
    stop_event: Event | None = None,
    max_iterations: int | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
    runner_state: RelayRunnerState | None = None,
) -> RelayRunnerResult:
    """
    Run one compiled secure relay-hop service until stopped.

    Invalid relay packets are silent on the network. This loop only publishes
    local diagnostics and counters, preserving the fail-closed relay contract.
    """
    stop_event = stop_event or Event()
    forwarder = forwarder_factory(config)
    state = runner_state or RelayRunnerState(
        name=config.name,
        listen=config.listen,
        next_hop=config.executor.next_hop_address,
        direction=config.executor.direction,
        stop_event=stop_event,
    )
    state.running = True
    local_addr = _call_or_unknown(forwarder, "local_addr")
    if diagnostics_bus is not None:
        diagnostics_bus.publish(
            DiagnosticEvent.service_bound(
                service=config.name,
                listen=local_addr,
                target=config.executor.next_hop_address,
                details={
                    "kind": "relay",
                    "relay_receiver_index": config.executor.relay_receiver_index,
                    "next_hop_receiver_index": config.executor.next_hop_receiver_index,
                    "direction": config.executor.direction,
                },
            )
        )
    iterations = 0
    forwarded_packets = 0
    dropped_packets = 0

    while not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        state.iterations = iterations
        outcome = forwarder.try_forward_one(_unix_us())
        kind = outcome.get("kind") if isinstance(outcome, dict) else None
        if kind == "forwarded":
            forwarded_packets += 1
            state.forwarded_packets = forwarded_packets
            if diagnostics_bus is not None:
                diagnostics_bus.publish(
                    DiagnosticEvent.packet_forwarded(
                        service=config.name,
                        path=None,
                        packets=1,
                        bytes_forwarded=int(outcome.get("emitted_bytes", 0) or 0),
                    )
                )
        elif kind == "dropped":
            dropped_packets += 1
            state.dropped_packets = dropped_packets
            if diagnostics_bus is not None:
                diagnostics_bus.publish(_relay_drop_event(config, outcome))
        else:
            time.sleep(config.poll_sleep_seconds)
        if diagnostics_bus is not None:
            diagnostics_bus.drain(limit=128)

    counters = forwarder.counters()
    result = RelayRunnerResult(
        iterations=iterations,
        forwarded_packets=int(counters.get("forwarded_packets", forwarded_packets) or 0),
        dropped_packets=int(counters.get("dropped_packets", dropped_packets) or 0),
        emitted_packets=int(counters.get("emitted_packets", 0) or 0),
        forwarded_bytes=int(counters.get("forwarded_bytes", 0) or 0),
        emitted_bytes=int(counters.get("emitted_bytes", 0) or 0),
    )
    state.forwarded_packets = result.forwarded_packets
    state.dropped_packets = result.dropped_packets
    state.emitted_packets = result.emitted_packets
    state.forwarded_bytes = result.forwarded_bytes
    state.emitted_bytes = result.emitted_bytes
    if diagnostics_bus is not None:
        diagnostics_bus.publish(
            DiagnosticEvent.shutdown(
                reason="relay runner stopped",
                service=config.name,
                details=result.__dict__,
            )
        )
        diagnostics_bus.drain()
    state.running = False
    return result


def _relay_drop_event(config: RelayRuntimeConfig, outcome: dict[str, object]) -> DiagnosticEvent:
    reason = str(outcome.get("reason") or "unknown")
    code = {
        "UnknownReceiverIndex": "relay.unknown_receiver_index",
        "ExpiredSession": "relay.expired_session",
        "LimitExceeded": "relay.limit_exceeded",
        "PacketTooLarge": "relay.packet_too_large",
        "HopCryptoUnavailable": "relay.auth_failed",
        "HopAuthFailed": "relay.auth_failed",
    }.get(reason, "relay.auth_failed")
    return DiagnosticEvent.drop_event(
        code=code,
        service=config.name,
        peer=str(outcome.get("source") or "") or None,
        message="relay packet silently dropped",
        details={
            "reason": reason,
            "received_bytes": int(outcome.get("received_bytes", 0) or 0),
            "relay_receiver_index": config.executor.relay_receiver_index,
            "direction": config.executor.direction,
        },
    )


def _decode_key(value: str, field: str) -> bytes:
    """Decode one base64 encoded 32-byte secret without logging its value."""
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{field} must be base64 encoded") from exc
    if len(decoded) != 32:
        raise ValueError(f"{field} must decode to exactly 32 bytes")
    return decoded


def _unix_us() -> int:
    return int(time.time() * 1_000_000)


def _call_or_unknown(obj: Any, method: str) -> str:
    callable_method = getattr(obj, method, None)
    if callable(callable_method):
        try:
            return str(callable_method())
        except (RustDataplaneUnavailableError, RuntimeError, ValueError):
            return "unknown"
    return "unknown"
