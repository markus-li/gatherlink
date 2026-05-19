"""UDP-backed stream transport and companion exit for helper services."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from dataclasses import dataclass
from typing import Any

from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus, drain_diagnostics_until_cancelled
from gatherlink.helpers.transport import HelperStream, HelperStreamTarget, HelperStreamTransport
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

FRAME_VERSION = 1
FRAME_CHUNK_BYTES = 900


class GatherlinkUdpStreamTransport(HelperStreamTransport):
    """
    Stream adapter that frames helper bytes into a configured Gatherlink UDP service.

    The service endpoint is the local UDP listen address owned by Gatherlink
    core. Gatherlink carries these datagrams to a companion exit helper on the
    peer. This adapter does not bypass Gatherlink; the only direct UDP socket it
    opens is the local app-side socket that talks to the configured Gatherlink
    service just like any other UDP application.
    """

    def __init__(self, service_host: str, service_port: int) -> None:
        self.service_host = service_host
        self.service_port = service_port

    async def open_stream(self, target: HelperStreamTarget, *, timeout_seconds: float) -> HelperStream:
        """Open a local byte-stream bridge backed by Gatherlink UDP frames."""
        stream_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        udp_protocol = _ClientUdpProtocol()
        udp_transport, _ = await loop.create_datagram_endpoint(lambda: udp_protocol, local_addr=("127.0.0.1", 0))
        service_addr = (self.service_host, self.service_port)
        bridge = _GatherlinkUdpClientBridge(
            stream_id=stream_id,
            target=target,
            udp_transport=udp_transport,
            udp_protocol=udp_protocol,
            service_addr=service_addr,
            timeout_seconds=timeout_seconds,
        )
        server = await asyncio.start_server(bridge.handle_local_stream, "127.0.0.1", 0)
        listen_host, listen_port = server.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(listen_host, listen_port)
        bridge.attach_server(server)
        return HelperStream(reader=reader, writer=writer, bound_host=str(listen_host), bound_port=int(listen_port))


class GatherlinkUdpStreamExit:
    """
    Companion UDP exit helper for stream-oriented helpers.

    It receives frames from a Gatherlink UDP service target, opens explicitly
    requested TCP targets, and sends response frames back to the source address.
    The caller should restrict allowed hosts and ports when exposing this in a
    real environment so it cannot become an open exit.
    """

    def __init__(
        self,
        *,
        listen_host: str,
        listen_port: int,
        allowed_hosts: set[str] | frozenset[str] | None = None,
        allowed_ports: set[int] | frozenset[int] | None = None,
        connect_timeout_seconds: float = 10.0,
        diagnostics_bus: DiagnosticsBus | None = None,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts) if allowed_hosts is not None else None
        self.allowed_ports = frozenset(allowed_ports) if allowed_ports is not None else None
        self.connect_timeout_seconds = connect_timeout_seconds
        self.diagnostics_bus = diagnostics_bus
        self._sessions: dict[tuple[str, tuple[str, int]], _ExitSession] = {}
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> asyncio.DatagramTransport:
        """Start the companion UDP exit listener."""
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _ExitUdpProtocol(self),
            local_addr=(self.listen_host, self.listen_port),
        )
        self._transport = transport
        return transport

    async def serve_forever(self) -> None:
        """Run until cancelled by a supervisor."""
        transport = await self.start()
        diagnostics_task: asyncio.Task[None] | None = None
        if self.diagnostics_bus is not None:
            diagnostics_task = asyncio.create_task(drain_diagnostics_until_cancelled(self.diagnostics_bus))
        try:
            await asyncio.Future()
        finally:
            if diagnostics_task is not None:
                diagnostics_task.cancel()
                await asyncio.gather(diagnostics_task, return_exceptions=True)
            transport.close()

    async def handle_frame(self, frame: dict[str, Any], addr: tuple[str, int]) -> None:
        """Handle one decoded stream frame."""
        stream_id = str(frame.get("sid", ""))
        if not stream_id:
            return
        key = (stream_id, addr)
        frame_type = frame.get("type")
        if frame_type == "open":
            await self._open_session(key, frame, addr)
        elif frame_type == "data":
            session = self._sessions.get(key)
            if session is not None:
                payload = _decode_payload(frame)
                session.writer.write(payload)
                await session.writer.drain()
        elif frame_type == "close":
            await self._close_session_write(key)

    async def _open_session(
        self, key: tuple[str, tuple[str, int]], frame: dict[str, Any], addr: tuple[str, int]
    ) -> None:
        host = str(frame.get("host", ""))
        port = int(frame.get("port", 0))
        if not self._target_allowed(host, port):
            self._publish_helper_event(
                code="helper.stream.denied",
                severity="warning",
                message="helper UDP stream target denied",
                stream_id=key[0],
                peer=addr,
                target_host=host,
                target_port=port,
            )
            self._send({"type": "close", "sid": key[0], "reason": "target denied"}, addr)
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self.connect_timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            self._publish_helper_event(
                code="helper.stream.unreachable",
                severity="warning",
                message="helper UDP stream target unreachable",
                stream_id=key[0],
                peer=addr,
                target_host=host,
                target_port=port,
                error=str(exc),
            )
            self._send({"type": "close", "sid": key[0], "reason": "connect failed"}, addr)
            return
        session = _ExitSession(reader=reader, writer=writer, peer=addr)
        self._sessions[key] = session
        self._publish_helper_event(
            code="helper.stream.opened",
            message="helper UDP stream opened",
            stream_id=key[0],
            peer=addr,
            target_host=host,
            target_port=port,
        )
        task = asyncio.create_task(self._pump_remote_to_udp(key, session))
        task.add_done_callback(_log_task_failure)

    async def _pump_remote_to_udp(self, key: tuple[str, tuple[str, int]], session: _ExitSession) -> None:
        try:
            while True:
                data = await session.reader.read(FRAME_CHUNK_BYTES)
                if not data:
                    break
                self._send(_data_frame(key[0], data), session.peer)
        finally:
            await self._close_session(key, notify=True)

    async def _close_session(self, key: tuple[str, tuple[str, int]], *, notify: bool = False) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        session.writer.close()
        await session.writer.wait_closed()
        self._publish_helper_event(
            code="helper.stream.closed",
            message="helper UDP stream closed",
            stream_id=key[0],
            peer=session.peer,
        )
        if notify:
            self._send({"type": "close", "sid": key[0]}, session.peer)

    async def _close_session_write(self, key: tuple[str, tuple[str, int]]) -> None:
        session = self._sessions.get(key)
        if session is None or session.write_closed:
            return
        session.write_closed = True
        if session.writer.can_write_eof():
            session.writer.write_eof()
            await session.writer.drain()
            return
        session.writer.close()
        await session.writer.wait_closed()

    def _target_allowed(self, host: str, port: int) -> bool:
        if self.allowed_hosts is not None and host.lower() not in self.allowed_hosts:
            return False
        return not (self.allowed_ports is not None and port not in self.allowed_ports)

    def _send(self, frame: dict[str, Any], addr: tuple[str, int]) -> None:
        if self._transport is None:
            return
        self._transport.sendto(_encode_frame(frame), addr)

    def _publish_helper_event(
        self,
        *,
        code: str,
        message: str,
        stream_id: str,
        peer: tuple[str, int],
        severity: str = "info",
        **details: Any,
    ) -> None:
        # DiagnosticsBus.publish is intentionally non-blocking; helper exits
        # should report facts without slowing down stream forwarding.
        if self.diagnostics_bus is None:
            return
        event_details = {"stream_id": stream_id, "peer": f"{peer[0]}:{peer[1]}", **details}
        self.diagnostics_bus.publish(
            DiagnosticEvent.helper_event(
                code=code,
                helper="udp_stream",
                severity=severity,
                message=message,
                details=event_details,
            )
        )


def run_gatherlink_udp_stream_exit(
    *,
    listen_host: str,
    listen_port: int,
    allowed_hosts: set[str] | frozenset[str] | None = None,
    allowed_ports: set[int] | frozenset[int] | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
) -> None:
    """Run the companion UDP stream exit helper in the foreground."""
    exit_helper = GatherlinkUdpStreamExit(
        listen_host=listen_host,
        listen_port=listen_port,
        allowed_hosts=allowed_hosts,
        allowed_ports=allowed_ports,
        diagnostics_bus=diagnostics_bus,
    )
    asyncio.run(exit_helper.serve_forever())


@dataclass
class _ExitSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: tuple[str, int]
    write_closed: bool = False


class _ClientUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self.queue.put_nowait(_decode_frame(data))
        except (ValueError, asyncio.QueueFull):
            logger.warning("helper UDP stream dropped invalid client frame", extra={"source": f"{addr[0]}:{addr[1]}"})


class _ExitUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, exit_helper: GatherlinkUdpStreamExit) -> None:
        self.exit_helper = exit_helper
        self.queue: asyncio.Queue[tuple[dict[str, Any], tuple[str, int]]] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            frame = _decode_frame(data)
        except ValueError:
            logger.warning("helper UDP stream exit dropped invalid frame", extra={"source": f"{addr[0]}:{addr[1]}"})
            self.exit_helper._publish_helper_event(
                code="helper.stream.invalid_frame",
                severity="warning",
                message="helper UDP stream exit dropped invalid frame",
                stream_id="[unknown]",
                peer=addr,
            )
            return
        self.queue.put_nowait((frame, addr))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._handle_frames())
            self.worker.add_done_callback(_log_task_failure)

    async def _handle_frames(self) -> None:
        while not self.queue.empty():
            frame, addr = await self.queue.get()
            await self.exit_helper.handle_frame(frame, addr)


class _GatherlinkUdpClientBridge:
    def __init__(
        self,
        *,
        stream_id: str,
        target: HelperStreamTarget,
        udp_transport: asyncio.DatagramTransport,
        udp_protocol: _ClientUdpProtocol,
        service_addr: tuple[str, int],
        timeout_seconds: float,
    ) -> None:
        self.stream_id = stream_id
        self.target = target
        self.udp_transport = udp_transport
        self.udp_protocol = udp_protocol
        self.service_addr = service_addr
        self.timeout_seconds = timeout_seconds
        self.server: asyncio.AbstractServer | None = None

    def attach_server(self, server: asyncio.AbstractServer) -> None:
        self.server = server

    async def handle_local_stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._send({"type": "open", "sid": self.stream_id, "host": self.target.host, "port": self.target.port})
        try:
            await asyncio.gather(self._local_to_udp(reader), self._udp_to_local(writer))
        finally:
            self._send({"type": "close", "sid": self.stream_id})
            writer.close()
            await writer.wait_closed()
            self.udp_transport.close()
            if self.server is not None:
                self.server.close()

    async def _local_to_udp(self, reader: asyncio.StreamReader) -> None:
        while True:
            data = await reader.read(FRAME_CHUNK_BYTES)
            if not data:
                self._send({"type": "close", "sid": self.stream_id})
                return
            self._send(_data_frame(self.stream_id, data))

    async def _udp_to_local(self, writer: asyncio.StreamWriter) -> None:
        while True:
            frame = await self.udp_protocol.queue.get()
            if frame.get("sid") != self.stream_id:
                continue
            if frame.get("type") == "close":
                return
            if frame.get("type") == "data":
                writer.write(_decode_payload(frame))
                await writer.drain()

    def _send(self, frame: dict[str, Any]) -> None:
        self.udp_transport.sendto(_encode_frame(frame), self.service_addr)


def _data_frame(stream_id: str, payload: bytes) -> dict[str, Any]:
    return {"type": "data", "sid": stream_id, "payload": base64.b64encode(payload).decode("ascii")}


def _encode_frame(frame: dict[str, Any]) -> bytes:
    payload = {"v": FRAME_VERSION, **frame}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _decode_frame(data: bytes) -> dict[str, Any]:
    try:
        frame = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid helper stream frame") from exc
    if not isinstance(frame, dict) or frame.get("v") != FRAME_VERSION:
        raise ValueError("unsupported helper stream frame")
    return frame


def _decode_payload(frame: dict[str, Any]) -> bytes:
    payload = frame.get("payload")
    if not isinstance(payload, str):
        return b""
    return base64.b64decode(payload.encode("ascii"))


def _log_task_failure(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("helper UDP stream background task failed", extra={"error": str(exc)})
