"""Python-owned lifecycle for standard carrier adapters."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from threading import Event, Thread

from gatherlink.carriers.quic_datagram import CarrierAdapterConfig, CarrierMode, QuicDatagramCarrierAdapter
from gatherlink.config.runtime import RuntimeConfig, RuntimePathConfig


@dataclass(frozen=True)
class CarrierRuntimeBinding:
    """One adapter started for a non-UDP path before Rust receives runtime DTOs."""

    path: str
    carrier: str
    rust_bind: str
    rust_remote: str
    carrier_endpoint: str


class CarrierSupervisor:
    """
    Start standard-protocol carriers and expose local UDP sockets to Rust.

    The Rust dataplane still executes compact Gatherlink packets over UDP path
    sockets. Python owns the QUIC/H3 wrapper and rewrites the runtime path into
    local UDP endpoints before the Rust DTO bridge sees it.
    """

    def __init__(self, runtime_config: RuntimeConfig) -> None:
        self._runtime_config = runtime_config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._ready = Event()
        self._adapters: list[QuicDatagramCarrierAdapter] = []
        self.bindings: list[CarrierRuntimeBinding] = []

    def start(self) -> RuntimeConfig:
        """Start any non-UDP path carriers and return the Rust-facing config."""
        if not any(path.carrier != "udp" for path in self._runtime_config.paths):
            return self._runtime_config
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, name="gatherlink-carrier-supervisor", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        future = asyncio.run_coroutine_threadsafe(self._start_adapters(), self._loop)
        try:
            return future.result(timeout=5.0)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Stop all carrier adapters and their private event loop."""
        loop = self._loop
        if loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._stop_adapters(), loop)
        future.result(timeout=5.0)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        loop.close()
        self._loop = None
        self._thread = None

    def _run_loop(self) -> None:
        """Run the adapter event loop in a private thread."""
        if self._loop is None:
            return
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    async def _start_adapters(self) -> RuntimeConfig:
        """Start configured carriers and compile their local UDP endpoints."""
        paths: list[RuntimePathConfig] = []
        try:
            for path in self._runtime_config.paths:
                if path.carrier == "udp":
                    paths.append(path)
                    continue
                adapter, updated_path, binding = await self._start_path_adapter(path)
                self._adapters.append(adapter)
                self.bindings.append(binding)
                paths.append(updated_path)
        except Exception:
            await self._stop_adapters()
            raise
        return self._runtime_config.model_copy(update={"paths": paths})

    async def _start_path_adapter(
        self, path: RuntimePathConfig
    ) -> tuple[QuicDatagramCarrierAdapter, RuntimePathConfig, CarrierRuntimeBinding]:
        """Start one carrier adapter and return the Rust-facing path."""
        if path.transport_bind is None:
            raise RuntimeError(f"path {path.name} uses {path.carrier} but has no transport_bind")
        mode = CarrierMode(path.carrier)
        original_bind = _parse_socket_addr(path.transport_bind)
        rust_bind = _reserve_loopback_udp_endpoint(original_bind[0])
        is_listener = path.transport_remote is None or self._runtime_config.role == "server"
        adapter_config = CarrierAdapterConfig(
            mode=mode,
            local_udp_listen=(_loopback_for_host(original_bind[0]), 0),
            local_udp_target=_parse_socket_addr(rust_bind),
            quic_bind=original_bind if is_listener else None,
            quic_remote=None if is_listener else _parse_socket_addr(_require_remote(path)),
            quic_local_port=0 if is_listener else original_bind[1],
            max_datagram_frame_size=path.carrier_max_datagram_size or 1350,
        )
        adapter = QuicDatagramCarrierAdapter(adapter_config)
        await adapter.start()
        rust_remote = _socket_addr_text(adapter.local_udp_addr())
        updated_path = path.model_copy(
            update={
                "carrier": "udp",
                "transport_bind": rust_bind,
                "transport_remote": rust_remote,
            }
        )
        return (
            adapter,
            updated_path,
            CarrierRuntimeBinding(
                path=path.name,
                carrier=path.carrier,
                rust_bind=rust_bind,
                rust_remote=rust_remote,
                carrier_endpoint=path.transport_bind if is_listener else _require_remote(path),
            ),
        )

    async def _stop_adapters(self) -> None:
        """Close carrier adapters in reverse startup order."""
        while self._adapters:
            adapter = self._adapters.pop()
            await adapter.stop()


def _require_remote(path: RuntimePathConfig) -> str:
    """Return a configured carrier remote or raise a readable error."""
    if path.transport_remote is None:
        raise RuntimeError(f"path {path.name} uses {path.carrier} but has no transport_remote")
    return path.transport_remote


def _reserve_loopback_udp_endpoint(host: str) -> str:
    """Reserve and release one local UDP endpoint for Rust's path socket."""
    bind_host = _loopback_for_host(host)
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.bind((bind_host, 0))
        return _socket_addr_text(sock.getsockname())
    finally:
        sock.close()


def _loopback_for_host(host: str) -> str:
    """Pick an address-family-compatible loopback for local Rust/adapter UDP."""
    return "::1" if ":" in host else "127.0.0.1"


def _socket_addr_text(sockaddr: tuple) -> str:
    """Render an IPv4 or bracketed IPv6 socket address."""
    host = str(sockaddr[0])
    port = int(sockaddr[1])
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _parse_socket_addr(value: str) -> tuple[str, int]:
    """Parse IPv4 host:port or bracketed IPv6 [host]:port socket text."""
    if value.startswith("["):
        host, separator, port = value[1:].partition("]:")
        if not separator:
            raise ValueError(f"invalid bracketed IPv6 socket address: {value}")
        return host, int(port)
    host, port = value.rsplit(":", 1)
    return host, int(port)
