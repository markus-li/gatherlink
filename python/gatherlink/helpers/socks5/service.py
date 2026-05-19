"""
Optional SOCKS5 helper for metadata-aware proxy use cases.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from asyncio_socks_server import Addon, Address, Connection, Flow, Server

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
class SocksConnectDecision:
    """Policy decision for one SOCKS5 TCP CONNECT request."""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class Socks5Policy:
    """
    Conservative SOCKS5 helper policy.

    Empty allow-lists deny traffic. Operators must deliberately choose target
    hosts and ports so the helper does not become an accidental open proxy.
    """

    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    allowed_ports: frozenset[int] = field(default_factory=frozenset)
    connection_timeout_seconds: float = 10.0

    @classmethod
    def allow(cls, *, hosts: Iterable[str], ports: Iterable[int]) -> Socks5Policy:
        """Build a policy from explicit host and port allow-lists."""
        return cls(
            allowed_hosts=frozenset(host.lower() for host in hosts),
            allowed_ports=frozenset(ports),
        )

    def decide_connect(self, host: str, port: int) -> SocksConnectDecision:
        """Return whether a CONNECT target is allowed by policy."""
        normalized_host = host.lower()
        if normalized_host not in self.allowed_hosts:
            return SocksConnectDecision(False, f"target host is not allowed: {host}")
        if port not in self.allowed_ports:
            return SocksConnectDecision(False, f"target port is not allowed: {port}")
        return SocksConnectDecision(True, "allowed")


@dataclass
class Socks5ConnectionStats:
    """Runtime counters for helper diagnostics."""

    accepted: int = 0
    denied: int = 0
    opened: int = 0
    failed: int = 0
    closed: int = 0
    bytes_up: int = 0
    bytes_down: int = 0


class Socks5ExitConnector:
    """Companion exit connector interface used by the SOCKS5 addon."""

    async def open(self, host: str, port: int, *, timeout_seconds: float) -> Connection:
        """Open the remote side of an allowed SOCKS5 TCP CONNECT."""
        raise NotImplementedError


class GatherlinkServiceExitConnector(Socks5ExitConnector):
    """SOCKS5 exit connector backed by an explicit Gatherlink stream transport."""

    def __init__(self, transport: HelperStreamTransport | None = None) -> None:
        self.transport = transport or UnconfiguredGatherlinkStreamTransport()

    async def open(self, host: str, port: int, *, timeout_seconds: float) -> Connection:
        """Open the CONNECT target through Gatherlink service transport."""
        stream = await self.transport.open_stream(HelperStreamTarget(host, port), timeout_seconds=timeout_seconds)
        return Connection(reader=stream.reader, writer=stream.writer, address=Address(stream.bound_host, stream.bound_port))


class LabDirectTcpExitConnector(GatherlinkServiceExitConnector):
    """
    Lab-only direct TCP exit connector.

    Production SOCKS helpers should use ``GatherlinkServiceExitConnector`` with
    a real Gatherlink service transport adapter. This class exists for local
    smoke tests where both ends run in one process.
    """

    def __init__(self) -> None:
        super().__init__(LabDirectTcpStreamTransport())


class GatherlinkSocks5Addon(Addon):
    """SOCKS5 server addon that applies Gatherlink policy and exit behavior."""

    def __init__(
        self,
        *,
        policy: Socks5Policy,
        exit_connector: Socks5ExitConnector | None = None,
        diagnostics_bus: DiagnosticsBus | None = None,
    ) -> None:
        self.policy = policy
        self.exit_connector = exit_connector or GatherlinkServiceExitConnector()
        self.diagnostics_bus = diagnostics_bus
        self.stats = Socks5ConnectionStats()

    async def on_connect(self, flow: Flow) -> Connection | None:
        """Approve and open TCP CONNECT requests through the configured exit connector."""
        decision = self.policy.decide_connect(flow.dst.host, flow.dst.port)
        if not decision.allowed:
            self.stats.denied += 1
            logger.warning("SOCKS5 CONNECT denied", extra={"target": f"{flow.dst.host}:{flow.dst.port}"})
            self._publish_event(
                code="socks.exit_denied",
                severity="warning",
                message="SOCKS5 CONNECT denied by policy",
                target=f"{flow.dst.host}:{flow.dst.port}",
                reason=decision.reason,
            )
            raise PermissionError(decision.reason)
        self.stats.accepted += 1
        try:
            connection = await self.exit_connector.open(
                flow.dst.host,
                flow.dst.port,
                timeout_seconds=self.policy.connection_timeout_seconds,
            )
        except (OSError, TimeoutError, RuntimeError) as exc:
            self.stats.failed += 1
            self._publish_event(
                code="socks.exit_unreachable",
                severity="warning",
                message="SOCKS5 CONNECT target unreachable",
                target=f"{flow.dst.host}:{flow.dst.port}",
                error=str(exc),
            )
            raise
        self.stats.opened += 1
        self._publish_event(
            code="helper.stream.opened",
            message="SOCKS5 CONNECT stream opened",
            target=f"{flow.dst.host}:{flow.dst.port}",
        )
        return connection

    async def on_flow_close(self, flow: Flow) -> None:
        """Record byte counters when a SOCKS flow closes."""
        self.stats.closed += 1
        self.stats.bytes_up += int(getattr(flow, "bytes_up", 0))
        self.stats.bytes_down += int(getattr(flow, "bytes_down", 0))
        self._publish_event(
            code="helper.stream.closed",
            message="SOCKS5 CONNECT stream closed",
            bytes_up=int(getattr(flow, "bytes_up", 0)),
            bytes_down=int(getattr(flow, "bytes_down", 0)),
        )

    def _publish_event(self, *, code: str, message: str, severity: str = "info", **details: Any) -> None:
        # TODO(helper-diagnostics): Keep SOCKS policy decisions visible through
        # structured diagnostics while keeping the helper independent from core
        # transport policy.
        if self.diagnostics_bus is None:
            return
        self.diagnostics_bus.publish(
            DiagnosticEvent.helper_event(
                code=code,
                helper="socks5",
                severity=severity,
                message=message,
                details=details,
            )
        )


def build_socks5_server(
    *,
    listen_host: str,
    listen_port: int,
    policy: Socks5Policy,
    exit_connector: Socks5ExitConnector | None = None,
    auth: tuple[str, str] | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
) -> Server:
    """Build a SOCKS5 TCP CONNECT server with Gatherlink policy hooks."""
    addon = GatherlinkSocks5Addon(policy=policy, exit_connector=exit_connector, diagnostics_bus=diagnostics_bus)
    return Server(host=listen_host, port=listen_port, addons=[addon], auth=auth)


def run_socks5_server(
    *,
    listen_host: str,
    listen_port: int,
    allowed_hosts: Sequence[str],
    allowed_ports: Sequence[int],
    auth: tuple[str, str] | None = None,
    exit_connector: Socks5ExitConnector | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
) -> None:
    """Run the SOCKS5 helper foreground server."""
    policy = Socks5Policy.allow(hosts=allowed_hosts, ports=allowed_ports)
    server = build_socks5_server(
        listen_host=listen_host,
        listen_port=listen_port,
        policy=policy,
        exit_connector=exit_connector,
        auth=auth,
        diagnostics_bus=diagnostics_bus,
    )
    server.run()


def run_lab_direct_socks5_server(
    *,
    listen_host: str,
    listen_port: int,
    allowed_hosts: Sequence[str],
    allowed_ports: Sequence[int],
    auth: tuple[str, str] | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
) -> None:
    """Run SOCKS5 with lab-only direct TCP exit transport."""
    run_socks5_server(
        listen_host=listen_host,
        listen_port=listen_port,
        allowed_hosts=allowed_hosts,
        allowed_ports=allowed_ports,
        auth=auth,
        exit_connector=LabDirectTcpExitConnector(),
        diagnostics_bus=diagnostics_bus,
    )
