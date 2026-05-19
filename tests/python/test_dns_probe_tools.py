from __future__ import annotations

import importlib.util
import socket
import threading
from pathlib import Path

import dns.message
import dns.rcode


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(f"tools/{name}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DNS_PROBE = _load_tool("dns_probe")
_DNS_STATIC_SERVER = _load_tool("dns_static_server")


def test_static_dns_response_answers_configured_a_record() -> None:
    query = dns.message.make_query("vm-dns.gatherlink.test.", "A")

    response = dns.message.from_wire(
        _DNS_STATIC_SERVER.build_static_response(
            query.to_wire(),
            name="vm-dns.gatherlink.test.",
            address="192.0.2.77",
            ttl=30,
        )
    )

    assert response.rcode() == dns.rcode.NOERROR
    assert _DNS_PROBE.response_values(response, query_type="A") == ["192.0.2.77"]


def test_static_dns_response_fails_unknown_name_closed() -> None:
    query = dns.message.make_query("other.gatherlink.test.", "A")

    response = dns.message.from_wire(
        _DNS_STATIC_SERVER.build_static_response(
            query.to_wire(),
            name="vm-dns.gatherlink.test.",
            address="192.0.2.77",
            ttl=30,
        )
    )

    assert response.rcode() == dns.rcode.NXDOMAIN
    assert _DNS_PROBE.response_values(response, query_type="A") == []


def test_dns_probe_queries_udp_server() -> None:
    stop = threading.Event()
    ready = threading.Event()

    def run_server() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.bind(("127.0.0.1", 0))
            port_holder.append(udp_socket.getsockname()[1])
            ready.set()
            while not stop.is_set():
                udp_socket.settimeout(0.1)
                try:
                    query_wire, source = udp_socket.recvfrom(4096)
                except TimeoutError:
                    continue
                response_wire = _DNS_STATIC_SERVER.build_static_response(
                    query_wire,
                    name="vm-dns.gatherlink.test.",
                    address="192.0.2.77",
                    ttl=30,
                )
                udp_socket.sendto(response_wire, source)

    port_holder: list[int] = []
    thread = threading.Thread(target=run_server)
    thread.start()
    ready.wait(timeout=2)
    try:
        response = _DNS_PROBE.query_dns(
            server_host="127.0.0.1",
            server_port=port_holder[0],
            name="vm-dns.gatherlink.test.",
            query_type="A",
            timeout=2,
        )
    finally:
        stop.set()
        thread.join(timeout=2)

    assert _DNS_PROBE.response_values(response, query_type="A") == ["192.0.2.77"]
