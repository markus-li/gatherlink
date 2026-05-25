"""
QUIC and HTTP/3 DATAGRAM carrier adapters for opaque Gatherlink packets.

The adapter intentionally knows nothing about Gatherlink services, routing,
security policy, or packet contents. It moves already-formed Gatherlink carrier
packet bytes between a local UDP socket, usually owned by the Rust dataplane,
and a standard QUIC/H3 datagram connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from aioquic.asyncio import connect, serve
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DatagramReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import DatagramFrameReceived, HandshakeCompleted, QuicEvent
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from gatherlink.diagnostics.events import DiagnosticEvent


class CarrierMode(StrEnum):
    """Standard datagram carrier modes supported by this adapter."""

    QUIC_DATAGRAM = "quic-datagram"
    HTTP3_DATAGRAM = "http3-datagram"


@dataclass(frozen=True)
class CarrierAdapterConfig:
    """Runtime configuration for one local carrier adapter."""

    mode: CarrierMode
    local_udp_listen: tuple[str, int]
    local_udp_target: tuple[str, int]
    quic_bind: tuple[str, int] | None = None
    quic_remote: tuple[str, int] | None = None
    quic_local_port: int = 0
    server_name: str = "gatherlink.local"
    verify_peer_certificate: bool = False
    max_datagram_frame_size: int = 1350
    path_name: str | None = None
    diagnostic_callback: Callable[[DiagnosticEvent], None] | None = None


@dataclass
class CarrierAdapterCounters:
    """Small lifecycle and datagram counters for operator diagnostics."""

    ready: bool = False
    closed: bool = False
    connect_failed: int = 0
    datagrams_sent: int = 0
    bytes_sent: int = 0
    datagrams_received: int = 0
    bytes_received: int = 0
    datagrams_dropped: int = 0
    bytes_dropped: int = 0
    last_error: str | None = None

    def export_dict(self) -> dict[str, int | bool | str | None]:
        """Return JSON-safe carrier facts for monitor, doctor, and tests."""
        return {
            "ready": self.ready,
            "closed": self.closed,
            "connect_failed": self.connect_failed,
            "datagrams_sent": self.datagrams_sent,
            "bytes_sent": self.bytes_sent,
            "datagrams_received": self.datagrams_received,
            "bytes_received": self.bytes_received,
            "datagrams_dropped": self.datagrams_dropped,
            "bytes_dropped": self.bytes_dropped,
            "last_error": self.last_error,
        }


class QuicDatagramCarrierAdapter:
    """
    Bridge opaque local UDP packets over QUIC DATAGRAM or HTTP/3 DATAGRAM.

    `quic_remote=None` starts a listening endpoint. A remote address starts a
    client endpoint. Both sides can forward local UDP packets once a connection
    exists, which keeps the Gatherlink packet direction independent from the
    side that initiated the standard QUIC connection.
    """

    def __init__(self, config: CarrierAdapterConfig) -> None:
        self.config = config
        self._connection_ready = asyncio.Event()
        self._local_udp_transport: asyncio.DatagramTransport | None = None
        self._local_emit_transport: asyncio.DatagramTransport | None = None
        self._server: Any | None = None
        self._client_context: Any | None = None
        self._client_protocol: _BaseCarrierProtocol | None = None
        self._active_protocols: list[_BaseCarrierProtocol] = []
        self._server_cert_dir: tempfile.TemporaryDirectory[str] | None = None
        self._counters = CarrierAdapterCounters()

    async def start(self) -> None:
        """Start the local UDP side and QUIC/H3 side."""
        loop = asyncio.get_running_loop()
        try:
            self._local_udp_transport, _ = await loop.create_datagram_endpoint(
                lambda: _LocalUdpProtocol(self._handle_local_udp_datagram),
                local_addr=self.config.local_udp_listen,
            )
            self._local_emit_transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                local_addr=("::" if ":" in self.config.local_udp_target[0] else "0.0.0.0", 0),
            )
            if self.config.quic_remote is None:
                await self._start_server()
            else:
                await self._start_client()
        except Exception as exc:
            self._counters.connect_failed += 1
            self._counters.last_error = str(exc)
            self._publish_diagnostic(
                code="carrier.connect_failed",
                message=f"{self.config.mode} carrier failed to connect or bind",
                severity="error",
                details={"error": str(exc)},
            )
            await self.stop()
            raise

    async def stop(self) -> None:
        """Close sockets and QUIC state."""
        was_closed = self._counters.closed
        self._counters.closed = True
        if not was_closed:
            self._publish_diagnostic(code="carrier.closed", message=f"{self.config.mode} carrier closed")
        if self._client_context is not None:
            await self._client_context.__aexit__(None, None, None)
            self._client_context = None
        if self._server is not None:
            self._server.close()
            self._server = None
        for protocol in self._active_protocols:
            protocol.close()
        if self._local_udp_transport is not None:
            self._local_udp_transport.close()
            self._local_udp_transport = None
        if self._local_emit_transport is not None:
            self._local_emit_transport.close()
            self._local_emit_transport = None
        if self._server_cert_dir is not None:
            self._server_cert_dir.cleanup()
            self._server_cert_dir = None

    def local_udp_addr(self) -> tuple[str, int]:
        """Return the actual local UDP address the Rust side should send to."""
        if self._local_udp_transport is None:
            raise RuntimeError("carrier adapter is not started")
        socket = self._local_udp_transport.get_extra_info("socket")
        host, port = socket.getsockname()[:2]
        return str(host), int(port)

    def quic_addr(self) -> tuple[str, int]:
        """Return the actual QUIC/H3 address peers should connect to."""
        if self._server is None:
            raise RuntimeError("carrier adapter is not a listening endpoint")
        transport = self._server._transport
        socket = transport.get_extra_info("socket")
        host, port = socket.getsockname()[:2]
        return str(host), int(port)

    async def wait_ready(self, timeout_seconds: float = 3.0) -> None:
        """Wait until at least one QUIC/H3 connection can carry datagrams."""
        await asyncio.wait_for(self._connection_ready.wait(), timeout=timeout_seconds)

    def counters(self) -> dict[str, int | bool | str | None]:
        """Return current carrier lifecycle and datagram counters."""
        return self._counters.export_dict()

    async def _start_server(self) -> None:
        bind = self.config.quic_bind or ("::", 0)
        configuration, self._server_cert_dir = _server_configuration(self.config)
        self._server = await serve(
            bind[0],
            bind[1],
            configuration=configuration,
            create_protocol=self._create_protocol,
        )

    async def _start_client(self) -> None:
        remote = self.config.quic_remote
        if remote is None:
            raise RuntimeError("client carrier requires quic_remote")
        configuration = _client_configuration(self.config)
        self._client_context = connect(
            remote[0],
            remote[1],
            configuration=configuration,
            create_protocol=self._create_protocol,
            wait_connected=True,
            local_port=self.config.quic_local_port,
        )
        self._client_protocol = await self._client_context.__aenter__()
        self._client_protocol.mark_ready()

    def _create_protocol(self, *args: Any, **kwargs: Any) -> _BaseCarrierProtocol:
        protocol_cls: type[_BaseCarrierProtocol]
        if self.config.mode == CarrierMode.HTTP3_DATAGRAM:
            protocol_cls = _Http3DatagramProtocol
        else:
            protocol_cls = _QuicDatagramProtocol
        protocol = protocol_cls(
            *args, on_datagram=self._emit_remote_datagram_to_udp, on_ready=self._mark_ready, **kwargs
        )
        self._active_protocols.append(protocol)
        return protocol

    def _mark_ready(self, protocol: _BaseCarrierProtocol) -> None:
        was_ready = self._counters.ready
        self._counters.ready = True
        self._connection_ready.set()
        if not was_ready:
            self._publish_diagnostic(code="carrier.ready", message=f"{self.config.mode} carrier ready")

    def _handle_local_udp_datagram(self, data: bytes) -> None:
        if len(data) > self.config.max_datagram_frame_size:
            self._counters.datagrams_dropped += 1
            self._counters.bytes_dropped += len(data)
            self._counters.last_error = (
                f"carrier datagram length {len(data)} exceeds max_datagram_frame_size "
                f"{self.config.max_datagram_frame_size}"
            )
            self._publish_diagnostic(
                code="carrier.datagram_dropped",
                message="carrier datagram dropped because it exceeds max size",
                severity="warning",
                details={"bytes": len(data), "max_datagram_frame_size": self.config.max_datagram_frame_size},
            )
            return
        protocol = self._client_protocol or (self._active_protocols[-1] if self._active_protocols else None)
        if protocol is not None:
            protocol.send_carrier_datagram(data)
            self._counters.datagrams_sent += 1
            self._counters.bytes_sent += len(data)
            self._publish_diagnostic(
                code="carrier.datagram_sent",
                message="carrier datagram sent",
                severity="debug",
                details={"bytes": len(data), "datagrams_sent": self._counters.datagrams_sent},
            )
        else:
            self._counters.datagrams_dropped += 1
            self._counters.bytes_dropped += len(data)
            self._counters.last_error = "carrier datagram dropped before connection was ready"
            self._publish_diagnostic(
                code="carrier.datagram_dropped",
                message="carrier datagram dropped before connection was ready",
                severity="warning",
                details={"bytes": len(data)},
            )

    async def _emit_remote_datagram_to_udp(self, data: bytes) -> None:
        if self._local_emit_transport is not None:
            self._local_emit_transport.sendto(data, self.config.local_udp_target)
            self._counters.datagrams_received += 1
            self._counters.bytes_received += len(data)
            self._publish_diagnostic(
                code="carrier.datagram_received",
                message="carrier datagram received",
                severity="debug",
                details={"bytes": len(data), "datagrams_received": self._counters.datagrams_received},
            )

    def _publish_diagnostic(
        self,
        *,
        code: str,
        message: str,
        severity: str = "info",
        details: dict[str, Any] | None = None,
    ) -> None:
        """
        Emit one non-blocking carrier diagnostic when a callback is configured.

        The adapter deliberately treats diagnostics as best-effort. A broken
        sink must never stop carrier packet movement or lifecycle cleanup.
        """
        callback = self.config.diagnostic_callback
        if callback is None:
            return
        merged_details = {
            "carrier": str(self.config.mode),
            "local_udp_listen": f"{self.config.local_udp_listen[0]}:{self.config.local_udp_listen[1]}",
            "local_udp_target": f"{self.config.local_udp_target[0]}:{self.config.local_udp_target[1]}",
            "quic_bind": _format_endpoint(self.config.quic_bind),
            "quic_remote": _format_endpoint(self.config.quic_remote),
            "max_datagram_frame_size": self.config.max_datagram_frame_size,
        }
        if details:
            merged_details.update(details)
        try:
            callback(
                DiagnosticEvent.carrier_event(
                    code=code,
                    message=message,
                    path=self.config.path_name,
                    severity=severity,  # type: ignore[arg-type]
                    details=merged_details,
                )
            )
        except Exception:
            return


class _LocalUdpProtocol(asyncio.DatagramProtocol):
    """Receive local UDP packets from the Rust path socket."""

    def __init__(self, on_datagram: Callable[[bytes], None]) -> None:
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Forward one opaque packet to the standard carrier."""
        self._on_datagram(data)


class _BaseCarrierProtocol(QuicConnectionProtocol):
    """Common protocol behavior for byte-preserving carrier protocols."""

    def __init__(
        self,
        *args: Any,
        on_datagram: Callable[[bytes], Awaitable[None]],
        on_ready: Callable[[_BaseCarrierProtocol], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_carrier_datagram = on_datagram
        self._on_ready = on_ready
        self._delivery_tasks: set[asyncio.Task[None]] = set()

    def mark_ready(self) -> None:
        """Mark this protocol ready for locally generated packets."""
        self._on_ready(self)

    def send_carrier_datagram(self, data: bytes) -> None:
        """Send one opaque Gatherlink packet through this carrier."""
        raise NotImplementedError

    def _deliver_carrier_datagram(self, data: bytes) -> None:
        """Schedule one received carrier datagram for local UDP emission."""
        task = asyncio.create_task(self._on_carrier_datagram(data))
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)


class _QuicDatagramProtocol(_BaseCarrierProtocol):
    """Direct QUIC DATAGRAM carrier protocol."""

    def send_carrier_datagram(self, data: bytes) -> None:
        """Send one opaque Gatherlink packet as a QUIC DATAGRAM frame."""
        self._quic.send_datagram_frame(data)
        self.transmit()

    def quic_event_received(self, event: QuicEvent) -> None:
        """Forward QUIC DATAGRAM payloads back to the local Rust UDP socket."""
        if isinstance(event, HandshakeCompleted):
            self.mark_ready()
        elif isinstance(event, DatagramFrameReceived):
            self._deliver_carrier_datagram(event.data)
        super().quic_event_received(event)


class _Http3DatagramProtocol(_BaseCarrierProtocol):
    """HTTP/3 DATAGRAM carrier protocol."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._h3 = H3Connection(self._quic)
        self._stream_id: int | None = None

    def mark_ready(self) -> None:
        """Open the request stream used to scope HTTP/3 DATAGRAM frames."""
        if self._stream_id is None and self._quic.configuration.is_client:
            self._stream_id = self._quic.get_next_available_stream_id()
            self._h3.send_headers(
                self._stream_id,
                [
                    (b":method", b"CONNECT"),
                    (b":scheme", b"https"),
                    (b":authority", b"gatherlink.local"),
                    (b":path", b"/gatherlink-carrier"),
                    (b":protocol", b"gatherlink-datagram"),
                ],
                end_stream=False,
            )
            self.transmit()
        super().mark_ready()

    def send_carrier_datagram(self, data: bytes) -> None:
        """Send one opaque Gatherlink packet as an HTTP/3 DATAGRAM."""
        if self._stream_id is None:
            return
        self._h3.send_datagram(self._stream_id, data)
        self.transmit()

    def quic_event_received(self, event: QuicEvent) -> None:
        """Forward HTTP/3 DATAGRAM payloads back to the local Rust UDP socket."""
        if isinstance(event, HandshakeCompleted):
            self.mark_ready()
        for h3_event in self._h3.handle_event(event):
            if isinstance(h3_event, HeadersReceived) and self._stream_id is None:
                self._stream_id = h3_event.stream_id
                self.mark_ready()
            elif isinstance(h3_event, DatagramReceived):
                self._deliver_carrier_datagram(h3_event.data)
        # HTTP/3 owns QUIC stream events here. Calling the raw stream base
        # handler creates stream writers for H3 control streams, which makes
        # shutdown noisy and mixes carrier layers.


def _format_endpoint(endpoint: tuple[str, int] | None) -> str | None:
    """Format an endpoint for diagnostics without adding packet meaning."""
    if endpoint is None:
        return None
    return f"{endpoint[0]}:{endpoint[1]}"


def _client_configuration(config: CarrierAdapterConfig) -> QuicConfiguration:
    """Build client-side QUIC configuration for carrier-only use."""
    quic_config = QuicConfiguration(
        is_client=True,
        alpn_protocols=_alpn(config.mode),
        max_datagram_frame_size=config.max_datagram_frame_size,
        server_name=config.server_name,
        verify_mode=ssl.CERT_REQUIRED if config.verify_peer_certificate else ssl.CERT_NONE,
    )
    return quic_config


def _server_configuration(config: CarrierAdapterConfig) -> tuple[QuicConfiguration, tempfile.TemporaryDirectory[str]]:
    """Build server-side QUIC configuration with an ephemeral self-signed cert."""
    quic_config = QuicConfiguration(
        is_client=False,
        alpn_protocols=_alpn(config.mode),
        max_datagram_frame_size=config.max_datagram_frame_size,
    )
    cert_dir = tempfile.TemporaryDirectory()
    cert_path, key_path = _write_ephemeral_cert(Path(cert_dir.name), config.server_name)
    quic_config.load_cert_chain(cert_path, key_path)
    return quic_config, cert_dir


def _alpn(mode: CarrierMode) -> list[str]:
    """Return ALPN protocols for the selected carrier wrapper."""
    if mode == CarrierMode.HTTP3_DATAGRAM:
        return H3_ALPN
    return ["gatherlink-quic-datagram"]


def _write_ephemeral_cert(directory: Path, server_name: str) -> tuple[str, str]:
    """Write an ephemeral self-signed certificate for local/lab carrier tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=7))
    )
    try:
        san = x509.IPAddress(ipaddress.ip_address(server_name))
    except ValueError:
        san = x509.DNSName(server_name)
    cert = builder.add_extension(x509.SubjectAlternativeName([san]), critical=False).sign(key, hashes.SHA256())
    cert_path = directory / "carrier-cert.pem"
    key_path = directory / "carrier-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)
