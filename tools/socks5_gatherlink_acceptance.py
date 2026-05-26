#!/usr/bin/env python3
"""
Run a local SOCKS5-over-Gatherlink acceptance proof.

The proof deliberately uses real processes for the core path:

local SOCKS client
  -> SOCKS5 helper
  -> Gatherlink node A UDP service
  -> Gatherlink path socket
  -> Gatherlink node B UDP service target
  -> helper stream exit
  -> status HTTP helper

This is not a throughput test. It is a small end-to-end correctness gate that
proves SOCKS5 helper traffic can move through the Gatherlink transport and exit
on the peer side before broader VM acceptance is attempted.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from socks5_http_probe import fetch_via_socks5


@dataclass(frozen=True)
class AcceptancePorts:
    """Local ports used by one isolated acceptance run."""

    path_a: int
    path_b: int
    service_listen: int
    service_target: int
    socks: int
    status_http: int


def main() -> int:
    """Run the local acceptance proof and write a report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="Directory for configs, logs, and report.")
    parser.add_argument("--timeout", type=float, default=12.0, help="Readiness and request timeout in seconds.")
    args = parser.parse_args()

    out_dir = args.out or Path(tempfile.mkdtemp(prefix="gatherlink-socks5-acceptance-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ports = _allocate_ports(6)
    acceptance_ports = AcceptancePorts(
        path_a=ports[0],
        path_b=ports[1],
        service_listen=ports[2],
        service_target=ports[3],
        socks=ports[4],
        status_http=ports[5],
    )
    config_a, config_b = _write_configs(out_dir, acceptance_ports)
    report: dict[str, Any] = {
        "out_dir": str(out_dir),
        "ports": acceptance_ports.__dict__,
        "passed": False,
        "steps": [],
    }
    process_env = os.environ.copy()
    # Unix-domain socket paths are short on Linux, so keep the registry outside
    # deeply nested pytest/report paths.
    process_env["GATHERLINK_SERVICE_REGISTRY"] = tempfile.mkdtemp(prefix="gl-svc-")
    report["service_registry"] = process_env["GATHERLINK_SERVICE_REGISTRY"]
    processes: list[subprocess.Popen[str]] = []
    try:
        _run_checked(["gatherlink", "config", "validate", str(config_a)], report, "validate-node-a")
        _run_checked(["gatherlink", "config", "validate", str(config_b)], report, "validate-node-b")

        processes.append(
            _start_process(
                out_dir,
                "status-http",
                [
                    "gatherlink",
                    "helpers",
                    "status-http",
                    "--listen",
                    f"127.0.0.1:{acceptance_ports.status_http}",
                    "--write-window-seconds",
                    "0",
                ],
                env=process_env,
            )
        )
        _wait_for_tcp("127.0.0.1", acceptance_ports.status_http, args.timeout)

        processes.append(
            _start_process(
                out_dir,
                "stream-exit",
                [
                    "gatherlink",
                    "helpers",
                    "stream-exit",
                    "--listen",
                    f"127.0.0.1:{acceptance_ports.service_target}",
                    "--allow-host",
                    "127.0.0.1",
                    "--allow-port",
                    str(acceptance_ports.status_http),
                    "--diagnostics-jsonl",
                    str(out_dir / "stream-exit.jsonl"),
                ],
                env=process_env,
            )
        )
        _wait_for_udp_bind(processes[-1], args.timeout)

        processes.append(
            _start_process(
                out_dir,
                "node-b",
                [
                    "gatherlink",
                    "run",
                    "service",
                    str(config_b),
                    "--diagnostics-jsonl",
                    str(out_dir / "node-b.jsonl"),
                    "--service-name",
                    "socks5.acceptance.node-b",
                    "--service-log",
                    str(out_dir / "node-b-service.log"),
                    "--batch-size",
                    "64",
                ],
                env=process_env,
            )
        )
        processes.append(
            _start_process(
                out_dir,
                "node-a",
                [
                    "gatherlink",
                    "run",
                    "service",
                    str(config_a),
                    "--diagnostics-jsonl",
                    str(out_dir / "node-a.jsonl"),
                    "--service-name",
                    "socks5.acceptance.node-a",
                    "--service-log",
                    str(out_dir / "node-a-service.log"),
                    "--batch-size",
                    "64",
                ],
                env=process_env,
            )
        )
        time.sleep(1.0)

        processes.append(
            _start_process(
                out_dir,
                "socks5",
                [
                    "gatherlink",
                    "helpers",
                    "socks5-serve",
                    "--listen",
                    f"127.0.0.1:{acceptance_ports.socks}",
                    "--allow-host",
                    "127.0.0.1",
                    "--allow-port",
                    str(acceptance_ports.status_http),
                    "--gatherlink-service",
                    f"127.0.0.1:{acceptance_ports.service_listen}",
                    "--diagnostics-jsonl",
                    str(out_dir / "socks5.jsonl"),
                ],
                env=process_env,
            )
        )
        _wait_for_tcp("127.0.0.1", acceptance_ports.socks, args.timeout)

        response = fetch_via_socks5(
            socks_host="127.0.0.1",
            socks_port=acceptance_ports.socks,
            target_host="127.0.0.1",
            target_port=acceptance_ports.status_http,
            path="/text",
            timeout=args.timeout,
        )
        headers, _, body = response.partition(b"\r\n\r\n")
        body_text = body.decode("utf-8")
        assert b"200 OK" in headers, headers.decode("utf-8", errors="replace")
        assert f"listening=127.0.0.1:{acceptance_ports.status_http}" in body_text
        assert "Gatherlink local status (EXPERIMENTAL)" in body_text
        # Give background diagnostics drains a short chance to flush before the
        # supervisor tears down foreground helper processes.
        time.sleep(0.75)
        diagnostics = {
            "socks5": _read_jsonl_codes(out_dir / "socks5.jsonl"),
            "stream_exit": _read_jsonl_codes(out_dir / "stream-exit.jsonl"),
        }
        assert "helper.stream.opened" in diagnostics["socks5"], diagnostics
        assert "helper.stream.opened" in diagnostics["stream_exit"], diagnostics
        report["http_status_payload"] = {
            "listen": f"127.0.0.1:{acceptance_ports.status_http}",
            "body_preview": body_text[:300],
        }
        report["diagnostics"] = diagnostics
        report["passed"] = True
        report["steps"].append("socks5-http-over-gatherlink-ok")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        for process in reversed(processes):
            _terminate(process)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def _write_configs(out_dir: Path, ports: AcceptancePorts) -> tuple[Path, Path]:
    key_ab = _random_key()
    key_ba = _random_key()
    common_service = {
        "name": "socks5-stream",
        "target": f"127.0.0.1:{ports.service_target}",
    }
    node_a = {
        "schema_version": 1,
        "node": "socks5-acceptance-a",
        "role": "client",
        "peer": "socks5-acceptance-b",
        "paths": [
            {
                "name": "path-a",
                "interface": "lo",
                "transport_bind": f"127.0.0.1:{ports.path_a}",
                "transport_remote": f"127.0.0.1:{ports.path_b}",
            }
        ],
        "services": [
            {
                **common_service,
                "listen": f"127.0.0.1:{ports.service_listen}",
                "return_mode": "learned-single-source",
            }
        ],
        "security": {
            "mode": "authenticated",
            "receiver_index": 7771,
            "send_key": key_ab,
            "receive_key": key_ba,
        },
    }
    node_b = {
        "schema_version": 1,
        "node": "socks5-acceptance-b",
        "role": "server",
        "peer": "socks5-acceptance-a",
        "paths": [
            {
                "name": "path-a",
                "interface": "lo",
                "transport_bind": f"127.0.0.1:{ports.path_b}",
                "transport_remote": f"127.0.0.1:{ports.path_a}",
            }
        ],
        "services": [{**common_service, "listen": "127.0.0.1:0"}],
        "security": {
            "mode": "authenticated",
            "receiver_index": 7771,
            "send_key": key_ba,
            "receive_key": key_ab,
        },
    }
    path_a = out_dir / "node-a.json"
    path_b = out_dir / "node-b.json"
    path_a.write_text(json.dumps(node_a, indent=2, sort_keys=True), encoding="utf-8")
    path_b.write_text(json.dumps(node_b, indent=2, sort_keys=True), encoding="utf-8")
    return path_a, path_b


def _start_process(
    out_dir: Path, name: str, command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    log = (out_dir / f"{name}.log").open("w", encoding="utf-8")
    resolved = _resolve_command(command)
    return subprocess.Popen(
        resolved,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )


def _resolve_command(command: list[str]) -> list[str]:
    if command[0] == "gatherlink":
        return [sys.executable, "-m", "gatherlink.cli.main", *command[1:]]
    executable = shutil.which(command[0])
    if executable is None:
        raise RuntimeError(f"command not found: {command[0]}")
    return [executable, *command[1:]]


def _run_checked(command: list[str], report: dict[str, Any], label: str) -> None:
    resolved = _resolve_command(command)
    result = subprocess.run(resolved, check=True, text=True, capture_output=True)
    report["steps"].append(label)
    if result.stdout.strip():
        report[f"{label}_stdout"] = result.stdout.strip()


def _wait_for_tcp(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for TCP {host}:{port}")


def _wait_for_udp_bind(process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited before UDP readiness: pid={process.pid}")
        time.sleep(0.1)
        return
    raise TimeoutError(f"timed out waiting for UDP process pid={process.pid}")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


def _allocate_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            with closing(sock):
                pass


def _read_jsonl_codes(path: Path) -> list[str]:
    """Return diagnostic event codes from one JSONL sink, tolerating blank lines."""
    if not path.exists():
        return []
    codes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = event.get("code")
        if isinstance(code, str):
            codes.append(code)
    return codes


def _random_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


if __name__ == "__main__":
    raise SystemExit(main())
