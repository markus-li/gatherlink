from __future__ import annotations

import asyncio

from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.helpers.transport import HelperStreamTarget
from gatherlink.helpers.udp_stream import GatherlinkUdpStreamExit, GatherlinkUdpStreamTransport, _encode_frame


class MemorySink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    def write(self, event: DiagnosticEvent) -> None:
        self.events.append(event)


def test_gatherlink_udp_stream_transport_moves_bytes_through_companion_exit() -> None:
    async def scenario() -> None:
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

        stream.writer.write(b"hello over gatherlink")
        await stream.writer.drain()
        stream.writer.write_eof()
        response = await asyncio.wait_for(stream.reader.read(1024), timeout=1)
        stream.writer.close()
        await stream.writer.wait_closed()

        udp_exit.close()
        target.close()
        await target.wait_closed()

        assert response == b"HELLO OVER GATHERLINK"

    asyncio.run(scenario())


def test_gatherlink_udp_stream_exit_emits_denied_target_diagnostic() -> None:
    async def scenario() -> None:
        sink = MemorySink()
        bus = DiagnosticsBus(sinks=[sink])
        exit_helper = GatherlinkUdpStreamExit(
            listen_host="127.0.0.1",
            listen_port=0,
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({1}),
            diagnostics_bus=bus,
        )
        udp_exit = await exit_helper.start()
        exit_port = udp_exit.get_extra_info("sockname")[1]

        loop = asyncio.get_running_loop()
        client_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=("127.0.0.1", 0),
        )
        client_transport.sendto(
            _encode_frame({"type": "open", "sid": "denied-stream", "host": "127.0.0.1", "port": 8080}),
            ("127.0.0.1", exit_port),
        )
        await asyncio.sleep(0.05)
        bus.drain()

        client_transport.close()
        udp_exit.close()

        assert [event.code for event in sink.events] == ["helper.stream.denied"]
        assert sink.events[0].details["stream_id"] == "denied-stream"
        assert sink.events[0].details["target_port"] == 8080

    asyncio.run(scenario())


def test_gatherlink_udp_stream_exit_emits_invalid_frame_diagnostic() -> None:
    async def scenario() -> None:
        sink = MemorySink()
        bus = DiagnosticsBus(sinks=[sink])
        exit_helper = GatherlinkUdpStreamExit(
            listen_host="127.0.0.1",
            listen_port=0,
            diagnostics_bus=bus,
        )
        udp_exit = await exit_helper.start()
        exit_port = udp_exit.get_extra_info("sockname")[1]

        loop = asyncio.get_running_loop()
        client_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=("127.0.0.1", 0),
        )
        client_transport.sendto(b"not-json", ("127.0.0.1", exit_port))
        await asyncio.sleep(0.05)
        bus.drain()

        client_transport.close()
        udp_exit.close()

        assert [event.code for event in sink.events] == ["helper.stream.invalid_frame"]
        assert sink.events[0].helper == "udp_stream"

    asyncio.run(scenario())
