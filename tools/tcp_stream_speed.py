#!/usr/bin/env python3
"""Measure simple TCP stream throughput for helper-path benchmarks."""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Endpoint:
    """Host and port parsed from a CLI endpoint string."""

    host: str
    port: int


def main() -> int:
    """Run a TCP sender or sink and write one JSON result to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sink = subparsers.add_parser("sink", help="Accept TCP connections and count received bytes.")
    sink.add_argument("--bind", required=True, help="Bind endpoint as host:port.")
    sink.add_argument("--duration", type=float, default=20.0, help="Maximum sink lifetime in seconds.")
    sink.add_argument("--idle-after-first", type=float, default=2.0, help="Stop after this idle time once data arrives.")
    sink.add_argument("--receive-size", type=int, default=262_144, help="Socket recv size.")

    send = subparsers.add_parser("send", help="Send TCP bytes for a duration.")
    send.add_argument("--target", required=True, help="Target endpoint as host:port.")
    send.add_argument("--duration", type=float, default=10.0, help="Send duration in seconds.")
    send.add_argument("--payload-size", type=int, default=65_536, help="Write block size.")
    send.add_argument("--target-mbit", type=float, default=0.0, help="Optional transmit cap in decimal Mbit/s.")
    send.add_argument("--connect-timeout", type=float, default=10.0, help="TCP connect timeout in seconds.")

    args = parser.parse_args()
    if args.command == "sink":
        result = run_sink(
            bind=parse_endpoint(args.bind),
            duration_seconds=args.duration,
            idle_after_first_seconds=args.idle_after_first,
            receive_size=args.receive_size,
        )
    else:
        result = run_sender(
            target=parse_endpoint(args.target),
            duration_seconds=args.duration,
            payload_size=args.payload_size,
            target_mbit=args.target_mbit,
            connect_timeout_seconds=args.connect_timeout,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_sink(
    *,
    bind: Endpoint,
    duration_seconds: float,
    idle_after_first_seconds: float,
    receive_size: int,
) -> dict[str, Any]:
    """Accept TCP streams until the duration expires and count received bytes."""
    started = time.monotonic()
    deadline = started + duration_seconds
    first_data_at: float | None = None
    last_data_at: float | None = None
    bytes_received = 0
    connections = 0
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((bind.host, bind.port))
    server.listen(16)
    server.settimeout(0.2)
    try:
        while time.monotonic() < deadline:
            if last_data_at is not None and time.monotonic() - last_data_at >= idle_after_first_seconds:
                break
            try:
                conn, _peer = server.accept()
            except TimeoutError:
                continue
            connections += 1
            conn.settimeout(0.2)
            with conn:
                while time.monotonic() < deadline:
                    try:
                        chunk = conn.recv(receive_size)
                    except TimeoutError:
                        if last_data_at is not None and time.monotonic() - last_data_at >= idle_after_first_seconds:
                            break
                        continue
                    if not chunk:
                        break
                    bytes_received += len(chunk)
                    if first_data_at is None:
                        first_data_at = time.monotonic()
                    last_data_at = time.monotonic()
    finally:
        server.close()

    elapsed = max(time.monotonic() - started, 0.000_001)
    active_elapsed = max((last_data_at or time.monotonic()) - (first_data_at or started), 0.000_001)
    return {
        "role": "sink",
        "bytes": bytes_received,
        "bits_per_second": bytes_received * 8 / elapsed,
        "mbit_per_second": bytes_received * 8 / elapsed / 1_000_000,
        "active_bits_per_second": bytes_received * 8 / active_elapsed,
        "active_mbit_per_second": bytes_received * 8 / active_elapsed / 1_000_000,
        "connections": connections,
        "elapsed_seconds": elapsed,
        "active_elapsed_seconds": active_elapsed,
    }


def run_sender(
    *,
    target: Endpoint,
    duration_seconds: float,
    payload_size: int,
    target_mbit: float,
    connect_timeout_seconds: float,
) -> dict[str, Any]:
    """Send bytes to one TCP target for the requested duration."""
    payload = b"x" * payload_size
    started = time.monotonic()
    deadline = started + duration_seconds
    bytes_sent = 0
    writes = 0
    with socket.create_connection((target.host, target.port), timeout=connect_timeout_seconds) as sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while time.monotonic() < deadline:
            sock.sendall(payload)
            bytes_sent += len(payload)
            writes += 1
            if target_mbit > 0:
                expected_elapsed = (bytes_sent * 8) / (target_mbit * 1_000_000)
                sleep_for = started + expected_elapsed - time.monotonic()
                if sleep_for > 0:
                    time.sleep(min(sleep_for, 0.01))
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)

    elapsed = max(time.monotonic() - started, 0.000_001)
    return {
        "role": "sender",
        "bytes": bytes_sent,
        "bits_per_second": bytes_sent * 8 / elapsed,
        "mbit_per_second": bytes_sent * 8 / elapsed / 1_000_000,
        "writes": writes,
        "elapsed_seconds": elapsed,
    }


def parse_endpoint(value: str) -> Endpoint:
    """Parse a host:port endpoint from CLI input."""
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port:
        raise argparse.ArgumentTypeError("expected host:port")
    return Endpoint(host=host, port=int(port))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1) from None
