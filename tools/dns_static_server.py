"""Tiny static UDP DNS server for Gatherlink acceptance probes."""

from __future__ import annotations

import argparse
import socket

import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset


def build_static_response(query_wire: bytes, *, name: str, address: str, ttl: int) -> bytes:
    """Build a minimal DNS response that answers one configured A record."""
    query = dns.message.from_wire(query_wire)
    response = dns.message.make_response(query)
    response.flags |= dns.flags.AA
    if not query.question:
        response.set_rcode(dns.rcode.FORMERR)
        return response.to_wire()

    question = query.question[0]
    expected_name = dns.name.from_text(name)
    if question.name != expected_name:
        response.set_rcode(dns.rcode.NXDOMAIN)
        return response.to_wire()
    if question.rdtype not in {dns.rdatatype.A, dns.rdatatype.ANY}:
        response.set_rcode(dns.rcode.NOERROR)
        return response.to_wire()

    response.answer.append(dns.rrset.from_text(question.name, ttl, dns.rdataclass.IN, dns.rdatatype.A, address))
    return response.to_wire()


def serve_static_dns(bind: tuple[str, int], *, name: str, address: str, ttl: int) -> None:
    """Serve the configured DNS answer until the process is stopped."""
    family = socket.AF_INET6 if ":" in bind[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.bind(bind)
        print(f"dns_static_server listening on {bind[0]}:{bind[1]} name={name} address={address}", flush=True)
        while True:
            query_wire, source = udp_socket.recvfrom(4096)
            response_wire = build_static_response(query_wire, name=name, address=address, ttl=ttl)
            udp_socket.sendto(response_wire, source)


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
    """Run the static DNS probe server."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=_parse_host_port, default=("127.0.0.1", 53053))
    parser.add_argument("--name", default="vm-dns.gatherlink.test.")
    parser.add_argument("--address", default="192.0.2.77")
    parser.add_argument("--ttl", type=int, default=30)
    args = parser.parse_args()
    if args.ttl < 0:
        parser.error("--ttl must be non-negative")
    serve_static_dns(args.listen, name=args.name, address=args.address, ttl=args.ttl)


if __name__ == "__main__":
    main()
