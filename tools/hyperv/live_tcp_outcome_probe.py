#!/usr/bin/env python3
"""
Push live service-outcome facts from Linux TCP socket counters.

This probe is benchmark/helper-side policy. It samples `ss -tin` for a target
TCP peer during WireGuard-over-Gatherlink tests and sends the compact service
outcome payload over Gatherlink's existing service IPC socket. Rust never sees
retransmit semantics; Python only receives a service outcome fact and then
decides whether to adjust compiled service-budget primitives.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from gatherlink.runtime.services import ServiceIpcError, ServiceRegistry, request_service

RETRANSMIT_RE = re.compile(r"\bretrans:(\d+)(?:/(\d+))?\b")
TCP_SNMP = Path("/proc/net/snmp")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for one bounded live outcome probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--service-record", required=True, help="Registered Gatherlink service to update over IPC.")
    parser.add_argument("--runtime-service", required=True, help="Runtime service name the outcome belongs to.")
    parser.add_argument("--target", required=True, help="Target IP substring to match in ss output.")
    parser.add_argument("--max-retrans", type=int, default=0)
    return parser.parse_args()


def tcp_retransmits_for_target(target: str) -> int:
    """Return the largest retransmit counter visible for sockets matching target."""
    try:
        completed = subprocess.run(
            ["ss", "-tin"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return 0
    lines = completed.stdout.splitlines()
    max_retransmits = 0
    for index, line in enumerate(lines):
        previous_line = lines[index - 1] if index > 0 else ""
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        socket_block = f"{previous_line}\n{line}\n{next_line}"
        if target not in socket_block:
            continue
        max_retransmits = max(max_retransmits, _retransmits_from_line(socket_block))
    return max_retransmits


def tcp_global_retransmits() -> int:
    """
    Return Linux's global TCP RetransSegs counter when available.

    This is intentionally a benchmark-helper fallback, not Gatherlink control
    policy. Some kernels do not expose useful per-socket retransmit counters
    through `ss` during short iperf runs, while iperf still reports retransmits
    at completion. The Hyper-V benchmark VM is dedicated to one active TCP
    transfer during this probe, so the global delta is a practical live signal.
    """
    try:
        lines = TCP_SNMP.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("Tcp:"):
            continue
        headers = line.split()
        values = lines[index + 1].split()
        if not values or values[0] != "Tcp:":
            continue
        try:
            retrans_index = headers.index("RetransSegs")
            return int(values[retrans_index])
        except (ValueError, IndexError):
            return 0
    return 0


def outcome_payload(*, service: str, retransmits: int, max_retransmits: int) -> dict[str, object]:
    """Return the compact IPC payload for the current retransmit state."""
    if retransmits > max_retransmits:
        outcomes = [
            {
                "service": service,
                "degraded": True,
                "reason": f"live tcp retransmits {retransmits} above {max_retransmits}",
            }
        ]
    else:
        outcomes = []
    return {"outcomes": outcomes}


def push_outcome(service_record: str, payload: dict[str, object]) -> None:
    """Push the latest service outcome over the existing service IPC socket."""
    record = ServiceRegistry().resolve(service_record)
    request_service(record, "service-outcome", payload=payload)


def _retransmits_from_line(line: str) -> int:
    """Extract the visible retransmit counter from one ss detail line."""
    match = RETRANSMIT_RE.search(line)
    if not match:
        return 0
    values = [int(value) for value in match.groups() if value is not None]
    return max(values, default=0)


def main() -> None:
    """Run the bounded probe loop and push changed outcomes through service IPC."""
    args = parse_args()
    deadline = time.monotonic() + args.duration
    last_payload: dict[str, object] | None = None
    global_retransmits_start = tcp_global_retransmits()
    while time.monotonic() <= deadline:
        socket_retransmits = tcp_retransmits_for_target(args.target)
        global_retransmits_delta = max(0, tcp_global_retransmits() - global_retransmits_start)
        retransmits = max(socket_retransmits, global_retransmits_delta)
        payload = outcome_payload(
            service=args.runtime_service,
            retransmits=retransmits,
            max_retransmits=args.max_retrans,
        )
        if payload != last_payload:
            try:
                push_outcome(args.service_record, payload)
                print(json.dumps({"sent": payload, "retransmits": retransmits}, sort_keys=True), flush=True)
            except (ServiceIpcError, ValueError) as exc:
                print(json.dumps({"error": str(exc), "retransmits": retransmits}, sort_keys=True), flush=True)
            last_payload = payload
        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    main()
