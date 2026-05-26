from __future__ import annotations

import asyncio

from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.helpers.transport import HelperStreamTarget
from gatherlink.helpers.udp_stream import (
    STREAM_INITIAL_CREDIT_BYTES,
    GatherlinkUdpStreamExit,
    GatherlinkUdpStreamTransport,
    _encode_frame,
    _stream_hint_from_frame,
)


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


def test_helper_stream_target_hints_are_bounded_for_diagnostics() -> None:
    hints = _stream_hint_from_frame(
        {
            "traffic_class": "tcp_ordered",
            "flowlet_key": "stream-" + ("x" * 200),
        }
    )

    assert hints["traffic_class"] == "tcp_ordered"
    assert hints["flowlet_key"].startswith("stream-")
    assert len(hints["flowlet_key"]) == 128


def test_gatherlink_udp_stream_exit_emits_open_stream_hints() -> None:
    async def scenario() -> None:
        async def echo(reader, writer) -> None:
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        sink = MemorySink()
        bus = DiagnosticsBus(sinks=[sink])
        exit_helper = GatherlinkUdpStreamExit(
            listen_host="127.0.0.1",
            listen_port=0,
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({target_port}),
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
            _encode_frame(
                {
                    "type": "open",
                    "sid": "hinted-stream",
                    "host": "127.0.0.1",
                    "port": target_port,
                    "traffic_class": "tcp_ordered",
                    "flowlet_key": "hinted-stream",
                }
            ),
            ("127.0.0.1", exit_port),
        )
        await asyncio.sleep(0.05)
        bus.drain()

        client_transport.close()
        udp_exit.close()
        target.close()
        await target.wait_closed()

        opened = next(event for event in sink.events if event.code == "helper.stream.opened")
        assert opened.details["traffic_class"] == "tcp_ordered"
        assert opened.details["flowlet_key"] == "hinted-stream"

    asyncio.run(scenario())


def test_gatherlink_udp_stream_exit_reports_close_credit_and_backlog_facts() -> None:
    async def scenario() -> None:
        async def echo(reader, writer) -> None:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        sink = MemorySink()
        bus = DiagnosticsBus(sinks=[sink])
        exit_helper = GatherlinkUdpStreamExit(
            listen_host="127.0.0.1",
            listen_port=0,
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({target_port}),
            diagnostics_bus=bus,
        )
        udp_exit = await exit_helper.start()
        exit_port = udp_exit.get_extra_info("sockname")[1]
        transport = GatherlinkUdpStreamTransport("127.0.0.1", exit_port)
        stream = await transport.open_stream(
            HelperStreamTarget("127.0.0.1", target_port, traffic_class="tcp_ordered"),
            timeout_seconds=1,
        )

        stream.writer.write(b"stream outcome facts")
        await stream.writer.drain()
        stream.writer.write_eof()
        assert await asyncio.wait_for(stream.reader.read(1024), timeout=1) == b"stream outcome facts"
        stream.writer.close()
        await stream.writer.wait_closed()
        await asyncio.sleep(0.05)
        bus.drain()

        udp_exit.close()
        target.close()
        await target.wait_closed()

        credit = next(event for event in sink.events if event.code == "helper.stream.credit")
        closed = next(event for event in sink.events if event.code == "helper.stream.closed")
        assert credit.details["credit_bytes"] == STREAM_INITIAL_CREDIT_BYTES
        assert closed.details["bytes_from_peer"] == len(b"stream outcome facts")
        assert closed.details["bytes_to_peer"] == len(b"stream outcome facts")
        assert closed.details["data_frames_from_peer"] >= 1
        assert closed.details["data_frames_to_peer"] >= 1
        assert "write_backlog_peak_bytes" in closed.details

    asyncio.run(scenario())


def test_gatherlink_udp_stream_exit_reports_reset_frame() -> None:
    async def scenario() -> None:
        async def echo(reader, writer) -> None:
            await reader.read(1024)
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        sink = MemorySink()
        bus = DiagnosticsBus(sinks=[sink])
        exit_helper = GatherlinkUdpStreamExit(
            listen_host="127.0.0.1",
            listen_port=0,
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({target_port}),
            diagnostics_bus=bus,
        )
        udp_exit = await exit_helper.start()
        exit_port = udp_exit.get_extra_info("sockname")[1]

        loop = asyncio.get_running_loop()
        client_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=("127.0.0.1", 0),
        )
        peer = ("127.0.0.1", exit_port)
        client_transport.sendto(
            _encode_frame({"type": "open", "sid": "reset-stream", "host": "127.0.0.1", "port": target_port}),
            peer,
        )
        await asyncio.sleep(0.05)
        client_transport.sendto(_encode_frame({"type": "reset", "sid": "reset-stream", "reason": "client_abort"}), peer)
        await asyncio.sleep(0.05)
        bus.drain()

        client_transport.close()
        udp_exit.close()
        target.close()
        await target.wait_closed()

        reset = next(event for event in sink.events if event.code == "helper.stream.reset")
        closed = next(event for event in sink.events if event.code == "helper.stream.closed")
        assert reset.details["reason"] == "client_abort"
        assert closed.details["reason"] == "reset"

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
