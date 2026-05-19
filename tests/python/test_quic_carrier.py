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
    finally:
        sender.close()
        server_target.close()
        client_target.close()
        await client.stop()
        await server.stop()
    assert returned[0] == server_reply
    return received[0]


@pytest.mark.parametrize("mode", [CarrierMode.QUIC_DATAGRAM, CarrierMode.HTTP3_DATAGRAM])
def test_standard_datagram_carrier_preserves_opaque_gatherlink_packet_bytes(mode: CarrierMode) -> None:
    payload = b"\x91gatherlink-encrypted-packet-bytes\x00\xff"

    received = asyncio.run(_send_and_receive(mode=mode, payload=payload))

    assert received == payload
