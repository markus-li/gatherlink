#!/usr/bin/env python3
"""Fetch an HTTP path through a SOCKS5 CONNECT proxy."""

from __future__ import annotations

import argparse
import socket


def main() -> int:
    """Run one SOCKS5 CONNECT and print the HTTP response body."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socks", required=True, help="SOCKS5 endpoint as host:port.")
    parser.add_argument("--target", required=True, help="HTTP target as host:port.")
    parser.add_argument("--path", default="/text", help="HTTP path to request.")
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()

    socks_host, socks_port = _parse_host_port(args.socks)
    target_host, target_port = _parse_host_port(args.target)
    response = fetch_via_socks5(
        socks_host=socks_host,
        socks_port=socks_port,
        target_host=target_host,
        target_port=target_port,
        path=args.path,
        timeout=args.timeout,
    )
    _headers, _separator, body = response.partition(b"\r\n\r\n")
    print(body.decode("utf-8", errors="replace"))
    return 0


def fetch_via_socks5(
    *,
    socks_host: str,
    socks_port: int,
    target_host: str,
    target_port: int,
    path: str = "/text",
    timeout: float = 12.0,
) -> bytes:
    """Return one HTTP response fetched through a SOCKS5 CONNECT proxy."""
    with socket.create_connection((socks_host, socks_port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        if sock.recv(2) != b"\x05\x00":
            raise RuntimeError("SOCKS5 server did not accept no-auth method")
        sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(target_host) + target_port.to_bytes(2, "big"))
        reply = sock.recv(10)
        if len(reply) < 2 or reply[1] != 0:
            raise RuntimeError(f"SOCKS5 CONNECT failed: {reply!r}")
        request = f"GET {path} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode("ascii"))
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
