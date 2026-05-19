"""Small DNS query probe for Gatherlink helper and VM acceptance checks."""

from __future__ import annotations

import argparse

import dns.message
import dns.query
import dns.rdatatype


def query_dns(
    *,
    server_host: str,
    server_port: int,
    name: str,
    query_type: str,
    timeout: float,
) -> dns.message.Message:
    """Send one UDP DNS query and return the parsed response."""
    rdtype = dns.rdatatype.from_text(query_type)
    query = dns.message.make_query(name, rdtype)
    return dns.query.udp(query, server_host, port=server_port, timeout=timeout)


def response_values(response: dns.message.Message, *, query_type: str) -> list[str]:
    """Return text values from answer records matching the requested type."""
    rdtype = dns.rdatatype.from_text(query_type)
    values: list[str] = []
    for answer in response.answer:
        if answer.rdtype != rdtype:
            continue
        values.extend(item.to_text() for item in answer.items)
    return values


def _parse_host_port(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    return host, port


def main() -> None:
    """Run the DNS probe command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=_parse_host_port, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--type", default="A")
    parser.add_argument("--expect", default=None)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    response = query_dns(
        server_host=args.server[0],
        server_port=args.server[1],
        name=args.name,
        query_type=args.type,
        timeout=args.timeout,
    )
    values = response_values(response, query_type=args.type)
    print(f"rcode={response.rcode()}")
    for value in values:
        print(value)
    if args.expect is not None and args.expect not in values:
        raise SystemExit(f"expected {args.expect!r}, got {values!r}")


if __name__ == "__main__":
    main()
