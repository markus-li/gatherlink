"""
Optional TCP forwarding helper using reliable stream carriers later.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.helpers.transport import (
    HelperStreamTarget,
    HelperStreamTransport,
    LabDirectTcpStreamTransport,
    UnconfiguredGatherlinkStreamTransport,
)
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TcpForwardConfig:
    """One explicit TCP forwarding rule."""

    listen_host: str
    listen_port: int
    target_host: str
    target_port: int
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 300.0


@dataclass
class TcpForwardStats:
    """Runtime counters for TCP forwarding diagnostics."""

    accepted: int = 0
    connected: int = 0
    failed: int = 0
    closed: int = 0
    bytes_up: int = 0
    bytes_down: int = 0


class TcpForwarder:
    """
    Narrow one-to-one TCP forwarder.

    Production callers must supply a Gatherlink stream transport adapter. Local
    smoke tests may deliberately pass ``LabDirectTcpStreamTransport`` so helper
    lifecycle and counters can be tested without bypassing the boundary by
    accident.
    """

    def __init__(
        self,
        config: TcpForwardConfig,
        *,
        transport: HelperStreamTransport | None = None,
        diagnostics_bus: DiagnosticsBus | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UnconfiguredGatherlinkStreamTransport()
        self.diagnostics_bus = diagnostics_bus
        self.stats = TcpForwardStats()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> asyncio.AbstractServer:
        """Start accepting local TCP connections."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.config.listen_host,
            port=self.config.listen_port,
        )
        return self._server

    async def serve_forever(self) -> None:
        """Run the forwarder until cancelled by its supervisor."""
        server = await self.start()
        async with server:
            await server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.stats.accepted += 1
        try:
            stream = await self.transport.open_stream(
                HelperStreamTarget(self.config.target_host, self.config.target_port),
                timeout_seconds=self.config.connect_timeout_seconds,
            )
        except (OSError, TimeoutError, RuntimeError) as exc:
            self.stats.failed += 1
            self._publish_event(
                code="helper.stream.unreachable",
                severity="warning",
                message="TCP forward helper target unreachable",
                error=str(exc),
            )
            writer.close()
            await writer.wait_closed()
            return

        self.stats.connected += 1
        self._publish_event(code="helper.stream.opened", message="TCP forward helper stream opened")
        try:
            await asyncio.gather(
                self._pipe(reader, stream.writer, "up"),
                self._pipe(stream.reader, writer, "down"),
            )
        finally:
            self.stats.closed += 1
            self._publish_event(
                code="helper.stream.closed",
                message="TCP forward helper stream closed",
                bytes_up=self.stats.bytes_up,
                bytes_down=self.stats.bytes_down,
            )
            stream.writer.close()
            writer.close()
            await asyncio.gather(stream.writer.wait_closed(), writer.wait_closed(), return_exceptions=True)

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str) -> None:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=self.config.idle_timeout_seconds)
            except TimeoutError:
                return
            if not data:
                return
            if direction == "up":
                self.stats.bytes_up += len(data)
            else:
                self.stats.bytes_down += len(data)
            writer.write(data)
            await writer.drain()

    def _publish_event(self, *, code: str, message: str, severity: str = "info", **details: Any) -> None:
        # TODO(helper-diagnostics): Keep helper stream events as lifecycle and
        # policy facts, not per-packet traces, so diagnostics never slow payload
        # movement or drown useful operator signals.
        if self.diagnostics_bus is None:
            return
        self.diagnostics_bus.publish(
            DiagnosticEvent.helper_event(
                code=code,
                helper="tcp_forward",
                severity=severity,
                message=message,
                details={
                    "listen": f"{self.config.listen_host}:{self.config.listen_port}",
                    "target": f"{self.config.target_host}:{self.config.target_port}",
                    **details,
                },
            )
        )


def run_tcp_forwarder(
    config: TcpForwardConfig,
    *,
    transport: HelperStreamTransport | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
) -> None:
    """Run the TCP forwarding helper in the foreground."""
    asyncio.run(TcpForwarder(config, transport=transport, diagnostics_bus=diagnostics_bus).serve_forever())


def run_lab_direct_tcp_forwarder(
    config: TcpForwardConfig,
    *,
    diagnostics_bus: DiagnosticsBus | None = None,
) -> None:
    """Run TCP forwarding with lab-only direct TCP transport."""
    run_tcp_forwarder(config, transport=LabDirectTcpStreamTransport(), diagnostics_bus=diagnostics_bus)
