"""Lab smoke scenarios for optional helper services."""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset
from asyncio_socks_server import Address, Connection

from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.helpers.dns import DnsHelperResolver, DnsResolverPolicy, DnsUpstream
from gatherlink.helpers.relay_fabric import RelayCandidate, RelayEndpoint, discover_relays
from gatherlink.helpers.socks5 import GatherlinkSocks5Addon, Socks5ExitConnector, Socks5Policy
from gatherlink.helpers.tcp_forward import TcpForwardConfig, TcpForwarder
from gatherlink.helpers.transport import (
    HelperStreamTarget,
    LabDirectTcpStreamTransport,
    MissingGatherlinkTransportError,
)
from gatherlink.helpers.udp_stream import GatherlinkUdpStreamExit, GatherlinkUdpStreamTransport
from gatherlink.helpers.wireguard import (
    WireGuardSetupRequest,
    default_local_paths,
    generate_wireguard_setup,
    wireguard_tool_status,
    wireguard_transport_plans,
)
from gatherlink.time.helper_client import TimeCorrectionRequest, request_time_correction

EXAMPLE_CONFIG_DIR = Path("configs/examples")


@dataclass(frozen=True)
class HelperSmokeResult:
    """One helper smoke result for lab CLI output."""

    helper: str
    ok: bool
    detail: str


def run_all_helper_smokes() -> list[HelperSmokeResult]:
    """Run local userland smoke scenarios for all active helpers."""
    results = [
        _smoke_time_helper(),
        _smoke_dns_helper(),
        _smoke_dns_negative_helper(),
        _smoke_wireguard_helper(),
        _smoke_relay_fabric_helper(),
        _smoke_relay_negative_helper(),
        _smoke_transport_boundary_helper(),
    ]
    results.extend(asyncio.run(_run_async_smokes()))
    return results


async def _run_async_smokes() -> list[HelperSmokeResult]:
    return [
        await _smoke_gatherlink_udp_stream_helper(),
        await _smoke_tcp_forward_helper(),
        await _smoke_socks5_helper(),
    ]


def _smoke_time_helper() -> HelperSmokeResult:
    with tempfile.TemporaryDirectory() as temp_dir:
        socket_path = Path(temp_dir) / "time-helper.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(1)

        def serve_once() -> None:
            connection, _address = listener.accept()
            with connection:
                _request = connection.recv(4096)
                connection.sendall(
                    b"status=preview\n"
                    b"applied=false\n"
                    b"offset_us=10\n"
                    b"target_unix_us=1010\n"
                    b"system_unix_us=1000\n"
                )
            listener.close()

        thread = threading.Thread(target=serve_once)
        thread.start()
        response = request_time_correction(TimeCorrectionRequest(target_unix_us=1010), socket_path=socket_path)
        thread.join(timeout=2)
    return HelperSmokeResult(
        "time", response.status == "preview", f"status={response.status} offset={response.offset_us}"
    )


def _smoke_dns_helper() -> HelperSmokeResult:
    def fake_upstream(query, upstream):
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.10"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(upstreams=[DnsUpstream(name="lab", address="192.0.2.53")]),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("helper-smoke.example.", dns.rdatatype.A)
    result = resolver.resolve_wire(query.to_wire())
    response = dns.message.from_wire(result.response_wire)
    ok = response.answer[0][0].address == "192.0.2.10"
    return HelperSmokeResult("dns", ok, f"cache={result.diagnostic.cache} dnssec={result.diagnostic.dnssec.status}")


def _smoke_dns_negative_helper() -> HelperSmokeResult:
    def fake_upstream(query, upstream):
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.10"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            upstreams=[DnsUpstream(name="lab", address="192.0.2.53")],
            dnssec_mode="require_ad",
        ),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("unsigned.example.", dns.rdatatype.A)
    result = resolver.resolve_wire(query.to_wire())
    response = dns.message.from_wire(result.response_wire)
    ok = response.rcode() == dns.rcode.SERVFAIL and result.diagnostic.dnssec.status == "failed"
    return HelperSmokeResult("dns-negative", ok, f"rcode={dns.rcode.to_text(response.rcode())}")


async def _smoke_gatherlink_udp_stream_helper() -> HelperSmokeResult:
    async def echo(reader, writer) -> None:
        while data := await reader.read(1024):
            writer.write(data.upper())
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]
    exit_helper = GatherlinkUdpStreamExit(
        listen_host="127.0.0.1",
        listen_port=0,
        allowed_hosts=frozenset({"127.0.0.1"}),
        allowed_ports=frozenset({target_port}),
    )
    udp_exit = await exit_helper.start()
    exit_port = udp_exit.get_extra_info("sockname")[1]
    transport = GatherlinkUdpStreamTransport("127.0.0.1", exit_port)
    stream = await transport.open_stream(HelperStreamTarget("127.0.0.1", target_port), timeout_seconds=1)
    stream.writer.write(b"stream")
    await stream.writer.drain()
    stream.writer.write_eof()
    response = await asyncio.wait_for(stream.reader.read(1024), timeout=1)
    stream.writer.close()
    await stream.writer.wait_closed()
    udp_exit.close()
    target.close()
    await target.wait_closed()
    return HelperSmokeResult("gatherlink-udp-stream", response == b"STREAM", f"bytes={len(response)}")


async def _smoke_tcp_forward_helper() -> HelperSmokeResult:
    async def echo(reader, writer) -> None:
        data = await reader.read(1024)
        writer.write(data.upper())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]
    forwarder = TcpForwarder(
        TcpForwardConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            target_host="127.0.0.1",
            target_port=target_port,
            idle_timeout_seconds=1,
        ),
        transport=LabDirectTcpStreamTransport(),
    )
    server = await forwarder.start()
    listen_port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
    writer.write(b"helper")
    await writer.drain()
    writer.write_eof()
    response = await reader.read(1024)
    writer.close()
    await writer.wait_closed()
    server.close()
    target.close()
    await server.wait_closed()
    await target.wait_closed()
    return HelperSmokeResult(
        "tcp-forward",
        response == b"HELPER",
        f"up={forwarder.stats.bytes_up} down={forwarder.stats.bytes_down}",
    )


class _SocksSmokeConnector(Socks5ExitConnector):
    def __init__(self, target_host: str, target_port: int) -> None:
        self.target_host = target_host
        self.target_port = target_port

    async def open(self, host: str, port: int, *, timeout_seconds: float) -> Connection:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.target_host, self.target_port),
            timeout=timeout_seconds,
        )
        return Connection(reader=reader, writer=writer, address=Address(self.target_host, self.target_port))


async def _smoke_socks5_helper() -> HelperSmokeResult:
    async def echo(reader, writer) -> None:
        data = await reader.read(1024)
        writer.write(data[::-1])
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]
    addon = GatherlinkSocks5Addon(
        policy=Socks5Policy.allow(hosts=["example.test"], ports=[443]),
        exit_connector=_SocksSmokeConnector("127.0.0.1", target_port),
    )
    connection = await addon.on_connect(
        type("Flow", (), {"dst": type("Dst", (), {"host": "example.test", "port": 443})()})()
    )
    connection.writer.write(b"abc")
    await connection.writer.drain()
    connection.writer.write_eof()
    payload = await connection.reader.read(1024)
    connection.writer.close()
    await connection.writer.wait_closed()
    target.close()
    await target.wait_closed()
    return HelperSmokeResult(
        "socks5", payload == b"cba", f"accepted={addon.stats.accepted} opened={addon.stats.opened}"
    )


def _smoke_wireguard_helper() -> HelperSmokeResult:
    runtime = expand_config(validate_config_file(EXAMPLE_CONFIG_DIR / "wireguard-client.json"))
    plans = wireguard_transport_plans(runtime)
    setup = generate_wireguard_setup(
        WireGuardSetupRequest(model="split", paths=default_local_paths(2), security="static", local_only=True)
    )
    tool_status = wireguard_tool_status()
    ok = bool(
        plans
        and plans[0].gatherlink_listen
        and plans[0].wireguard_target
        and "gatherlink-client.json" in setup.files
        and "wireguard-stable-client.conf" in setup.files
    )
    return HelperSmokeResult(
        "wireguard",
        ok,
        f"service={plans[0].service if plans else '-'} setup=split wg={'yes' if tool_status['wg'] else 'no'}",
    )


def _smoke_relay_fabric_helper() -> HelperSmokeResult:
    relay = RelayCandidate(
        node_id="relay-smoke",
        endpoints=[RelayEndpoint(protocol="udp", address="203.0.113.10", port=51820)],
        capabilities=["v1", "authenticated"],
    )
    report = discover_relays([relay], required_protocol_version="v1", endpoint_probe=lambda _endpoint: True)
    return HelperSmokeResult(
        "relay-fabric", bool(report.usable_candidates()), json.dumps(report.health[0].export_dict())
    )


def _smoke_relay_negative_helper() -> HelperSmokeResult:
    relay = RelayCandidate(
        node_id="relay-old",
        endpoints=[RelayEndpoint(protocol="udp", address="203.0.113.11", port=51820)],
        capabilities=["legacy"],
    )
    report = discover_relays([relay], required_protocol_version="v1", endpoint_probe=lambda _endpoint: True)
    ok = report.health[0].state == "incompatible" and not report.usable_candidates()
    return HelperSmokeResult("relay-fabric-negative", ok, f"state={report.health[0].state}")


def _smoke_transport_boundary_helper() -> HelperSmokeResult:
    addon = GatherlinkSocks5Addon(policy=Socks5Policy.allow(hosts=["example.test"], ports=[443]))
    try:
        asyncio.run(
            addon.on_connect(type("Flow", (), {"dst": type("Dst", (), {"host": "example.test", "port": 443})()})())
        )
    except MissingGatherlinkTransportError:
        return HelperSmokeResult("transport-boundary", True, "production helper requires Gatherlink transport")
    return HelperSmokeResult("transport-boundary", False, "production helper bypassed Gatherlink transport")
