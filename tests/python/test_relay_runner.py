from __future__ import annotations

import base64
import socket
import time
from threading import Event

import pytest
from gatherlink.dataplane.rust_backend import RustDataplaneUnavailableError, _load_bindings
from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.runtime.relay_runner import RelayRunnerState, RelayRuntimeConfig, run_relay_service
from gatherlink.security.envelope import decrypt_packet_without_replay, encrypt_frame_with_counter
from gatherlink.security.relay_sessions import RelayExecutorConfig


class MemorySink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    def write(self, event: DiagnosticEvent) -> None:
        self.events.append(event)


class FakeRelayForwarder:
    def __init__(self, outcomes: list[dict[str, object]]) -> None:
        self.outcomes = outcomes
        self.forwarded = 0
        self.dropped = 0
        self.forwarded_bytes = 0
        self.emitted_bytes = 0

    def local_addr(self) -> str:
        return "127.0.0.1:55100"

    def try_forward_one(self, _now_unix_us: int) -> dict[str, object]:
        if not self.outcomes:
            return {"kind": "no_packet"}
        outcome = self.outcomes.pop(0)
        if outcome["kind"] == "forwarded":
            self.forwarded += 1
            self.forwarded_bytes += int(outcome["received_bytes"])
            self.emitted_bytes += int(outcome["emitted_bytes"])
        if outcome["kind"] == "dropped":
            self.dropped += 1
        return outcome

    def counters(self) -> dict[str, int]:
        return {
            "forwarded_packets": self.forwarded,
            "forwarded_bytes": self.forwarded_bytes,
            "dropped_packets": self.dropped,
            "emitted_packets": self.forwarded,
            "emitted_bytes": self.emitted_bytes,
        }


def test_relay_runner_publishes_forward_and_drop_diagnostics() -> None:
    config = _relay_config()
    sink = MemorySink()
    bus = DiagnosticsBus(sinks=[sink])
    forwarder = FakeRelayForwarder(
        [
            {"kind": "forwarded", "source": "127.0.0.1:50000", "received_bytes": 40, "emitted_bytes": 44},
            {
                "kind": "dropped",
                "source": "127.0.0.1:50001",
                "reason": "UnknownReceiverIndex",
                "received_bytes": 40,
            },
        ]
    )

    result = run_relay_service(
        config,
        forwarder_factory=lambda _config: forwarder,
        max_iterations=2,
        diagnostics_bus=bus,
    )

    assert result.forwarded_packets == 1
    assert result.dropped_packets == 1
    assert [event.code for event in sink.events] == [
        "service.bound",
        "packet.forwarded",
        "relay.unknown_receiver_index",
        "runtime.shutdown",
    ]
    assert sink.events[2].stable_code
    assert sink.events[2].details["reason"] == "UnknownReceiverIndex"


def test_relay_runner_state_exposes_ipc_status_and_stop() -> None:
    config = _relay_config()
    state = RelayRunnerState(
        name=config.name,
        listen=config.listen,
        next_hop=config.executor.next_hop_address,
        direction=config.executor.direction,
        stop_event=Event(),
    )
    forwarder = FakeRelayForwarder(
        [{"kind": "forwarded", "source": "127.0.0.1:50000", "received_bytes": 40, "emitted_bytes": 44}]
    )

    result = run_relay_service(
        config,
        forwarder_factory=lambda _config: forwarder,
        max_iterations=1,
        runner_state=state,
    )

    assert result.forwarded_packets == 1
    assert state.snapshot()["running"] is False
    assert state.snapshot()["kind"] == "relay"
    assert state.snapshot()["forwarded_packets"] == 1
    state.stop()
    assert state.stop_event.is_set()


def test_relay_runner_status_can_mark_final_hop_exit() -> None:
    config = _relay_config().model_copy(update={"exit_to_inner_packet": True})
    state = RelayRunnerState(
        name=config.name,
        listen=config.listen,
        next_hop=config.executor.next_hop_address,
        direction=config.executor.direction,
        stop_event=Event(),
        exit_to_inner_packet=config.exit_to_inner_packet,
    )

    assert state.snapshot()["exit_to_inner_packet"] is True


def test_relay_runtime_config_validates_key_encoding() -> None:
    config = _relay_config(send_key="bad-key")

    with pytest.raises(ValueError, match="send_key must be base64 encoded"):
        config.keys.decoded_send_key()


def test_rust_relay_forwarder_rewraps_hop_packet_to_next_hop() -> None:
    bindings = _load_relay_bindings()
    upstream_to_relay = b"a" * 32
    relay_to_downstream = b"b" * 32
    relay_receiver_index = 55
    downstream_receiver_index = 77
    now_us = int(time.time() * 1_000_000)

    next_hop = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    next_hop.bind(("127.0.0.1", 0))
    next_hop.settimeout(1)
    forwarder = bindings.RelayHopForwarder.bind(
        "127.0.0.1:0",
        f"127.0.0.1:{next_hop.getsockname()[1]}",
        relay_receiver_index,
        downstream_receiver_index,
        relay_to_downstream,
        upstream_to_relay,
        now_us + 5_000_000,
        None,
        None,
        None,
    )
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    plaintext = b"endpoint-encrypted-packet-stays-opaque"
    sender.sendto(
        encrypt_frame_with_counter(relay_receiver_index, upstream_to_relay, 0, plaintext),
        _socket_addr(forwarder.local_addr()),
    )

    outcome = _poll_forwarder(forwarder, now_us)
    resealed, _source = next_hop.recvfrom(4096)
    decrypted = decrypt_packet_without_replay(relay_to_downstream, resealed)

    assert outcome["kind"] == "forwarded"
    assert decrypted.receiver_index == downstream_receiver_index
    assert decrypted.plaintext == plaintext
    assert forwarder.counters()["forwarded_packets"] == 1


def test_rust_relay_forwarder_silently_counts_invalid_hop_packet() -> None:
    bindings = _load_relay_bindings()
    now_us = int(time.time() * 1_000_000)
    next_hop = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    next_hop.bind(("127.0.0.1", 0))
    next_hop.settimeout(0.05)
    forwarder = bindings.RelayHopForwarder.bind(
        "127.0.0.1:0",
        f"127.0.0.1:{next_hop.getsockname()[1]}",
        55,
        77,
        b"b" * 32,
        b"a" * 32,
        now_us + 5_000_000,
        None,
        None,
        None,
    )
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.sendto(b"not-authenticated", _socket_addr(forwarder.local_addr()))

    outcome = _poll_forwarder(forwarder, now_us)

    assert outcome["kind"] == "dropped"
    assert outcome["reason"] == "HopAuthFailed"
    assert forwarder.counters()["dropped_packets"] == 1
    with pytest.raises(TimeoutError):
        next_hop.recvfrom(4096)


def _relay_config(send_key: str | None = None) -> RelayRuntimeConfig:
    key = base64.b64encode(b"a" * 32).decode("ascii")
    return RelayRuntimeConfig(
        name="relay.test",
        listen="127.0.0.1:0",
        executor=RelayExecutorConfig(
            relay_receiver_index=55,
            next_hop_address="127.0.0.1:51820",
            next_hop_receiver_index=77,
            direction="upstream_to_downstream",
            topology_generation=1,
            expires_at_unix_us=4102444800000000,
        ),
        keys={"send_key": send_key or key, "receive_key": key},
    )


def _load_relay_bindings():
    try:
        bindings = _load_bindings()
    except RustDataplaneUnavailableError as exc:
        pytest.skip(str(exc))
    if not hasattr(bindings, "RelayHopForwarder"):
        pytest.skip("Rust relay binding is not installed")
    return bindings


def _poll_forwarder(forwarder, now_us: int) -> dict[str, object]:
    for offset in range(100):
        outcome = forwarder.try_forward_one(now_us + offset)
        if outcome["kind"] != "no_packet":
            return outcome
        time.sleep(0.001)
    raise AssertionError("relay forwarder did not observe packet")


def _socket_addr(value: str) -> tuple[str, int]:
    host, port = value.rsplit(":", 1)
    return host, int(port)
