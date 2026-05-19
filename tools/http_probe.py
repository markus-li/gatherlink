#!/usr/bin/env python3
"""Fetch one HTTP path directly from a TCP endpoint."""

from __future__ import annotations

import argparse
import socket


def main() -> int:
    """Fetch the HTTP response body from a direct TCP endpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="HTTP target as host:port.")
    parser.add_argument("--path", default="/text", help="HTTP path to request.")
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()

    target_host, target_port = _parse_host_port(args.target)
    response = fetch_http(target_host=target_host, target_port=target_port, path=args.path, timeout=args.timeout)
    _headers, _separator, body = response.partition(b"\r\n\r\n")
    print(body.decode("utf-8", errors="replace"))
    return 0


def fetch_http(*, target_host: str, target_port: int, path: str = "/text", timeout: float = 12.0) -> bytes:
    """Return one HTTP response fetched directly over TCP."""
    with socket.create_connection((target_host, target_port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        request = f"GET {path} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode("ascii"))
        # The helper stream adapter treats TCP half-close as the signal that
        # request bytes are complete. Sending it here keeps one-shot probes from
        # depending on peer-specific HTTP keepalive behavior.
        sock.shutdown(socket.SHUT_WR)
        return _read_http_response(sock, timeout=timeout)


def _read_http_response(sock: socket.socket, *, timeout: float) -> bytes:
    """Read until HTTP content length is satisfied or the peer closes."""
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    expected_total: int | None = None
    while True:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            if chunks:
                break
            raise
        if not chunk:
            break
        chunks.append(chunk)
        response = b"".join(chunks)
        if expected_total is None and b"\r\n\r\n" in response:
            header_block, _separator, _body = response.partition(b"\r\n\r\n")
            for line in header_block.split(b"\r\n"):
                name, separator, value = line.partition(b":")
                if separator and name.lower() == b"content-length":
                    expected_total = len(header_block) + 4 + int(value.strip())
                    break
        if expected_total and len(response) >= expected_total:
            break
    return b"".join(chunks)


def _parse_host_port(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port:
        raise ValueError("expected host:port")
    return host, int(port)


if __name__ == "__main__":
    raise SystemExit(main())
