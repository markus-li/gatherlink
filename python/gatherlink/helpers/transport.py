"""Shared helper transport abstractions for stream-oriented helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class MissingGatherlinkTransportError(RuntimeError):
    """Raised when a production helper is started without a Gatherlink service transport."""


@dataclass(frozen=True)
class HelperStreamTarget:
    """Explicit remote stream target requested by a helper policy."""

    host: str
    port: int


@dataclass(frozen=True)
class HelperStream:
    """Opened helper stream represented as asyncio reader/writer pair."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    bound_host: str
    bound_port: int


class HelperStreamTransport:
    """Interface for opening helper streams over Gatherlink service transport."""

    async def open_stream(self, target: HelperStreamTarget, *, timeout_seconds: float) -> HelperStream:
        """Open a helper stream to an explicitly allowed remote target."""
        raise NotImplementedError


class UnconfiguredGatherlinkStreamTransport(HelperStreamTransport):
    """Fail-closed helper transport used until a Gatherlink service adapter is supplied."""

    async def open_stream(self, target: HelperStreamTarget, *, timeout_seconds: float) -> HelperStream:
        """Fail loudly instead of silently bypassing Gatherlink transport."""
        raise MissingGatherlinkTransportError(
            "helper requires a Gatherlink service stream transport; use lab-only direct transport for local smoke tests"
        )


class LabDirectTcpStreamTransport(HelperStreamTransport):
    """
    Lab-only direct TCP stream transport.

    This exists to test helper protocol, counters, and lifecycle without a
    remote peer. Production helpers should receive a transport adapter that
    frames streams over a configured Gatherlink UDP service.
    """

    async def open_stream(self, target: HelperStreamTarget, *, timeout_seconds: float) -> HelperStream:
        """Open a direct TCP connection for local lab/smoke use."""
        reader, writer = await asyncio.wait_for(asyncio.open_connection(target.host, target.port), timeout_seconds)
        sock = writer.get_extra_info("socket")
        sockname = sock.getsockname() if sock else ("::", 0)
        return HelperStream(reader=reader, writer=writer, bound_host=sockname[0], bound_port=sockname[1])
