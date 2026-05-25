from __future__ import annotations

import asyncio
import socket

import pytest
from gatherlink.carriers import CarrierAdapterConfig, CarrierMode, QuicDatagramCarrierAdapter


def _udp_endpoint() -> tuple[socket.socket, tuple[str, int]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    return sock, sock.getsockname()


async def _send_and_receive(
    *,
    mode: CarrierMode,
    payload: bytes,
) -> bytes:
    server_target, server_target_addr = _udp_endpoint()
    client_target, client_target_addr = _udp_endpoint()
    server = QuicDatagramCarrierAdapter(
        CarrierAdapterConfig(
            mode=mode,
            local_udp_listen=("127.0.0.1", 0),
            local_udp_target=server_target_addr,
            quic_bind=("127.0.0.1", 0),
        )
    )
    await server.start()
    client = QuicDatagramCarrierAdapter(
        CarrierAdapterConfig(
            mode=mode,
            local_udp_listen=("127.0.0.1", 0),
            local_udp_target=client_target_addr,
            quic_remote=server.quic_addr(),
        )
    )
    await client.start()
    await client.wait_ready()
    await server.wait_ready()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setblocking(False)
    try:
        sender.sendto(payload, client.local_udp_addr())
        received = await asyncio.to_thread(server_target.recvfrom, 65535)
        server_reply = b"reply:" + payload
        sender.sendto(server_reply, server.local_udp_addr())
        returned = await asyncio.to_thread(client_target.recvfrom, 65535)
        client_counters = client.counters()
        server_counters = server.counters()
    finally:
        sender.close()
        server_target.close()
        client_target.close()
        await client.stop()
        await server.stop()
    assert returned[0] == server_reply
    assert client_counters["ready"] is True
    assert server_counters["ready"] is True
    assert client_counters["datagrams_sent"] == 1
    assert client_counters["datagrams_received"] == 1
    assert server_counters["datagrams_sent"] == 1
    assert server_counters["datagrams_received"] == 1
    return received[0]


@pytest.mark.parametrize("mode", [CarrierMode.QUIC_DATAGRAM, CarrierMode.HTTP3_DATAGRAM])
def test_standard_datagram_carrier_preserves_opaque_gatherlink_packet_bytes(mode: CarrierMode) -> None:
    payload = b"\x91gatherlink-encrypted-packet-bytes\x00\xff"

    received = asyncio.run(_send_and_receive(mode=mode, payload=payload))

    assert received == payload


def test_standard_datagram_carrier_emits_lifecycle_and_drop_diagnostics() -> None:
    async def run() -> list[str]:
        events = []
        target, target_addr = _udp_endpoint()
        adapter = QuicDatagramCarrierAdapter(
            CarrierAdapterConfig(
                mode=CarrierMode.QUIC_DATAGRAM,
                local_udp_listen=("127.0.0.1", 0),
                local_udp_target=target_addr,
                quic_bind=("127.0.0.1", 0),
                max_datagram_frame_size=8,
                path_name="path-a",
                diagnostic_callback=events.append,
            )
        )
        await adapter.start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setblocking(False)
        try:
            sender.sendto(b"too-large-payload", adapter.local_udp_addr())
            await asyncio.sleep(0.05)
            assert events[-1].path == "path-a"
        finally:
            sender.close()
            target.close()
            await adapter.stop()
        return [event.code for event in events]

    codes = asyncio.run(run())

    assert "carrier.datagram_dropped" in codes
    assert codes[-1] == "carrier.closed"


def test_standard_datagram_carrier_diagnostics_are_best_effort() -> None:
    async def run() -> dict[str, int | bool | str | None]:
        target, target_addr = _udp_endpoint()

        def broken_callback(_event) -> None:
            raise RuntimeError("diagnostic sink is broken")

        adapter = QuicDatagramCarrierAdapter(
            CarrierAdapterConfig(
                mode=CarrierMode.QUIC_DATAGRAM,
                local_udp_listen=("127.0.0.1", 0),
                local_udp_target=target_addr,
                quic_bind=("127.0.0.1", 0),
                max_datagram_frame_size=8,
                diagnostic_callback=broken_callback,
            )
        )
        await adapter.start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setblocking(False)
        try:
            sender.sendto(b"too-large-payload", adapter.local_udp_addr())
            await asyncio.sleep(0.05)
            return adapter.counters()
        finally:
            sender.close()
            target.close()
            await adapter.stop()

    counters = asyncio.run(run())

    assert counters["datagrams_dropped"] == 1
    assert "exceeds max_datagram_frame_size" in str(counters["last_error"])


def test_standard_datagram_carrier_drops_oversized_local_packets() -> None:
    async def run() -> dict[str, int | bool | str | None]:
        target, target_addr = _udp_endpoint()
        adapter = QuicDatagramCarrierAdapter(
            CarrierAdapterConfig(
                mode=CarrierMode.QUIC_DATAGRAM,
                local_udp_listen=("127.0.0.1", 0),
                local_udp_target=target_addr,
                quic_bind=("127.0.0.1", 0),
                max_datagram_frame_size=8,
            )
        )
        await adapter.start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setblocking(False)
        try:
            sender.sendto(b"too-large-payload", adapter.local_udp_addr())
            await asyncio.sleep(0.05)
            return adapter.counters()
        finally:
            sender.close()
            target.close()
            await adapter.stop()

    counters = asyncio.run(run())

    assert counters["datagrams_dropped"] == 1
    assert counters["bytes_dropped"] == len(b"too-large-payload")
    assert "exceeds max_datagram_frame_size" in str(counters["last_error"])
