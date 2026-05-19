from __future__ import annotations

import asyncio

from gatherlink.cli.main import app
from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.helpers.tcp_forward import TcpForwardConfig, TcpForwarder
from gatherlink.helpers.transport import LabDirectTcpStreamTransport
from gatherlink.helpers.udp_stream import GatherlinkUdpStreamExit, GatherlinkUdpStreamTransport
from typer.testing import CliRunner


class MemorySink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    def write(self, event: DiagnosticEvent) -> None:
        self.events.append(event)


def test_tcp_forward_cli_builds_explicit_forwarder(monkeypatch) -> None:
    captured = {}

    def fake_run(config, *, diagnostics_bus=None):
        captured["config"] = config

    monkeypatch.setattr("gatherlink.cli.helpers.run_lab_direct_tcp_forwarder", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "tcp-forward",
            "--listen",
            "127.0.0.1:8080",
            "--target",
            "127.0.0.1:80",
            "--lab-direct",
        ],
    )

    assert result.exit_code == 0
    assert captured["config"].listen_host == "127.0.0.1"
    assert captured["config"].listen_port == 8080
    assert captured["config"].target_host == "127.0.0.1"
    assert captured["config"].target_port == 80


def test_tcp_forward_cli_requires_gatherlink_service_unless_lab_direct(monkeypatch) -> None:
    called = False

    def fake_run(config, *, transport=None, diagnostics_bus=None):
        nonlocal called
        called = True

    monkeypatch.setattr("gatherlink.cli.helpers.run_tcp_forwarder", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "tcp-forward",
            "--listen",
            "127.0.0.1:8080",
            "--target",
            "127.0.0.1:80",
        ],
    )

    assert result.exit_code != 0
    assert "requires --gatherlink-service" in result.output
    assert called is False


def test_tcp_forward_cli_can_use_gatherlink_udp_service_transport(monkeypatch) -> None:
    captured = {}

    def fake_run(config, *, transport=None, diagnostics_bus=None):
        captured["config"] = config
        captured["transport"] = transport

    monkeypatch.setattr("gatherlink.cli.helpers.run_tcp_forwarder", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "tcp-forward",
            "--listen",
            "127.0.0.1:8080",
            "--target",
            "127.0.0.1:80",
            "--gatherlink-service",
            "127.0.0.1:55181",
        ],
    )

    assert result.exit_code == 0
    assert captured["config"].target_port == 80
    assert isinstance(captured["transport"], GatherlinkUdpStreamTransport)
    assert captured["transport"].service_host == "127.0.0.1"
    assert captured["transport"].service_port == 55181


def test_tcp_forward_cli_wires_jsonl_diagnostics(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run(config, *, transport=None, diagnostics_bus=None):
        captured["config"] = config
        captured["diagnostics_bus"] = diagnostics_bus
        diagnostics_bus.publish_warning("tcp forward warning")

    monkeypatch.setattr("gatherlink.cli.helpers.run_tcp_forwarder", fake_run)
    output = tmp_path / "tcp-forward.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "tcp-forward",
            "--listen",
            "127.0.0.1:8080",
            "--target",
            "127.0.0.1:80",
            "--gatherlink-service",
            "127.0.0.1:55181",
            "--diagnostics-jsonl",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert captured["diagnostics_bus"] is not None
    assert '"message":"tcp forward warning"' in output.read_text(encoding="utf-8")


def test_stream_exit_cli_builds_companion_udp_exit(monkeypatch) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("gatherlink.cli.helpers.run_gatherlink_udp_stream_exit", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "stream-exit",
            "--listen",
            "127.0.0.1:55190",
            "--allow-host",
            "127.0.0.1",
            "--allow-port",
            "80",
        ],
    )

    assert result.exit_code == 0
    assert captured["listen_host"] == "127.0.0.1"
    assert captured["listen_port"] == 55190
    assert captured["allowed_hosts"] == frozenset({"127.0.0.1"})
    assert captured["allowed_ports"] == frozenset({80})


def test_stream_exit_cli_wires_jsonl_diagnostics(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        kwargs["diagnostics_bus"].publish_warning("stream exit warning")

    monkeypatch.setattr("gatherlink.cli.helpers.run_gatherlink_udp_stream_exit", fake_run)
    output = tmp_path / "helper-events.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "stream-exit",
            "--listen",
            "127.0.0.1:55190",
            "--allow-host",
            "127.0.0.1",
            "--allow-port",
            "80",
            "--diagnostics-jsonl",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert captured["diagnostics_bus"] is not None
    assert '"message":"stream exit warning"' in output.read_text(encoding="utf-8")


def test_tcp_forwarder_moves_bytes_bidirectionally() -> None:
    async def scenario() -> None:
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
        writer.write(b"hello")
        await writer.drain()
        writer.write_eof()
        response = await reader.read(1024)
        writer.close()
        await writer.wait_closed()

        server.close()
        target.close()
        await server.wait_closed()
        await target.wait_closed()

        assert response == b"HELLO"
        assert forwarder.stats.accepted == 1
        assert forwarder.stats.connected == 1
        assert forwarder.stats.bytes_up == 5
        assert forwarder.stats.bytes_down == 5

    asyncio.run(scenario())


def test_tcp_forwarder_moves_bytes_through_gatherlink_udp_stream_exit() -> None:
    async def scenario() -> None:
        async def echo(reader, writer) -> None:
            data = await reader.read(1024)
            writer.write(data[::-1])
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
        forwarder = TcpForwarder(
            TcpForwardConfig(
                listen_host="127.0.0.1",
                listen_port=0,
                target_host="127.0.0.1",
                target_port=target_port,
                idle_timeout_seconds=1,
            ),
            transport=GatherlinkUdpStreamTransport("127.0.0.1", exit_port),
        )
        server = await forwarder.start()
        listen_port = server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"via-helper-stream")
        await writer.drain()
        writer.write_eof()
        response = await asyncio.wait_for(reader.read(1024), timeout=2)
        writer.close()
        await writer.wait_closed()

        server.close()
        udp_exit.close()
        target.close()
        await server.wait_closed()
        await target.wait_closed()

        assert response == b"maerts-repleh-aiv"
        assert forwarder.stats.accepted == 1
        assert forwarder.stats.connected == 1
        assert forwarder.stats.bytes_up == len(b"via-helper-stream")
        assert forwarder.stats.bytes_down == len(b"via-helper-stream")

    asyncio.run(scenario())


def test_tcp_forwarder_emits_structured_diagnostics() -> None:
    async def scenario() -> None:
        async def echo(reader, writer) -> None:
            data = await reader.read(1024)
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        bus = DiagnosticsBus()
        forwarder = TcpForwarder(
            TcpForwardConfig(
                listen_host="127.0.0.1",
                listen_port=0,
                target_host="127.0.0.1",
                target_port=target_port,
                idle_timeout_seconds=1,
            ),
            transport=LabDirectTcpStreamTransport(),
            diagnostics_bus=bus,
        )
        server = await forwarder.start()
        listen_port = server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"hi")
        await writer.drain()
        assert await reader.read(2) == b"hi"
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)

        server.close()
        target.close()
        await asyncio.gather(server.wait_closed(), target.wait_closed())
        assert bus.queued_events >= 1

    asyncio.run(scenario())


def test_tcp_forwarder_drains_diagnostics_while_serving() -> None:
    async def scenario() -> None:
        async def echo(reader, writer) -> None:
            data = await reader.read(1024)
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        sink = MemorySink()
        bus = DiagnosticsBus(sinks=[sink])
        forwarder = TcpForwarder(
            TcpForwardConfig(
                listen_host="127.0.0.1",
                listen_port=0,
                target_host="127.0.0.1",
                target_port=target_port,
                idle_timeout_seconds=1,
            ),
            transport=LabDirectTcpStreamTransport(),
            diagnostics_bus=bus,
        )
        server = await forwarder.start()
        listen_port = server.sockets[0].getsockname()[1]
        diagnostics_task = asyncio.create_task(forwarder.serve_forever())
        await asyncio.sleep(0)

        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"hi")
        await writer.drain()
        assert await reader.read(2) == b"hi"
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.3)

        diagnostics_task.cancel()
        await asyncio.gather(diagnostics_task, return_exceptions=True)
        server.close()
        target.close()
        await asyncio.gather(server.wait_closed(), target.wait_closed())

        assert any(event.code == "helper.stream.opened" for event in sink.events)

    asyncio.run(scenario())
