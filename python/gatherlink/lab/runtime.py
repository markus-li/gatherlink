"""Runtime helpers for local lab scenarios."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from base64 import b64encode
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event

from gatherlink.carriers import CarrierAdapterConfig, CarrierMode, QuicDatagramCarrierAdapter
from gatherlink.control import ControlCadenceState
from gatherlink.control import metadata as _control_metadata
from gatherlink.control import remote_status as _remote_status
from gatherlink.control import reserved as _reserved_control
from gatherlink.control.announcements import announce_control_metadata
from gatherlink.control.policy import apply_control_policy_to_dataplane
from gatherlink.dataplane.status import (
    merge_control_metadata as _merge_control_metadata,
)
from gatherlink.dataplane.status import (
    merge_disabled_service_errors as _merge_disabled_service_errors,
)
from gatherlink.dataplane.status import (
    named_rust_control_metadata as _named_rust_control_metadata,
)
from gatherlink.dataplane.status import (
    named_rust_path_stats as _named_rust_path_stats,
)
from gatherlink.lab import netns as _netns
from gatherlink.lab.netns import (
    bandwidth_to_bps as _bandwidth_to_bps,
)
from gatherlink.lab.netns import (
    client_namespace as _client_namespace,
)
from gatherlink.lab.netns import (
    lab_qdisc_stats as _lab_qdisc_stats,
)
from gatherlink.lab.netns import (
    namespace_exists as _namespace_exists,
)
from gatherlink.lab.netns import (
    qdisc_delta as _qdisc_delta,
)
from gatherlink.lab.netns import (
    server_namespace as _server_namespace,
)
from gatherlink.lab.scenarios import LabPathConfig, LabScenarioConfig
from gatherlink.paths.capacity import PATH_CAPACITY_DEFAULT_BPS as _PATH_CAPACITY_DEFAULT_BPS
from gatherlink.paths.capacity import PathCapacityDetector
from gatherlink.paths.mtu import detect_runtime_path_mtu as _detect_runtime_path_mtu
from gatherlink.paths.telemetry import DataTrafficLatencyTracker, PathLatencyTracker, take_data_transmit_sample_batch
from gatherlink.protocol import SERVICE_ID_AUTH_CRYPTO as _SERVICE_ID_AUTH_CRYPTO
from gatherlink.protocol import SERVICE_ID_CONTROL_METADATA as _SERVICE_ID_CONTROL_METADATA
from gatherlink.protocol import SERVICE_ID_REMOTE_STATUS as _SERVICE_ID_REMOTE_STATUS
from gatherlink.runtime.services import (
    ServiceIpcError,
    ServiceIpcServer,
    ServiceRecord,
    ServiceRegistry,
    request_service,
    service_name,
)
from gatherlink.scheduling.metrics import path_pressure_from_path_stats as _path_pressure_from_path_stats
from gatherlink.time.sink import (
    SINK_TIME_BOOTSTRAP_ENV as _SINK_TIME_BOOTSTRAP_ENV,
)
from gatherlink.time.sink import (
    encode_sink_time_sample as _encode_sink_time_sample,
)
from gatherlink.time.sink import (
    read_sink_ntp_sample as _read_sink_ntp_sample,
)

_SINK_TIME_ADVERTISE_INTERVAL_SECONDS = 5.0
_NTP_STATUS_REFRESH_INTERVAL_SECONDS = 30.0
_PATH_CAPACITY_CACHE_SCHEMA_VERSION = 1
_PATH_CAPACITY_CACHE_FILE = "path-capacity-cache.json"
_PATH_MTU_RECHECK_INTERVAL_SECONDS = 60.0
_LAB_SCHEDULER_REAPPLY_INTERVAL_SECONDS = 1.0
_LAB_HIDDEN_SINK_IPC_ENV = "GATHERLINK_LAB_HIDDEN_SINK_IPC"
_LAB_REMOTE_STATUS_ENV = "GATHERLINK_LAB_REMOTE_STATUS"
_LAB_REMOTE_STATUS_PROXY_ENV = "GATHERLINK_LAB_REMOTE_STATUS_PROXY"
_LAB_PACKET_LOG_ENV = "GATHERLINK_LAB_PACKET_LOG"
_LAB_DATAPLANE_BURST_CYCLES_ENV = "GATHERLINK_LAB_DATAPLANE_BURST_CYCLES"
_LAB_DATAPLANE_BATCH_SIZE = 512
_LAB_DATAPLANE_BURST_CYCLES = 8
_LAB_DATA_TIMING_SAMPLE_CONTROL_INTERVAL_SECONDS = 1.0

LabCleanupResult = _netns.LabCleanupResult
PathSetupResult = _netns.PathSetupResult
ShapeApplyResult = _netns.ShapeApplyResult
apply_lab_profile = _netns.apply_lab_profile
apply_lab_network_mode = _netns.apply_lab_network_mode
apply_lab_shape = _netns.apply_lab_shape
apply_lab_shape_profile = _netns.apply_lab_shape_profile
apply_lab_sink_view_rates = _netns.apply_lab_sink_view_rates
cleanup_lab_runtime = _netns.cleanup_lab_runtime
clear_lab_shape = _netns.clear_lab_shape
inspect_lab_interfaces = _netns.inspect_lab_interfaces
prepare_lab_runtime = _netns.prepare_lab_runtime
_parse_tc_qdisc_stats = _netns.parse_tc_qdisc_stats


@dataclass(frozen=True)
class ServiceStartResult:
    """Result from starting one background lab service."""

    name: str
    pid: int
    user: str
    pid_file: Path
    log_file: Path
    status: str


@dataclass(frozen=True)
class ServiceStatus:
    """Current background lab service state."""

    running: bool
    pid: int | None
    pid_file: Path
    log_file: Path


@dataclass(frozen=True)
class UdpSendResult:
    """Result from sending UDP traffic into a lab service."""

    target: str
    packets: int
    bytes: int


@dataclass(frozen=True)
class UdpReceiveResult:
    """Result from receiving UDP traffic from a lab service."""

    listen: str
    packets: int
    bytes: int
    payloads: list[str]


@dataclass(frozen=True)
class RustTransportSmokeResult:
    """Result from a production-shaped Rust path transport lab smoke."""

    packets: int
    bytes: int
    paths: int
    forwarded_packets: int
    delivered_packets: int
    client_listen: str
    remote_target: str


@dataclass(frozen=True)
class SharedSinkSmokeResult:
    """Result from a local shared-sink carrier-port smoke."""

    source_count: int
    packets: int
    bytes: int
    paths: int
    sink_transport: str
    remote_target: str


@dataclass(frozen=True)
class StandardCarrierSmokeResult:
    """Result from a standard-protocol carrier adapter smoke."""

    carrier: str
    packets: int
    bytes: int
    client_udp: str
    server_udp: str
    carrier_endpoint: str


@dataclass(frozen=True)
class StandardCarrierProxySmokeResult(StandardCarrierSmokeResult):
    """Result from a standard-protocol carrier smoke through a UDP proxy."""

    proxy: str
    proxy_endpoint: str
    upstream_endpoint: str


@dataclass(frozen=True)
class CarrierComparisonRow:
    """One row in a carrier comparison report."""

    carrier: str
    path: str
    ok: bool
    packets: int = 0
    bytes: int = 0
    detail: str = ""

    def export_dict(self) -> dict[str, str | int | bool]:
        """Return JSON-safe comparison facts."""
        return {
            "carrier": self.carrier,
            "path": self.path,
            "ok": self.ok,
            "packets": self.packets,
            "bytes": self.bytes,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CarrierComparisonReport:
    """Repeatable carrier comparison summary for lab and VM reports."""

    count: int
    rows: tuple[CarrierComparisonRow, ...]

    @property
    def ok(self) -> bool:
        """Return whether all requested carrier comparison rows passed."""
        return all(row.ok for row in self.rows)

    def export_dict(self) -> dict[str, object]:
        """Return JSON-safe carrier comparison facts."""
        return {
            "count": self.count,
            "ok": self.ok,
            "rows": [row.export_dict() for row in self.rows],
        }


def ensure_service_not_root() -> None:
    """Refuse to run Gatherlink service behavior as root."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("refusing to run Gatherlink lab service as root; run as a normal user")


def run_rust_transport_smoke(
    config: LabScenarioConfig,
    *,
    count: int = 3,
    payload: str = "gatherlink-rust-path",
) -> RustTransportSmokeResult:
    """Run a local two-peer smoke using the production Rust path transport."""
    from gatherlink.config.expansion import expand_config
    from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig
    from gatherlink.dataplane.rust_backend import bind_core_dataplane

    path_names = [path.name for path in config.paths] or ["path-a"]
    family = config.paths[0].family if config.paths else "ipv4"
    loopback_host = "::1" if family == "ipv6" else "127.0.0.1"
    socket_family = socket.AF_INET6 if family == "ipv6" else socket.AF_INET
    path_pairs = [(_reserve_udp_endpoint(loopback_host), _reserve_udp_endpoint(loopback_host)) for _path in path_names]
    remote_target = socket.socket(socket_family, socket.SOCK_DGRAM)
    remote_target.bind((loopback_host, 0))
    remote_target.settimeout(2.0)
    remote_target_text = _socket_addr_text(remote_target.getsockname())
    client_config = GatherlinkConfig(
        schema_version=1,
        node=f"{config.name}-client",
        role="client",
        peer=f"{config.name}-server",
        paths=[
            PathConfig(name=name, interface="lo", transport_bind=client, transport_remote=server)
            for name, (client, server) in zip(path_names, path_pairs)
        ],
        services=[
            ServiceConfig(name="udp-main", listen=_socket_addr_text((loopback_host, 0)), target=remote_target_text)
        ],
    )
    server_config = GatherlinkConfig(
        schema_version=1,
        node=f"{config.name}-server",
        role="server",
        paths=[
            PathConfig(name=name, interface="lo", transport_bind=server, transport_remote=client)
            for name, (client, server) in zip(path_names, path_pairs)
        ],
        services=[
            ServiceConfig(name="udp-main", listen=_socket_addr_text((loopback_host, 0)), target=remote_target_text)
        ],
    )
    client = bind_core_dataplane(expand_config(client_config))
    server = bind_core_dataplane(expand_config(server_config))
    app_sender = socket.socket(socket_family, socket.SOCK_DGRAM)
    client_listen = client.service_local_addr("udp-main")
    forwarded_packets = 0
    delivered_packets = 0
    received_bytes = 0
    try:
        for index in range(count):
            packet = f"{payload}-{index}".encode()
            app_sender.sendto(packet, _parse_socket_addr(client_listen))
            forwarded = client.forward_available_for_service("udp-main", 8)
            forwarded_packets += len(forwarded)
            delivered_packets += _drain_rust_path_frames(server)
            received, _source = remote_target.recvfrom(65535)
            received_bytes += len(received)
            if received != packet:
                raise RuntimeError(f"rust transport smoke payload mismatch: expected={packet!r} received={received!r}")
        return RustTransportSmokeResult(
            packets=count,
            bytes=received_bytes,
            paths=len(path_names),
            forwarded_packets=forwarded_packets,
            delivered_packets=delivered_packets,
            client_listen=client_listen,
            remote_target=remote_target_text,
        )
    finally:
        app_sender.close()
        remote_target.close()


def run_shared_sink_transport_smoke(
    config: LabScenarioConfig,
    *,
    count: int = 3,
    payload: str = "gatherlink-shared-sink",
) -> SharedSinkSmokeResult:
    """Run two source peers into one sink carrier port using production Rust transport."""
    from gatherlink.config.expansion import expand_config
    from gatherlink.config.models import GatherlinkConfig, PathConfig, SecurityConfig, ServiceConfig
    from gatherlink.dataplane.rust_backend import bind_core_dataplane

    path_names = [path.name for path in config.paths] or ["path-a"]
    family = config.paths[0].family if config.paths else "ipv4"
    loopback_host = "::1" if family == "ipv6" else "127.0.0.1"
    socket_family = socket.AF_INET6 if family == "ipv6" else socket.AF_INET
    source_a_paths = [_reserve_udp_endpoint(loopback_host) for _path in path_names]
    source_c_paths = [_reserve_udp_endpoint(loopback_host) for _path in path_names]
    sink_paths = [_reserve_udp_endpoint(loopback_host) for _path in path_names]
    remote_target = socket.socket(socket_family, socket.SOCK_DGRAM)
    remote_target.bind((loopback_host, 0))
    remote_target.settimeout(2.0)
    remote_target_text = _socket_addr_text(remote_target.getsockname())
    source_a_target = socket.socket(socket_family, socket.SOCK_DGRAM)
    source_c_target = socket.socket(socket_family, socket.SOCK_DGRAM)
    source_a_target.bind((loopback_host, 0))
    source_c_target.bind((loopback_host, 0))
    source_a_target.settimeout(2.0)
    source_c_target.settimeout(2.0)

    key_a_to_sink = _lab_shared_sink_key(config, "source-a-to-sink")
    key_sink_to_a = _lab_shared_sink_key(config, "sink-to-source-a")
    key_c_to_sink = _lab_shared_sink_key(config, "source-c-to-sink")
    key_sink_to_c = _lab_shared_sink_key(config, "sink-to-source-c")
    source_a_service = ServiceConfig(
        name="udp-main",
        listen=_socket_addr_text((loopback_host, 0)),
        target=_socket_addr_text(source_a_target.getsockname()),
    )
    source_c_service = ServiceConfig(
        name="udp-main",
        listen=_socket_addr_text((loopback_host, 0)),
        target=_socket_addr_text(source_c_target.getsockname()),
    )
    sink_service = ServiceConfig(
        name="udp-main",
        listen=_socket_addr_text((loopback_host, 0)),
        target=remote_target_text,
        return_mode="peer-scoped-source",
    )

    source_a_config = GatherlinkConfig(
        schema_version=1,
        node=f"{config.name}-source-a",
        role="client",
        peer=f"{config.name}-sink",
        paths=[
            PathConfig(name=name, interface="lo", transport_bind=source, transport_remote=sink)
            for name, source, sink in zip(path_names, source_a_paths, sink_paths)
        ],
        security=SecurityConfig(
            mode="static",
            local_receiver_index=101,
            remote_receiver_index=201,
            send_key=key_a_to_sink,
            receive_key=key_sink_to_a,
        ),
        services=[source_a_service],
    )
    source_c_config = GatherlinkConfig(
        schema_version=1,
        node=f"{config.name}-source-c",
        role="client",
        peer=f"{config.name}-sink",
        paths=[
            PathConfig(name=name, interface="lo", transport_bind=source, transport_remote=sink)
            for name, source, sink in zip(path_names, source_c_paths, sink_paths)
        ],
        security=SecurityConfig(
            mode="static",
            local_receiver_index=102,
            remote_receiver_index=202,
            send_key=key_c_to_sink,
            receive_key=key_sink_to_c,
        ),
        services=[source_c_service],
    )
    sink_config = GatherlinkConfig(
        schema_version=1,
        node=f"{config.name}-sink",
        role="server",
        paths=[
            PathConfig(name=name, interface="lo", transport_bind=sink) for name, sink in zip(path_names, sink_paths)
        ],
        security=SecurityConfig(
            mode="static",
            sessions=[
                {
                    "name": "source-a",
                    "local_receiver_index": 201,
                    "remote_receiver_index": 101,
                    "send_key": key_sink_to_a,
                    "receive_key": key_a_to_sink,
                    "services": ["udp-main"],
                },
                {
                    "name": "source-c",
                    "local_receiver_index": 202,
                    "remote_receiver_index": 102,
                    "send_key": key_sink_to_c,
                    "receive_key": key_c_to_sink,
                    "services": ["udp-main"],
                },
            ],
        ),
        services=[sink_service],
    )
    source_a = bind_core_dataplane(expand_config(source_a_config))
    source_c = bind_core_dataplane(expand_config(source_c_config))
    sink = bind_core_dataplane(expand_config(sink_config))
    app_sender = socket.socket(socket_family, socket.SOCK_DGRAM)
    received_bytes = 0
    received_payloads: set[bytes] = set()
    for index in range(count):
        for label, dataplane, listen, reply_target in [
            ("a", source_a, source_a.service_local_addr("udp-main"), source_a_target),
            ("c", source_c, source_c.service_local_addr("udp-main"), source_c_target),
        ]:
            packet = f"{payload}-{label}-{index}".encode()
            app_sender.sendto(packet, _parse_socket_addr(listen))
            dataplane.forward_available_for_service("udp-main", 8)
            _drain_rust_path_frames(sink)
            received, peer_source = remote_target.recvfrom(65535)
            received_bytes += len(received)
            received_payloads.add(received)
            if received != packet:
                raise RuntimeError(f"shared sink payload mismatch: expected={packet!r} received={received!r}")
            reply = f"{payload}-reply-{label}-{index}".encode()
            remote_target.sendto(reply, peer_source)
            sink.forward_available_for_service_nonblocking("udp-main", 8)
            _drain_rust_path_frames(dataplane)
            returned, _returned_source = reply_target.recvfrom(65535)
            received_bytes += len(returned)
            if returned != reply:
                raise RuntimeError(f"shared sink reply mismatch: expected={reply!r} received={returned!r}")

    source_count = 2
    expected_packets = count * source_count
    if len(received_payloads) != expected_packets:
        raise RuntimeError(
            f"shared sink received {len(received_payloads)} unique payloads; expected {expected_packets}"
        )
    return SharedSinkSmokeResult(
        source_count=source_count,
        packets=expected_packets,
        bytes=received_bytes,
        paths=len(path_names),
        sink_transport=sink_paths[0],
        remote_target=remote_target_text,
    )


def run_standard_carrier_smoke(
    carrier: str,
    *,
    count: int = 3,
    payload: str = "gatherlink-carrier-smoke",
) -> StandardCarrierSmokeResult:
    """Verify a standard carrier adapter preserves opaque packet bytes both ways."""
    try:
        mode = CarrierMode(carrier)
    except ValueError as exc:
        supported = ", ".join(mode.value for mode in CarrierMode)
        raise RuntimeError(f"unsupported carrier smoke mode {carrier!r}; supported={supported}") from exc
    return asyncio.run(_run_standard_carrier_smoke(mode=mode, count=count, payload=payload))


def run_standard_carrier_proxy_smoke(
    carrier: str,
    *,
    proxy: str = "traefik",
    count: int = 3,
    payload: str = "gatherlink-carrier-proxy-smoke",
    traefik_bin: str | None = None,
) -> StandardCarrierProxySmokeResult:
    """Verify a standard carrier preserves bytes through a real UDP-capable proxy."""
    try:
        mode = CarrierMode(carrier)
    except ValueError as exc:
        supported = ", ".join(mode.value for mode in CarrierMode)
        raise RuntimeError(f"unsupported carrier proxy smoke mode {carrier!r}; supported={supported}") from exc
    if proxy != "traefik":
        raise RuntimeError(f"unsupported carrier proxy smoke proxy {proxy!r}; supported=traefik")
    resolved_traefik = traefik_bin or shutil.which("traefik")
    if not resolved_traefik:
        raise RuntimeError("traefik binary not found; pass --traefik-bin or install traefik")
    return asyncio.run(
        _run_standard_carrier_proxy_smoke(
            mode=mode,
            proxy=proxy,
            count=count,
            payload=payload,
            traefik_bin=resolved_traefik,
        )
    )


def run_standard_carrier_comparison(
    *,
    count: int = 3,
    payload: str = "gatherlink-carrier-compare",
    include_proxy: bool = False,
    traefik_bin: str | None = None,
) -> CarrierComparisonReport:
    """
    Compare local UDP, direct QUIC DATAGRAM, and HTTP/3 DATAGRAM carrier paths.

    This is intentionally a lab/report primitive. It reuses the existing smoke
    transports so acceptance scripts can compare equivalent byte-preservation
    behavior before moving to shaped or VM-specific carrier tests.
    """
    rows: list[CarrierComparisonRow] = [_carrier_row_from_udp_smoke(count=count, payload=payload)]
    for mode in CarrierMode:
        rows.append(_carrier_row_from_standard_smoke(mode.value, count=count, payload=payload))
    if include_proxy:
        for mode in CarrierMode:
            rows.append(
                _carrier_row_from_proxy_smoke(
                    mode.value,
                    count=count,
                    payload=payload,
                    traefik_bin=traefik_bin,
                )
            )
    return CarrierComparisonReport(count=count, rows=tuple(rows))


def _carrier_row_from_udp_smoke(*, count: int, payload: str) -> CarrierComparisonRow:
    """Return direct UDP comparison facts using the same packet shape."""
    try:
        result = _run_direct_udp_smoke(count=count, payload=payload)
    except Exception as exc:
        return CarrierComparisonRow(carrier="udp", path="direct", ok=False, detail=str(exc))
    return CarrierComparisonRow(
        carrier="udp",
        path="direct",
        ok=True,
        packets=result.packets,
        bytes=result.bytes,
        detail=f"client_udp={result.client_udp} server_udp={result.server_udp}",
    )


def _carrier_row_from_standard_smoke(carrier: str, *, count: int, payload: str) -> CarrierComparisonRow:
    """Return direct standard-carrier comparison facts."""
    try:
        result = run_standard_carrier_smoke(carrier, count=count, payload=payload)
    except Exception as exc:
        return CarrierComparisonRow(carrier=carrier, path="direct", ok=False, detail=str(exc))
    return CarrierComparisonRow(
        carrier=result.carrier,
        path="direct",
        ok=True,
        packets=result.packets,
        bytes=result.bytes,
        detail=f"client_udp={result.client_udp} server_udp={result.server_udp}",
    )


def _carrier_row_from_proxy_smoke(
    carrier: str,
    *,
    count: int,
    payload: str,
    traefik_bin: str | None,
) -> CarrierComparisonRow:
    """Return proxied standard-carrier comparison facts."""
    try:
        result = run_standard_carrier_proxy_smoke(
            carrier,
            proxy="traefik",
            count=count,
            payload=payload,
            traefik_bin=traefik_bin,
        )
    except Exception as exc:
        return CarrierComparisonRow(carrier=carrier, path="traefik", ok=False, detail=str(exc))
    return CarrierComparisonRow(
        carrier=result.carrier,
        path=result.proxy,
        ok=True,
        packets=result.packets,
        bytes=result.bytes,
        detail=f"proxy_endpoint={result.proxy_endpoint} upstream_endpoint={result.upstream_endpoint}",
    )


def _run_direct_udp_smoke(*, count: int, payload: str) -> StandardCarrierSmokeResult:
    """Verify the baseline direct UDP path with the same bidirectional packet count."""
    server_target, server_target_addr = _udp_endpoint_for_smoke()
    client_target, client_target_addr = _udp_endpoint_for_smoke()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setblocking(False)
    received_bytes = 0
    try:
        for index in range(count):
            packet = f"{payload}-udp-{index}".encode()
            sender.sendto(packet, server_target_addr)
            received, _source = server_target.recvfrom(65535)
            if received != packet:
                raise RuntimeError(f"udp carrier comparison mismatch: expected={packet!r} received={received!r}")
            received_bytes += len(received)
            reply = f"{payload}-udp-reply-{index}".encode()
            sender.sendto(reply, client_target_addr)
            returned, _source = client_target.recvfrom(65535)
            if returned != reply:
                raise RuntimeError(f"udp carrier comparison reply mismatch: expected={reply!r} received={returned!r}")
            received_bytes += len(returned)
    finally:
        sender.close()
        server_target.close()
        client_target.close()
    return StandardCarrierSmokeResult(
        carrier="udp",
        packets=count * 2,
        bytes=received_bytes,
        client_udp=_socket_addr_text(client_target_addr),
        server_udp=_socket_addr_text(server_target_addr),
        carrier_endpoint=_socket_addr_text(server_target_addr),
    )


async def _run_standard_carrier_smoke(
    *,
    mode: CarrierMode,
    count: int,
    payload: str,
) -> StandardCarrierSmokeResult:
    """Run the async QUIC/H3 carrier smoke with real sockets."""
    server_target, server_target_addr = _udp_endpoint_for_smoke()
    client_target, client_target_addr = _udp_endpoint_for_smoke()
    server = QuicDatagramCarrierAdapter(
        CarrierAdapterConfig(
            mode=mode,
            local_udp_listen=("127.0.0.1", 0),
            local_udp_target=server_target_addr,
            quic_bind=("127.0.0.1", 0),
        )
    )
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setblocking(False)
    received_bytes = 0
    try:
        await server.start()
        client = QuicDatagramCarrierAdapter(
            CarrierAdapterConfig(
                mode=mode,
                local_udp_listen=("127.0.0.1", 0),
                local_udp_target=client_target_addr,
                quic_remote=server.quic_addr(),
            )
        )
        try:
            await client.start()
            await client.wait_ready()
            await server.wait_ready()
            for index in range(count):
                packet = f"{payload}-{index}".encode()
                sender.sendto(packet, client.local_udp_addr())
                received, _source = await asyncio.to_thread(server_target.recvfrom, 65535)
                if received != packet:
                    raise RuntimeError(
                        f"{mode.value} carrier smoke mismatch: expected={packet!r} received={received!r}"
                    )
                received_bytes += len(received)
                reply = f"{payload}-reply-{index}".encode()
                sender.sendto(reply, server.local_udp_addr())
                returned, _source = await asyncio.to_thread(client_target.recvfrom, 65535)
                if returned != reply:
                    raise RuntimeError(
                        f"{mode.value} carrier smoke reply mismatch: expected={reply!r} received={returned!r}"
                    )
                received_bytes += len(returned)
            return StandardCarrierSmokeResult(
                carrier=mode.value,
                packets=count * 2,
                bytes=received_bytes,
                client_udp=_socket_addr_text(client.local_udp_addr()),
                server_udp=_socket_addr_text(server.local_udp_addr()),
                carrier_endpoint=_socket_addr_text(server.quic_addr()),
            )
        finally:
            await client.stop()
    finally:
        sender.close()
        server_target.close()
        client_target.close()
        await server.stop()


async def _run_standard_carrier_proxy_smoke(
    *,
    mode: CarrierMode,
    proxy: str,
    count: int,
    payload: str,
    traefik_bin: str,
) -> StandardCarrierProxySmokeResult:
    """Run the async carrier smoke with Traefik UDP forwarding in the middle."""
    server_target, server_target_addr = _udp_endpoint_for_smoke()
    client_target, client_target_addr = _udp_endpoint_for_smoke()
    server = QuicDatagramCarrierAdapter(
        CarrierAdapterConfig(
            mode=mode,
            local_udp_listen=("127.0.0.1", 0),
            local_udp_target=server_target_addr,
            quic_bind=("127.0.0.1", 0),
        )
    )
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setblocking(False)
    traefik_process: subprocess.Popen[bytes] | None = None
    received_bytes = 0
    try:
        await server.start()
        proxy_endpoint = ("127.0.0.1", _free_udp_port())
        traefik_process = _start_traefik_udp_proxy(
            traefik_bin=traefik_bin,
            listen=proxy_endpoint,
            upstream=server.quic_addr(),
        )
        client = QuicDatagramCarrierAdapter(
            CarrierAdapterConfig(
                mode=mode,
                local_udp_listen=("127.0.0.1", 0),
                local_udp_target=client_target_addr,
                quic_remote=proxy_endpoint,
            )
        )
        try:
            await client.start()
            await client.wait_ready()
            await server.wait_ready()
            for index in range(count):
                packet = f"{payload}-{proxy}-{index}".encode()
                sender.sendto(packet, client.local_udp_addr())
                received, _source = await asyncio.to_thread(server_target.recvfrom, 65535)
                if received != packet:
                    raise RuntimeError(
                        f"{mode.value} carrier proxy smoke mismatch: expected={packet!r} received={received!r}"
                    )
                received_bytes += len(received)
                reply = f"{payload}-{proxy}-reply-{index}".encode()
                sender.sendto(reply, server.local_udp_addr())
                returned, _source = await asyncio.to_thread(client_target.recvfrom, 65535)
                if returned != reply:
                    raise RuntimeError(
                        f"{mode.value} carrier proxy smoke reply mismatch: expected={reply!r} received={returned!r}"
                    )
                received_bytes += len(returned)
            return StandardCarrierProxySmokeResult(
                carrier=mode.value,
                packets=count * 2,
                bytes=received_bytes,
                client_udp=_socket_addr_text(client.local_udp_addr()),
                server_udp=_socket_addr_text(server.local_udp_addr()),
                carrier_endpoint=_socket_addr_text(proxy_endpoint),
                proxy=proxy,
                proxy_endpoint=_socket_addr_text(proxy_endpoint),
                upstream_endpoint=_socket_addr_text(server.quic_addr()),
            )
        finally:
            await client.stop()
    finally:
        sender.close()
        server_target.close()
        client_target.close()
        if traefik_process is not None:
            _stop_process(traefik_process)
        await server.stop()


def _udp_endpoint_for_smoke() -> tuple[socket.socket, tuple[str, int]]:
    """Open a loopback UDP endpoint used by same-process carrier smokes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    return sock, sock.getsockname()


def _free_udp_port() -> int:
    """Reserve and release one local UDP port for short-lived lab listeners."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_traefik_udp_proxy(
    *,
    traefik_bin: str,
    listen: tuple[str, int],
    upstream: tuple[str, int],
) -> subprocess.Popen[bytes]:
    """Start Traefik as a local UDP layer-4 forwarder for carrier acceptance."""
    workdir = tempfile.TemporaryDirectory(prefix="gatherlink-traefik-smoke-")
    static_config = Path(workdir.name) / "traefik.yml"
    dynamic_config = Path(workdir.name) / "dynamic.yml"
    static_config.write_text(
        "\n".join(
            [
                "entryPoints:",
                "  gatherlink-quic:",
                f'    address: "{listen[0]}:{listen[1]}/udp"',
                "",
                "providers:",
                "  file:",
                f"    filename: {dynamic_config}",
                "",
                "log:",
                "  level: ERROR",
                "",
            ]
        ),
        encoding="utf-8",
    )
    dynamic_config.write_text(
        "\n".join(
            [
                "udp:",
                "  routers:",
                "    gatherlink-quic:",
                "      entryPoints:",
                "        - gatherlink-quic",
                "      service: gatherlink-quic",
                "",
                "  services:",
                "    gatherlink-quic:",
                "      loadBalancer:",
                "        servers:",
                f'          - address: "{upstream[0]}:{upstream[1]}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [traefik_bin, "--configFile", str(static_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process._gatherlink_tempdir = workdir  # type: ignore[attr-defined]
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            workdir.cleanup()
            raise RuntimeError(f"traefik UDP proxy exited during startup: {stderr.strip()}")
        time.sleep(0.05)
    return process


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate one helper process and clean up any attached temp directory."""
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)
    tempdir = getattr(process, "_gatherlink_tempdir", None)
    if tempdir is not None:
        tempdir.cleanup()


def _lab_shared_sink_key(config: LabScenarioConfig, label: str) -> str:
    """Derive deterministic lab-only static keys for shared-sink smokes."""
    digest = sha256(f"gatherlink shared sink static key v1:{config.name}:{label}".encode()).digest()
    return b64encode(digest).decode("ascii")


def _drain_rust_path_frames(dataplane, *, attempts: int = 20) -> int:
    """Drain path frames from a Rust dataplane during a local smoke test."""
    delivered_packets = 0
    for _attempt in range(attempts):
        delivered = dataplane.receive_available_from_paths(32)
        delivered_packets += len(delivered)
        if delivered:
            return delivered_packets
        time.sleep(0.01)
    return delivered_packets


def _reserve_udp_endpoint(host: str = "127.0.0.1") -> str:
    """Reserve and release one loopback UDP endpoint for a same-process smoke."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind((host, 0))
    endpoint = _socket_addr_text(sock.getsockname())
    sock.close()
    return endpoint


def _socket_addr_text(sockaddr: tuple) -> str:
    """Render an IPv4 or bracketed IPv6 socket address for Rust/PyO3 DTO parsing."""
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


@dataclass
class _LabAppSinkState:
    """Small app-facing receiver state for the Rust-backed sink lab process."""

    packets: int = 0
    bytes: int = 0
    last_payload: str = ""
    last_payload_bytes: int = 0
    last_source: str = ""


@dataclass
class _LabControlState:
    """Mutable peer-control facts this lab node advertises over production control metadata."""

    control_metadata: dict[str, object]
    service_disables: dict[int, str]
    path_capacity: dict[str, dict[str, int | str | None]]
    path_mtu: dict[str, dict[str, int | str | None]]
    path_pressure: dict[str, dict[str, int | str | None]]
    path_latency_tracker: PathLatencyTracker
    data_traffic_latency_tracker: DataTrafficLatencyTracker
    pending_data_transmit_samples: list[tuple[int, int, int, int]] = field(default_factory=list)
    remote_status: _remote_status.RemoteStatusState = field(default_factory=_remote_status.RemoteStatusState)
    applied_disabled_services: set[str] = field(default_factory=set)
    next_mtu_check_at: float = 0.0
    next_data_sample_control_at: float = 0.0


def _lab_runtime_config(config: LabScenarioConfig, *, role: str):
    """Build the production Gatherlink config used by one lab node."""
    from gatherlink.config.models import (
        GatherlinkConfig,
        PathConfig,
        PathSchedulerConfig,
        SchedulerConfig,
        SecurityConfig,
        ServiceConfig,
    )

    _listen_host, path_port = _split_host_port(config.traffic.target)
    paths: list[PathConfig] = []
    for lab_path in config.paths:
        handle = _netns.path_handle(config, lab_path)
        bind_ip = lab_path.client_address if role == "client" else lab_path.server_address
        remote_ip = lab_path.server_address if role == "client" else lab_path.client_address
        interface = handle.client_interface if role == "client" else handle.server_interface
        default_capacity = _default_path_capacity_bps(lab_path)
        paths.append(
            PathConfig(
                name=lab_path.name,
                interface=interface,
                source_ip=bind_ip,
                transport_bind=f"{bind_ip}:{path_port}",
                transport_remote=f"{remote_ip}:{path_port}",
                scheduler=PathSchedulerConfig(
                    tx_capacity_bps=default_capacity,
                    rx_capacity_bps=default_capacity,
                    mtu=lab_path.shape.mtu or 1200,
                    latency_us=_lab_path_latency_us(lab_path),
                    reorder_hold_us=_lab_path_reorder_hold_us(config, lab_path),
                ),
            )
        )

    if role == "client":
        service = ServiceConfig(
            name="udp-main",
            listen=config.traffic.listen,
            target=config.traffic.target,
            return_mode="learned-single-source",
        )
    elif role == "server":
        # The server's app-facing target is the lab sink socket. Its own listen
        # port is intentionally ephemeral; reverse test traffic is injected through
        # that bound address via IPC instead of hard-coding another public port.
        service = ServiceConfig(name="udp-main", listen="127.0.0.1:0", target=config.traffic.target)
    else:
        raise ValueError(f"unknown lab Rust role: {role}")

    return GatherlinkConfig(
        schema_version=config.schema_version,
        node=f"{config.name}-{role}",
        role="client" if role == "client" else "server",
        peer=f"{config.name}-server" if role == "client" else f"{config.name}-client",
        security=_lab_runtime_security(config, role=role, security_cls=SecurityConfig),
        scheduler=SchedulerConfig(mode=config.scheduler_mode),
        paths=paths,
        services=[service],
    )


def _lab_path_latency_us(path: LabPathConfig) -> int | None:
    """Return the lab path's configured one-way delay hint, when known."""
    return _duration_to_microseconds(path.shape.delay) if path.shape.delay else None


def _lab_path_reorder_hold_us(config: LabScenarioConfig, path: LabPathConfig) -> int:
    """
    Compile a path reorder hold from lab delay/jitter, capped by node-pair policy.

    `reorder_policies[].max_hold` is a ceiling, not the actual hold time. Clean
    synthetic paths keep the normal ordered-policy minimum so capacity tests do
    not accidentally measure a huge receive buffer.
    """
    if not path.shape.delay and not path.shape.jitter:
        return 0
    delay_us = _duration_to_microseconds(path.shape.delay) if path.shape.delay else 0
    jitter_us = _duration_to_microseconds(path.shape.jitter) if path.shape.jitter else 0
    requested_us = max(2_000, (delay_us + jitter_us) * 5 // 4)
    max_hold_us = _lab_reorder_max_hold_us(config)
    return min(max_hold_us, requested_us) if max_hold_us else requested_us


def _lab_reorder_max_hold_us(config: LabScenarioConfig) -> int:
    """Return the configured lab node-pair reorder ceiling in microseconds."""
    if not config.reorder_policies:
        return 0
    return _duration_to_microseconds(config.reorder_policies[0].max_hold)


def _duration_to_microseconds(value: str) -> int:
    """Parse the small duration vocabulary used by lab configs."""
    text = value.strip().lower()
    units = {
        "us": 1,
        "ms": 1_000,
        "s": 1_000_000,
    }
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return max(0, int(float(text[: -len(suffix)]) * multiplier))
    return max(0, int(float(text) * 1_000_000))


def _lab_runtime_security(config: LabScenarioConfig, *, role: str, security_cls):
    """Return role-specific security material while keeping lab policy outside Rust."""
    if config.security.mode == "none":
        return config.security
    if config.security.mode != "static":
        raise ValueError(f"unsupported lab security mode: {config.security.mode}")

    client_to_server = config.security.send_key or _lab_static_key(config, "client-to-server")
    server_to_client = config.security.receive_key or _lab_static_key(config, "server-to-client")
    if role == "client":
        send_key = client_to_server
        receive_key = server_to_client
    elif role == "server":
        send_key = server_to_client
        receive_key = client_to_server
    else:
        raise ValueError(f"unknown lab Rust role: {role}")
    return security_cls(
        mode="static",
        receiver_index=config.security.receiver_index,
        send_key=send_key,
        receive_key=receive_key,
    )


def _lab_static_key(config: LabScenarioConfig, label: str) -> str:
    """Derive deterministic lab-only static keys when a scenario opts into static crypto."""
    digest = sha256(f"gatherlink lab static key v1:{config.name}:{label}".encode()).digest()
    return b64encode(digest).decode("ascii")


def _run_rust_lab_dataplane(config: LabScenarioConfig, *, role: str) -> None:
    """Run one long-lived lab node using the production Rust dataplane transport."""
    from gatherlink.config.expansion import expand_config
    from gatherlink.dataplane.rust_backend import bind_core_dataplane
    from gatherlink.runtime.reload import hot_reapply_scheduler_from_status
    from gatherlink.scheduling.congestion import CongestionFairnessController
    from gatherlink.scheduling.coordinator import SchedulerPolicyCoordinator

    ensure_service_not_root()
    base_runtime_config = _lab_runtime_config(config, role=role)
    runtime_config = expand_config(base_runtime_config)
    dataplane = bind_core_dataplane(runtime_config)
    # Python owns service scheduling policy. Reserved control metadata is just
    # another service id from Rust's point of view; Python marks it as duplicated
    # across all paths so the executor can stay policy-free.
    dataplane.set_service_scheduler(_SERVICE_ID_CONTROL_METADATA, 0)
    dataplane.set_service_scheduler(_SERVICE_ID_AUTH_CRYPTO, 1)
    dataplane.set_service_scheduler(_SERVICE_ID_REMOTE_STATUS, 1)
    service_record = _ensure_lab_service_record(config) if role == "client" else _ensure_lab_sink_service_record(config)
    stop_event = Event()
    started_at = datetime.now(UTC)
    app_sink_state = _LabAppSinkState()
    path_names = [path.name for path in runtime_config.paths]
    capacity_detector = PathCapacityDetector(
        path_names=path_names,
        direction="tx" if role == "client" else "rx",
        initial_estimates=_initial_path_capacity_estimates(
            config,
            path_names,
            direction="tx" if role == "client" else "rx",
        ),
    )
    control_state = _LabControlState(
        control_metadata=_control_metadata.empty_control_metadata(),
        service_disables={},
        path_capacity=capacity_detector.snapshot(),
        path_mtu=_detect_runtime_path_mtu(
            runtime_config,
            logger=lambda message: print(f"lab service: {message}", flush=True),
        ),
        path_pressure={},
        path_latency_tracker=PathLatencyTracker(path_names),
        data_traffic_latency_tracker=DataTrafficLatencyTracker(
            {path.scheduler.path_id: path.name for path in runtime_config.paths}
        ),
    )
    if role == "client" and os.environ.get(_LAB_REMOTE_STATUS_ENV) == "1":
        control_state.remote_status.request(ttl_seconds=_remote_status.REMOTE_STATUS_REQUEST_TTL_SECONDS)
    app_sink_socket = _open_app_sink_socket(config) if role == "server" else None
    qdisc_side = "local" if role == "client" else "remote"
    qdisc_baseline = _lab_qdisc_stats(config, side=qdisc_side)
    control_cadence = ControlCadenceState()
    next_control_metadata_at = 0.0
    next_scheduler_reapply_at = 0.0
    scheduler_policy_coordinator = SchedulerPolicyCoordinator()
    congestion_controller = CongestionFairnessController()
    last_scheduler_path_stats: dict[str, dict[str, int]] = {}
    ntp_sample = _read_sink_ntp_sample() if role == "server" else None
    next_ntp_status_at = time.monotonic() + _NTP_STATUS_REFRESH_INTERVAL_SECONDS
    reported_disabled_services: set[str] = set()

    def status_payload() -> dict[str, object]:
        return _rust_lab_status_payload(
            dataplane,
            config,
            runtime_config=runtime_config,
            role=role,
            started_at=started_at,
            app_sink_state=app_sink_state,
            qdisc_side=qdisc_side,
            qdisc_baseline=qdisc_baseline,
            running=not stop_event.is_set(),
            control_cadence=control_cadence,
            control_state=control_state,
        )

    def current_path_stats() -> dict[str, dict[str, int]]:
        """Return current Rust path counters merged with lab qdisc drop counters."""
        qdisc_stats = _qdisc_delta(_lab_qdisc_stats(config, side=qdisc_side), qdisc_baseline)
        return _path_stats_with_qdisc(_named_rust_path_stats(dataplane.status_snapshot(), runtime_config), qdisc_stats)

    def reapply_scheduler_from_local_status(path_stats: dict[str, dict[str, int]]) -> None:
        """
        Recompile local scheduler primitives from local status without sending control traffic.

        Control metadata cadence decides how often peers receive facts. Scheduler
        reapply is local Python policy and can run faster so short lab pressure
        tests still exercise drop/queue feedback. Counter-like pressure facts are
        converted to interval deltas before compiling policy; otherwise one early
        burst of qdisc drops would keep suppressing a path for the rest of the
        run even after the active pressure has changed.
        """
        nonlocal last_scheduler_path_stats, runtime_config
        scheduler_path_stats = _scheduler_interval_path_stats(path_stats, last_scheduler_path_stats)
        last_scheduler_path_stats = {path_name: dict(stats) for path_name, stats in path_stats.items()}
        runtime_config = hot_reapply_scheduler_from_status(
            dataplane,
            base_runtime_config,
            runtime_config,
            {
                "control_metadata": control_state.control_metadata,
                "path_stats": scheduler_path_stats,
            },
            scheduler_coordinator=scheduler_policy_coordinator,
            congestion_controller=congestion_controller,
        )
        dataplane.set_service_scheduler(_SERVICE_ID_CONTROL_METADATA, 0)
        dataplane.set_service_scheduler(_SERVICE_ID_AUTH_CRYPTO, 1)
        dataplane.set_service_scheduler(_SERVICE_ID_REMOTE_STATUS, 1)

    def send_reverse_traffic(request: dict[str, object]) -> dict[str, object]:
        if role != "server":
            raise RuntimeError("reverse lab traffic can only be started from the sink service")
        return _send_reverse_through_rust_service(dataplane, request)

    def request_control_cadence(request: dict[str, object]) -> dict[str, object]:
        profile = str(request.get("profile") or "monitor")
        ttl_seconds = float(request.get("ttl_seconds") or 120.0)
        if profile != "monitor":
            raise RuntimeError(f"unsupported control cadence profile: {profile}")
        return control_cadence.request_monitor_profile(ttl_seconds=ttl_seconds)

    def disable_service(request: dict[str, object]) -> dict[str, object]:
        service_id = _resolve_runtime_service_id(runtime_config, str(request.get("service") or "udp-main"))
        reason = str(request.get("reason") or f"peer disabled service id {service_id}")
        control_state.service_disables[service_id] = reason
        print(
            f"lab {role} service: DISABLING peer service id={service_id} reason={reason}",
            flush=True,
        )
        _announce_lab_control_metadata(
            dataplane,
            runtime_config,
            role=role,
            ntp_sample=ntp_sample,
            control_state=control_state,
        )
        return {"service_id": service_id, "reason": reason, "advertised": True}

    commands = {"send-reverse": send_reverse_traffic} if role == "server" else {}
    commands["control-cadence"] = request_control_cadence
    commands["disable-service"] = disable_service
    ipc = ServiceIpcServer(service_record, status=status_payload, stop=stop_event.set, commands=commands)
    ipc.start()
    remote_status_proxy = _start_remote_status_proxy(config, control_state) if role == "client" else None
    try:
        service_addr = dataplane.service_local_addr("udp-main")
        print(
            f"lab {role} service: rust dataplane service={service_addr} "
            f"paths={','.join(path.name for path in runtime_config.paths)} ipc={service_record.ipc_socket}",
            flush=True,
        )
        print(f"lab {role} service: press Ctrl-C to stop", flush=True)
        while not stop_event.is_set():
            if role == "server" and time.monotonic() >= next_ntp_status_at:
                ntp_sample = _read_sink_ntp_sample()
                next_ntp_status_at = time.monotonic() + _NTP_STATUS_REFRESH_INTERVAL_SECONDS
            if time.monotonic() >= next_scheduler_reapply_at:
                path_stats = current_path_stats()
                reapply_scheduler_from_local_status(path_stats)
                next_scheduler_reapply_at = time.monotonic() + _LAB_SCHEDULER_REAPPLY_INTERVAL_SECONDS
            if time.monotonic() >= next_control_metadata_at:
                qdisc_stats = _qdisc_delta(_lab_qdisc_stats(config, side=qdisc_side), qdisc_baseline)
                path_stats = _path_stats_with_qdisc(
                    _named_rust_path_stats(dataplane.status_snapshot(), runtime_config), qdisc_stats
                )
                control_state.path_pressure = _path_pressure_from_path_stats(path_stats)
                capacity_changes = capacity_detector.observe(path_stats, qdisc_stats)
                if capacity_changes:
                    _save_path_capacity_cache(config, capacity_detector.snapshot())
                control_state.path_capacity = capacity_detector.snapshot()
                if time.monotonic() >= control_state.next_mtu_check_at:
                    control_state.path_mtu = _detect_runtime_path_mtu(
                        runtime_config,
                        logger=lambda message: print(f"lab service: {message}", flush=True),
                    )
                    control_state.next_mtu_check_at = time.monotonic() + _PATH_MTU_RECHECK_INTERVAL_SECONDS
                _announce_lab_control_metadata(
                    dataplane,
                    runtime_config,
                    role=role,
                    ntp_sample=ntp_sample,
                    control_state=control_state,
                )
                reapply_scheduler_from_local_status(path_stats)
                traffic_total = _rust_lab_traffic_total(dataplane)
                next_control_metadata_at = time.monotonic() + control_cadence.next_interval(traffic_total)
            did_work = _step_rust_lab_dataplane(dataplane)
            if did_work:
                _drain_lab_data_timing_samples(dataplane, control_state, role=role)
            if (
                control_state.pending_data_transmit_samples
                and time.monotonic() >= control_state.next_data_sample_control_at
            ):
                _announce_lab_control_metadata(
                    dataplane,
                    runtime_config,
                    role=role,
                    ntp_sample=ntp_sample,
                    control_state=control_state,
                )
            did_work = (
                _drain_reserved_service_events(
                    dataplane,
                    runtime_config,
                    control_state,
                    role=role,
                    status_provider=status_payload,
                )
                or did_work
            )
            did_work = _tick_remote_status_request(dataplane, control_state, role=role) or did_work
            _log_new_disabled_services(dataplane, role, reported_disabled_services)
            if app_sink_socket is not None:
                did_work = _drain_app_sink_socket(app_sink_socket, app_sink_state) or did_work
            if not did_work:
                time.sleep(0.01)
    except KeyboardInterrupt:
        print(f"lab {role} service: stopped", flush=True)
    finally:
        ipc.close()
        if remote_status_proxy is not None:
            remote_status_proxy.close()
        if app_sink_socket is not None:
            app_sink_socket.close()


def _step_rust_lab_dataplane(dataplane) -> bool:
    """Run one nonblocking Rust dataplane step for the lab supervisor."""
    if os.environ.get(_LAB_PACKET_LOG_ENV) != "1":
        summary = getattr(dataplane, "run_available_summary", None)
        if callable(summary):
            try:
                forwarded_packets, _forwarded_bytes, delivered_packets, _delivered_bytes = summary(
                    ["udp-main"],
                    _LAB_DATAPLANE_BATCH_SIZE,
                    _lab_dataplane_burst_cycles(),
                )
            except Exception as exc:
                if "disabled" not in str(exc):
                    raise
                print(f"lab service: SERVICE TRAFFIC STOPPED during dataplane summary step: {exc}", flush=True)
                return False
            return bool(forwarded_packets or delivered_packets)

    try:
        forwarded = dataplane.forward_available_for_service_nonblocking("udp-main", 32)
    except Exception as exc:
        if "disabled" not in str(exc):
            raise
        print(f"lab service: SERVICE TRAFFIC STOPPED while forwarding: {exc}", flush=True)
        forwarded = []
    try:
        delivered = dataplane.receive_available_from_paths(32)
    except Exception as exc:
        if "disabled" not in str(exc):
            raise
        print(f"lab service: SERVICE TRAFFIC STOPPED while delivering: {exc}", flush=True)
        delivered = []
    if forwarded:
        for outcome in forwarded:
            print(
                "lab service: rust forwarded "
                f"bytes={outcome.payload_len()} path={outcome.path_id()} target={outcome.target()}",
                flush=True,
            )
    if delivered:
        for outcome in delivered:
            print(
                "lab service: rust delivered "
                f"bytes={outcome.payload_len()} path={outcome.path_id()} target={outcome.target()}",
                flush=True,
            )
    return bool(forwarded or delivered)


def _lab_dataplane_burst_cycles() -> int:
    """
    Return the bounded aggregate-drain cycle count for lab services.

    The default is intentionally conservative for shaped low-speed paths. The
    environment override exists for benchmark experiments so we can separate
    scheduler behavior from lab-supervisor burst pressure without editing Rust
    or changing production service behavior.
    """
    raw = os.environ.get(_LAB_DATAPLANE_BURST_CYCLES_ENV)
    if raw is None:
        return _LAB_DATAPLANE_BURST_CYCLES
    try:
        value = int(raw)
    except ValueError:
        return _LAB_DATAPLANE_BURST_CYCLES
    return max(1, min(value, _LAB_DATAPLANE_BURST_CYCLES))


def _drain_reserved_service_events(
    dataplane,
    runtime_config,
    control_state: _LabControlState,
    *,
    role: str,
    status_provider,
) -> bool:
    """Let Python decode all reserved Gatherlink service traffic delivered by Rust."""
    path_names_by_id = {path.scheduler.path_id: path.name for path in runtime_config.paths}
    local_targets_by_service_id = {service.service_id: service.target for service in runtime_config.services}
    handled = _reserved_control.drain_reserved_service_events(
        dataplane,
        control_state.control_metadata,
        path_names_by_id=path_names_by_id,
        local_targets_by_service_id=local_targets_by_service_id,
        path_latency_tracker=control_state.path_latency_tracker,
        data_traffic_latency_tracker=control_state.data_traffic_latency_tracker,
        local_clock_is_authoritative=role == "server",
        extra_handlers={
            _SERVICE_ID_REMOTE_STATUS: lambda event: _handle_lab_remote_status_event(
                dataplane,
                event,
                control_state,
                role=role,
                status_provider=status_provider,
            )
        },
        logger=lambda message: print(f"lab {role} reserved service: {message}", flush=True),
    )
    _apply_lab_control_policy(dataplane, runtime_config, control_state, role=role)
    return handled > 0


def _drain_lab_data_timing_samples(dataplane, control_state: _LabControlState, *, role: str = "client") -> None:
    """Promote Rust timing facts into the same production control metadata used by services."""
    drain = getattr(dataplane, "drain_data_timing_samples", None)
    if not callable(drain):
        return
    samples = drain()
    if not isinstance(samples, dict):
        return
    control_state.pending_data_transmit_samples.extend(
        control_state.data_traffic_latency_tracker.observe_local_samples(
            samples,
            local_clock_offset_us=_current_lab_clock_offset_us(control_state.control_metadata),
        )
    )
    changed = control_state.data_traffic_latency_tracker.promote_pending_peer_transmit_samples(
        local_clock_offset_us=_current_lab_clock_offset_us(control_state.control_metadata),
        local_clock_is_authoritative=role == "server",
        latency_tracker=control_state.path_latency_tracker,
        rtt_us=_current_lab_clock_rtt_us(control_state.control_metadata),
        clock_error_us=_current_lab_clock_error_us(control_state.control_metadata),
    )
    if changed:
        _control_metadata.merge_control_path_latency(control_state.control_metadata, changed)


def _current_lab_clock_offset_us(control_metadata: dict[str, object]) -> int | None:
    """Return the current local-to-sink clock offset known to the lab control plane."""
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    return _optional_lab_int(internal_clock.get("mean_offset_us") or internal_clock.get("offset_us"))


def _current_lab_clock_rtt_us(control_metadata: dict[str, object]) -> int | None:
    """Return the current RTT sanity value known to the lab control plane."""
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    return _optional_lab_int(internal_clock.get("mean_rtt_us") or internal_clock.get("rtt_us"))


def _current_lab_clock_error_us(control_metadata: dict[str, object]) -> int | None:
    """Return the current clock error budget known to the lab control plane."""
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    return _optional_lab_int(internal_clock.get("error_budget_us"))


def _optional_lab_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tick_remote_status_request(dataplane, control_state: _LabControlState, *, role: str) -> bool:
    """Request a remote status snapshot through the production remote-status helper."""
    if role != "client":
        return False
    return _remote_status.send_request_if_due(dataplane, control_state.remote_status)


def _handle_lab_remote_status_event(
    dataplane,
    event: _reserved_control.ReservedServicePayload,
    control_state: _LabControlState,
    *,
    role: str,
    status_provider,
) -> bool:
    """Handle remote IPC/status payloads with the production decoder path."""
    return _remote_status.handle_event(
        event,
        dataplane=dataplane,
        state=control_state.remote_status,
        peer_name="sink",
        status_provider=status_provider,
        local_can_respond=role == "server",
        logger=lambda message: print(f"lab {role} remote-status: {message}", flush=True),
    )


def _start_remote_status_proxy(config: LabScenarioConfig, control_state: _LabControlState) -> ServiceIpcServer | None:
    """Expose the remote sink under its normal service name while local sink IPC stays hidden."""
    proxy_name = os.environ.get(_LAB_REMOTE_STATUS_PROXY_ENV)
    if not proxy_name:
        return None
    proxy_record = ServiceRegistry().register(
        ServiceRecord(
            name=proxy_name,
            kind="lab",
            manager="process",
            pid=os.getpid(),
            log_file=_log_file(config),
            detached_from_console=True,
            cwd=Path.cwd(),
            metadata={
                "runtime_dir": config.runtime_dir,
                "scenario": config.scenario,
                "security_mode": config.security.mode,
                "role": "remote-status-proxy",
                "remote_status_source": _lab_service_name(config),
            },
        )
    )

    def status() -> dict[str, object]:
        cached = control_state.remote_status.cache.get("sink")
        if cached and isinstance(cached.get("status"), dict):
            status_payload = dict(cached["status"])
            status_payload["remote_proxy"] = {
                "source": _lab_service_name(config),
                "received_at": cached.get("received_at"),
                "request_id": cached.get("request_id"),
                "source_path_id": cached.get("source_path_id"),
            }
            return status_payload
        return {
            "running": False,
            "role": "remote-status-proxy",
            "target": config.traffic.target,
            "remote_proxy": {
                "source": _lab_service_name(config),
                "status": "waiting_for_remote_status",
            },
            "path_stats": {},
            "control_metadata": _control_metadata.empty_control_metadata(),
        }

    proxy = ServiceIpcServer(proxy_record, status=status, stop=lambda: None, commands={})
    proxy.start()
    print(
        f"lab client remote-status: proxy={proxy_record.name} ipc={proxy_record.ipc_socket}",
        flush=True,
    )
    return proxy


def _apply_lab_control_policy(dataplane, runtime_config, control_state: _LabControlState, *, role: str) -> None:
    """Apply shared Python control policy from a lab-owned service loop."""
    _ = runtime_config
    apply_control_policy_to_dataplane(
        dataplane,
        control_state.control_metadata,
        runtime_config=runtime_config,
        applied_disabled_services=control_state.applied_disabled_services,
        logger=lambda message: print(f"lab {role} service: {message}", flush=True),
    )


def _log_new_disabled_services(dataplane, role: str, reported_disabled_services: set[str]) -> None:
    """Emit one loud log line when Rust first reports a peer-disabled service."""
    snapshot = dataplane.status_snapshot()
    disabled_services = snapshot.get("disabled_services", {})
    if not isinstance(disabled_services, dict):
        return
    for service_id, reason in disabled_services.items():
        service_id_text = str(service_id)
        if service_id_text in reported_disabled_services:
            continue
        reported_disabled_services.add(service_id_text)
        print(
            f"lab {role} service: SERVICE DISABLED by peer id={service_id_text} reason={reason}",
            flush=True,
        )


def _announce_lab_control_metadata(
    dataplane,
    runtime_config,
    *,
    role: str,
    ntp_sample,
    control_state: _LabControlState | None = None,
) -> None:
    """Announce shared control metadata and print the lab-visible summary."""
    metadata = (
        control_state.control_metadata if control_state is not None else _control_metadata.empty_control_metadata()
    )
    path_latency = control_state.path_latency_tracker.dirty_snapshot() if control_state is not None else {}
    data_transmit_samples = (
        take_data_transmit_sample_batch(control_state.pending_data_transmit_samples)
        if control_state is not None
        else []
    )
    announcement = announce_control_metadata(
        dataplane,
        runtime_config,
        metadata,
        path_capacity=control_state.path_capacity if control_state is not None else {},
        path_latency=path_latency,
        path_mtu=control_state.path_mtu if control_state is not None else {},
        path_pressure=control_state.path_pressure if control_state is not None else {},
        scheduler_status=(runtime_config.scheduler.mode, runtime_config.scheduler.mode, runtime_config.scheduler.mode),
        data_transmit_samples=data_transmit_samples,
        service_disables=control_state.service_disables if control_state is not None else {},
        ntp_sample=ntp_sample,
        include_sink_time=role == "server",
    )
    if control_state is not None:
        if path_latency:
            control_state.path_latency_tracker.mark_sent()
        if data_transmit_samples:
            control_state.next_data_sample_control_at = (
                time.monotonic() + _LAB_DATA_TIMING_SAMPLE_CONTROL_INTERVAL_SECONDS
            )
    print(
        f"lab {role} service: rust control sent paths={announcement.sent_paths} "
        f"metadata={announcement.path_count} services={announcement.service_count} "
        f"endpoint_assertions={announcement.endpoint_assertion_count} "
        f"scheduler_policies={announcement.scheduler_policy_count} "
        f"service_disables={announcement.service_disable_count} capacity={announcement.capacity_count} "
        f"latency={announcement.latency_count} mtu={announcement.mtu_count} pressure={announcement.pressure_count} "
        f"data_samples={announcement.data_transmit_sample_count} "
        f"data_samples_omitted={announcement.omitted_data_transmit_sample_count} "
        f"sink_time={announcement.sink_time_count} "
        f"bytes={announcement.payload_bytes}",
        flush=True,
    )


def _rust_lab_traffic_total(dataplane) -> int:
    """Return a simple traffic total for control-cadence decisions."""
    snapshot = dataplane.status_snapshot()
    service = dict(snapshot.get("services", {}).get("udp-main", {}))
    return int(service.get("tx_packets", 0)) + int(service.get("rx_packets", 0))


def _rust_lab_status_payload(
    dataplane,
    config: LabScenarioConfig,
    *,
    runtime_config,
    role: str,
    started_at: datetime,
    app_sink_state: _LabAppSinkState,
    qdisc_side: str,
    qdisc_baseline: dict[str, dict[str, int]],
    running: bool,
    control_cadence: ControlCadenceState,
    control_state: _LabControlState,
) -> dict[str, object]:
    """Convert Rust-owned counters into the generic service monitor status shape."""
    snapshot = dataplane.status_snapshot()
    service = dict(snapshot.get("services", {}).get("udp-main", {}))
    control_metadata = _named_rust_control_metadata(snapshot.get("control_metadata", {}), runtime_config)
    _merge_control_metadata(control_metadata, control_state.control_metadata)
    disabled_services = snapshot.get("disabled_services", {})
    if isinstance(disabled_services, dict):
        _merge_disabled_service_errors(control_metadata, disabled_services)
    _control_metadata.refresh_gatherlink_time(control_metadata)
    qdisc_stats = _qdisc_delta(_lab_qdisc_stats(config, side=qdisc_side), qdisc_baseline)
    path_stats = _path_stats_with_qdisc(_named_rust_path_stats(snapshot, runtime_config), qdisc_stats)
    duplicate_packets = max(
        int(service.get("duplicate_packets", 0) or 0),
        _sum_path_counter(path_stats, "duplicate_packets"),
    )
    expected_duplicate_packets = max(
        int(service.get("expected_duplicate_packets", 0) or 0),
        _sum_path_counter(path_stats, "expected_duplicate_packets"),
    )
    unexpected_duplicate_packets = max(
        int(service.get("unexpected_duplicate_packets", 0) or 0),
        _sum_path_counter(path_stats, "unexpected_duplicate_packets"),
    )
    send_failed_packets = _sum_path_counter(path_stats, "send_failed_packets")
    send_failed_bytes = _sum_path_counter(path_stats, "send_failed_bytes")
    fanout_send_failed_packets = _sum_path_counter(path_stats, "fanout_send_failed_packets")
    fanout_send_failed_bytes = _sum_path_counter(path_stats, "fanout_send_failed_bytes")
    return {
        "running": running,
        "listen": dataplane.service_local_addr("udp-main"),
        "target": config.traffic.target,
        "paths": [path.name for path in runtime_config.paths],
        "packets": int(service.get("packets", 0)),
        "bytes": int(service.get("bytes", 0)),
        "tx_packets": int(service.get("tx_packets", 0)),
        "tx_bytes": int(service.get("tx_bytes", 0)),
        "rx_packets": int(service.get("rx_packets", 0)),
        "rx_bytes": int(service.get("rx_bytes", 0)),
        "expected_duplicate_packets": expected_duplicate_packets,
        "unexpected_duplicate_packets": unexpected_duplicate_packets,
        "duplicate_packets": duplicate_packets,
        "send_failed_packets": send_failed_packets,
        "send_failed_bytes": send_failed_bytes,
        "fanout_send_failed_packets": fanout_send_failed_packets,
        "fanout_send_failed_bytes": fanout_send_failed_bytes,
        "missed_packets": _total_missed_packets(path_stats),
        "path_stats": path_stats,
        "control_metadata": control_metadata,
        "remote_status": control_state.remote_status.cache,
        "service_errors": dict(disabled_services) if isinstance(disabled_services, dict) else {},
        "control_cadence": control_cadence.status(),
        "started_at": started_at.isoformat(),
        "role": role,
        "last_payload": app_sink_state.last_payload,
        "last_payload_bytes": app_sink_state.last_payload_bytes,
        "last_source": app_sink_state.last_source,
        "app_packets": app_sink_state.packets,
        "app_bytes": app_sink_state.bytes,
    }


def _open_app_sink_socket(config: LabScenarioConfig) -> socket.socket:
    """Bind the lab sink's app-facing UDP socket that receives decapsulated payloads."""
    host, port = _split_host_port(config.traffic.target)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.setblocking(False)
    return sock


def _drain_app_sink_socket(sock: socket.socket, state: _LabAppSinkState) -> bool:
    """Drain decapsulated UDP payloads emitted by the Rust server dataplane."""
    did_work = False
    while True:
        try:
            payload, source = sock.recvfrom(65535)
        except BlockingIOError:
            return did_work
        did_work = True
        state.packets += 1
        state.bytes += len(payload)
        state.last_payload = payload.decode("utf-8", errors="replace")
        state.last_payload_bytes = len(payload)
        state.last_source = repr(source)
        if os.environ.get(_LAB_PACKET_LOG_ENV) == "1":
            print(
                f"lab sink app: received packet={state.packets} bytes={len(payload)} source={source} "
                f"payload={state.last_payload}",
                flush=True,
            )


def _send_reverse_through_rust_service(dataplane, request: dict[str, object]) -> dict[str, object]:
    """Inject sink-originated lab traffic through the server's app-facing Rust service port."""
    payload_text = str(request.get("payload") or "gatherlink-reverse")
    count = int(request.get("count") or 5)
    interval_seconds = float(request.get("interval_seconds") or 0.05)
    duration_raw = request.get("duration_seconds")
    duration_seconds = float(duration_raw) if duration_raw is not None else None
    bandwidth = request.get("bandwidth")
    payload_size_raw = request.get("payload_size")
    payload_size = int(payload_size_raw) if payload_size_raw is not None else None
    bps = _bandwidth_to_bps(str(bandwidth)) if bandwidth else None
    base_payload = _fixed_payload(payload_text.encode(), payload_size)
    target = _parse_socket_addr(dataplane.service_local_addr("udp-main"))
    packets = 0
    packet_bytes = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if duration_seconds is not None and bps is not None:
            interval = len(base_payload) * 8 / bps
            started = time.monotonic()
            next_send = started
            while time.monotonic() - started < duration_seconds:
                packet_bytes += sock.sendto(base_payload, target)
                packets += 1
                next_send += interval
                if (sleep_for := next_send - time.monotonic()) > 0:
                    time.sleep(sleep_for)
        else:
            for index in range(count):
                packet = base_payload if count == 1 else _indexed_payload(base_payload, index, payload_size)
                packet_bytes += sock.sendto(packet, target)
                packets += 1
                if interval_seconds > 0 and index + 1 < count:
                    time.sleep(interval_seconds)
    return {"target": _format_socket_addr(target), "packets": packets, "bytes": packet_bytes}


def start_lab_service(
    config_path: Path,
    config: LabScenarioConfig,
    *,
    request_remote_status: bool = False,
) -> ServiceStartResult:
    """Start the foreground lab service as an unprivileged background process."""
    extra_env = (
        {
            _LAB_REMOTE_STATUS_ENV: "1",
            _LAB_REMOTE_STATUS_PROXY_ENV: _lab_sink_service_name(config),
        }
        if request_remote_status
        else None
    )
    return _start_lab_process_service(
        config_path,
        config,
        service_name_value=_lab_service_name(config),
        command=_service_command(config, config_path, extra_env=extra_env),
        log_file=_log_file(config),
        metadata_role="forwarder",
        extra_env=extra_env,
    )


def start_lab_sink_service(
    config_path: Path,
    config: LabScenarioConfig,
    *,
    local_ipc: bool = True,
) -> ServiceStartResult:
    """Start the foreground lab sink service as an unprivileged background process."""
    bootstrap_sample = _read_sink_ntp_sample()
    extra_env = (
        {_SINK_TIME_BOOTSTRAP_ENV: _encode_sink_time_sample(bootstrap_sample)}
        if bootstrap_sample is not None and config.paths
        else {}
    )
    service_name_value = _lab_sink_service_name(config)
    metadata_role = "sink"
    if not local_ipc:
        service_name_value = _lab_hidden_sink_service_name(config)
        metadata_role = "sink-hidden"
        extra_env[_LAB_HIDDEN_SINK_IPC_ENV] = "1"
    return _start_lab_process_service(
        config_path,
        config,
        service_name_value=service_name_value,
        command=_sink_service_command(config, config_path, extra_env=extra_env),
        log_file=_sink_log_file(config),
        metadata_role=metadata_role,
        extra_env=extra_env,
    )


def _start_lab_process_service(
    config_path: Path,
    config: LabScenarioConfig,
    *,
    service_name_value: str,
    command: list[str],
    log_file: Path,
    metadata_role: str,
    extra_env: dict[str, str] | None = None,
) -> ServiceStartResult:
    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    existing = _read_service_status(config, service_name_value, log_file)
    if existing.running and existing.pid is not None:
        record = _register_lab_service(
            config_path,
            config,
            existing.pid,
            command,
            service_name_value=service_name_value,
            log_file=log_file,
            metadata_role=metadata_role,
        )
        return ServiceStartResult(
            name=record.name,
            pid=existing.pid,
            user=_service_user(),
            pid_file=existing.pid_file,
            log_file=existing.log_file,
            status="reused",
        )

    with log_file.open("a", encoding="utf-8") as log_handle:
        process_env = {**os.environ, **(extra_env or {})}
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=process_env,
        )
    record = _register_lab_service(
        config_path,
        config,
        process.pid,
        command,
        service_name_value=service_name_value,
        log_file=log_file,
        metadata_role=metadata_role,
    )
    return ServiceStartResult(
        name=record.name,
        pid=process.pid,
        user=_service_user(),
        pid_file=record.pid_file or _pid_file(config),
        log_file=log_file,
        status="started",
    )


def stop_lab_service(config: LabScenarioConfig) -> ServiceStatus:
    """Stop background lab services if they are running."""
    registry = ServiceRegistry()
    for name in [_lab_service_name(config), _lab_sink_service_name(config), _lab_hidden_sink_service_name(config)]:
        with suppress(ValueError, ServiceIpcError):
            registry.close(name)
    return read_service_status(config)


def read_service_status(config: LabScenarioConfig) -> ServiceStatus:
    """Read the background lab service PID and liveness."""
    return _read_service_status(config, _lab_service_name(config), _log_file(config))


def read_sink_service_status(config: LabScenarioConfig) -> ServiceStatus:
    """Read the background lab sink service PID and liveness."""
    return _read_service_status(config, _lab_sink_service_name(config), _sink_log_file(config))


def _read_service_status(config: LabScenarioConfig, name: str, log_file: Path) -> ServiceStatus:
    pid: int | None = None
    running = False
    try:
        record = ServiceRegistry().resolve(name)
    except ValueError:
        record = None
    if record is not None and record.pid_file is not None:
        pid = record.current_pid()
        running = record.is_running()
        return ServiceStatus(running=running, pid=pid, pid_file=record.pid_file, log_file=log_file)

    return ServiceStatus(running=running, pid=pid, pid_file=Path(config.runtime_dir) / "service.pid", log_file=log_file)


def run_udp_forwarder(config: LabScenarioConfig) -> None:
    """Run the foreground client-side lab node on the production Rust dataplane."""
    _run_rust_lab_dataplane(config, role="client")


def send_udp_packets(
    config: LabScenarioConfig,
    *,
    payload: str = "gatherlink-lab",
    count: int = 5,
    interval_seconds: float = 0.05,
    duration_seconds: float | None = None,
    bandwidth: str | None = None,
    payload_size: int | None = None,
    use_namespace: bool = False,
) -> UdpSendResult:
    """Send small UDP payloads into the lab service listener."""
    if use_namespace and config.paths and _namespace_exists(_client_namespace(config)):
        return _send_udp_packets_in_namespace(
            config,
            payload=payload,
            count=count,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
            bandwidth=bandwidth,
            payload_size=payload_size,
        )

    target = _split_host_port(config.traffic.listen)
    family = socket.AF_INET6 if ":" in target[0] else socket.AF_INET
    encoded = payload.encode("utf-8")
    packet_bytes = 0
    bps = _bandwidth_to_bps(bandwidth) if bandwidth else None
    fixed_payload = _fixed_payload(encoded, payload_size)
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        if duration_seconds is not None and bps is not None:
            packets = 0
            interval = len(fixed_payload) * 8 / bps
            started_at = time.monotonic()
            next_send = started_at
            while time.monotonic() - started_at < duration_seconds:
                packet_bytes += sock.sendto(fixed_payload, target)
                packets += 1
                next_send += interval
                if (sleep_for := next_send - time.monotonic()) > 0:
                    time.sleep(sleep_for)
            return UdpSendResult(target=config.traffic.listen, packets=packets, bytes=packet_bytes)

        for index in range(count):
            message = fixed_payload if count == 1 else _indexed_payload(encoded, index, payload_size)
            packet_bytes += sock.sendto(message, target)
            if interval_seconds > 0 and index + 1 < count:
                time.sleep(interval_seconds)
    return UdpSendResult(target=config.traffic.listen, packets=count, bytes=packet_bytes)


def send_udp_packets_from_sink(
    config: LabScenarioConfig,
    *,
    payload: str = "gatherlink-reverse",
    count: int = 5,
    interval_seconds: float = 0.05,
    duration_seconds: float | None = None,
    bandwidth: str | None = None,
    payload_size: int | None = None,
) -> UdpSendResult:
    """Ask the running sink service to emit reverse/reply traffic over learned paths."""
    sink_record = _running_sink_record(config)
    if sink_record is None:
        raise RuntimeError("lab sink service is not running")
    response = request_service(
        sink_record,
        "send-reverse",
        timeout_seconds=max(duration_seconds or 0, 0) + 5.0,
        payload={
            "payload": payload,
            "count": count,
            "interval_seconds": interval_seconds,
            "duration_seconds": duration_seconds,
            "bandwidth": bandwidth,
            "payload_size": payload_size,
        },
    )
    result = response["result"]
    return UdpSendResult(target=str(result["target"]), packets=int(result["packets"]), bytes=int(result["bytes"]))


def request_lab_service_disable(
    config: LabScenarioConfig,
    *,
    side: str,
    service: str = "udp-main",
    reason: str = "peer declined this service",
) -> dict[str, object]:
    """Ask one lab node to advertise a generic service-disable control assertion."""
    record = _running_lab_side_record(config, side)
    if record is None:
        raise RuntimeError(f"lab {side} service is not running")
    response = request_service(
        record,
        "disable-service",
        timeout_seconds=3.0,
        payload={"service": service, "reason": reason},
    )
    return dict(response["result"])


def run_udp_sink(
    config: LabScenarioConfig,
    *,
    count: int | None = None,
    timeout_seconds: float | None = None,
) -> UdpReceiveResult:
    """Receive UDP payloads emitted by the lab service target."""
    listen_host, listen_port = _split_host_port(config.traffic.target)
    family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
    packets = 0
    packet_bytes = 0
    payloads: list[str] = []
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.bind((listen_host, listen_port))
        sock.settimeout(0.2)
        while count is None or packets < count:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                payload, source = sock.recvfrom(65535)
            except TimeoutError:
                continue
            packets += 1
            packet_bytes += len(payload)
            text = payload.decode("utf-8", errors="replace")
            payloads.append(text)
            print(f"lab sink: packet={packets} bytes={len(payload)} source={source} payload={text}", flush=True)
    return UdpReceiveResult(listen=config.traffic.target, packets=packets, bytes=packet_bytes, payloads=payloads)


def run_udp_sink_service(config: LabScenarioConfig) -> None:
    """Run the foreground server-side lab node on the production Rust dataplane."""
    _run_rust_lab_dataplane(config, role="server")


def run_udp_smoke_test(
    config: LabScenarioConfig,
    *,
    payload: str = "gatherlink-smoke",
    count: int = 3,
    timeout_seconds: float = 3.0,
) -> UdpReceiveResult:
    """Send UDP packets through a running lab service and verify they arrive."""
    sink_record = _running_sink_record(config)
    if sink_record is not None:
        before = _sink_packet_count(sink_record)
        send_udp_packets(config, payload=payload, count=count, interval_seconds=0, use_namespace=True)
        deadline = time.monotonic() + timeout_seconds
        received = before
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = request_service(sink_record, "status")["status"]
            received = int(status.get("packets", 0))
            if received - before >= count:
                break
            time.sleep(0.05)
        return UdpReceiveResult(
            listen=config.traffic.target,
            packets=max(received - before, 0),
            bytes=int(status.get("bytes", 0)) if status else 0,
            payloads=[],
        )

    listen_host, listen_port = _split_host_port(config.traffic.target)
    family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
    target = _split_host_port(config.traffic.listen)
    payload_prefix = payload.encode("utf-8")
    received_payloads: list[str] = []
    packet_bytes = 0
    with socket.socket(family, socket.SOCK_DGRAM) as sink:
        sink.bind((listen_host, listen_port))
        sink.settimeout(0.2)
        with socket.socket(socket.AF_INET6 if ":" in target[0] else socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for index in range(count):
                message = payload_prefix + f"-{index + 1}".encode("ascii")
                sender.sendto(message, target)

        deadline = time.monotonic() + timeout_seconds
        while len(received_payloads) < count and time.monotonic() < deadline:
            try:
                received, _ = sink.recvfrom(65535)
            except TimeoutError:
                continue
            packet_bytes += len(received)
            received_payloads.append(received.decode("utf-8", errors="replace"))

    return UdpReceiveResult(
        listen=config.traffic.target,
        packets=len(received_payloads),
        bytes=packet_bytes,
        payloads=received_payloads,
    )


def _split_host_port(value: str) -> tuple[str, int]:
    if value.startswith("["):
        host, port = value.rsplit("]:", maxsplit=1)
        return host[1:], int(port)
    host, port = value.rsplit(":", maxsplit=1)
    return host, int(port)


def _format_socket_addr(addr: tuple[str, int]) -> str:
    host, port = addr
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _send_udp_packets_in_namespace(
    config: LabScenarioConfig,
    *,
    payload: str,
    count: int,
    interval_seconds: float,
    duration_seconds: float | None,
    bandwidth: str | None,
    payload_size: int | None,
) -> UdpSendResult:
    target_host, target_port = _split_host_port(config.traffic.listen)
    code = """
import socket
import sys
import time

target = (sys.argv[1], int(sys.argv[2]))
payload = sys.argv[3].encode("utf-8")
count = int(sys.argv[4])
interval = float(sys.argv[5])
duration = None if sys.argv[6] == "" else float(sys.argv[6])
bps = None if sys.argv[7] == "" else float(sys.argv[7])
payload_size = None if sys.argv[8] == "" else int(sys.argv[8])


def fixed_payload():
    if payload_size is None:
        return payload
    if len(payload) >= payload_size:
        return payload[:payload_size]
    return payload + (b"x" * (payload_size - len(payload)))


def indexed_payload(index):
    message = payload if count == 1 else payload + f"-{index + 1}".encode("ascii")
    if payload_size is None:
        return message
    if len(message) >= payload_size:
        return message[:payload_size]
    return message + (b"x" * (payload_size - len(message)))


family = socket.AF_INET6 if ":" in target[0] else socket.AF_INET
sent = 0
packets = 0
sock = socket.socket(family, socket.SOCK_DGRAM)
body = fixed_payload()

if duration is not None and bps is not None:
    send_interval = len(body) * 8 / bps
    started_at = time.monotonic()
    next_send = started_at
    while time.monotonic() - started_at < duration:
        sent += sock.sendto(body, target)
        packets += 1
        next_send += send_interval
        sleep_for = next_send - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
else:
    for index in range(count):
        sent += sock.sendto(indexed_payload(index), target)
        packets += 1
        if interval > 0 and index + 1 < count:
            time.sleep(interval)

print(f"{packets} {sent}")
"""
    bps = _bandwidth_to_bps(bandwidth) if bandwidth else None
    result = subprocess.run(
        [
            "sudo",
            "ip",
            "netns",
            "exec",
            _client_namespace(config),
            "sudo",
            "-u",
            _service_user(),
            "-E",
            sys.executable,
            "-c",
            code,
            target_host,
            str(target_port),
            payload,
            str(count),
            str(interval_seconds),
            "" if duration_seconds is None else str(duration_seconds),
            "" if bps is None else str(bps),
            "" if payload_size is None else str(payload_size),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    packets, packet_bytes = result.stdout.strip().split()
    return UdpSendResult(target=config.traffic.listen, packets=int(packets), bytes=int(packet_bytes))


def _empty_path_counter() -> dict[str, int]:
    return {
        "packets": 0,
        "bytes": 0,
        "missed_packets": 0,
        "reordered_packets": 0,
        "packets_needing_reorder": 0,
    }


def _initial_path_capacity_estimates(
    config: LabScenarioConfig,
    path_names: list[str],
    *,
    direction: str,
) -> dict[str, dict[str, int | str | None]]:
    """Build lab startup capacity estimates from cache plus scenario defaults."""
    cache = _load_path_capacity_cache(config)
    estimates: dict[str, dict[str, int | str | None]] = {}
    for path_name in path_names:
        cached = cache.get(path_name, {})
        path = next((candidate for candidate in config.paths if candidate.name == path_name), None)
        default_bps = _default_path_capacity_bps(path) if path is not None else _PATH_CAPACITY_DEFAULT_BPS
        estimates[path_name] = {
            "tx_bps": _control_metadata.int_or_none(cached.get("tx_bps")) if cached else None,
            "rx_bps": _control_metadata.int_or_none(cached.get("rx_bps")) if cached else None,
            "source": "cache" if cached else "config",
            "updated_at": cached.get("updated_at") if isinstance(cached.get("updated_at"), str) else None,
        }
        if estimates[path_name][direction + "_bps"] is None:
            estimates[path_name][direction + "_bps"] = default_bps
    return estimates


def _default_path_capacity_bps(path: LabPathConfig) -> int:
    configured = path.default_max_speed or path.shape.rate
    if configured:
        return int(_bandwidth_to_bps(configured))
    return _PATH_CAPACITY_DEFAULT_BPS


def _load_path_capacity_cache(config: LabScenarioConfig) -> dict[str, dict[str, int | str | None]]:
    cache_file = _path_capacity_cache_file(config)
    if not cache_file.exists():
        return {}
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if raw.get("schema_version") != _PATH_CAPACITY_CACHE_SCHEMA_VERSION:
        return {}
    paths = raw.get("paths")
    if not isinstance(paths, dict):
        return {}
    return {str(path_name): path_data for path_name, path_data in paths.items() if isinstance(path_data, dict)}


def _save_path_capacity_cache(config: LabScenarioConfig, path_capacity: dict[str, dict[str, int | str | None]]) -> None:
    cache_file = _path_capacity_cache_file(config)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    merged_paths = _load_path_capacity_cache(config)
    for path_name, capacity in path_capacity.items():
        existing = merged_paths.get(path_name, {})
        merged_paths[path_name] = _control_metadata.merge_capacity_record(existing, capacity)
    payload = {
        "schema_version": _PATH_CAPACITY_CACHE_SCHEMA_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "paths": merged_paths,
    }
    cache_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _path_capacity_cache_file(config: LabScenarioConfig) -> Path:
    return Path(config.runtime_dir) / _PATH_CAPACITY_CACHE_FILE


def _path_stats_with_qdisc(
    path_stats: dict[str, dict[str, int]],
    qdisc_stats: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Merge service counters with live qdisc drop counters for monitor rows."""
    merged: dict[str, dict[str, int]] = {}
    for path_name, stats in path_stats.items():
        row = dict(stats)
        qdisc_row = qdisc_stats.get(path_name, {})
        # TODO(rust-stats): Keep the monitor field named like dataplane loss. In the lab,
        # qdisc drops are the closest equivalent because they are packets accepted by
        # Gatherlink but discarded by the shaped test path before reaching the peer.
        row["missed_packets"] = row.get("missed_packets", 0) + qdisc_row.get("dropped", 0)
        row["qdisc_dropped_packets"] = qdisc_row.get("dropped", 0)
        row["qdisc_sent_packets"] = qdisc_row.get("sent_packets", 0)
        row["qdisc_sent_bytes"] = qdisc_row.get("sent_bytes", 0)
        merged[path_name] = row
    return merged


_SCHEDULER_INTERVAL_COUNTER_KEYS = {
    "packets",
    "bytes",
    "tx_packets",
    "tx_bytes",
    "rx_packets",
    "rx_bytes",
    "missed_packets",
    "qdisc_dropped_packets",
    "qdisc_sent_packets",
    "qdisc_sent_bytes",
    "security_drop_packets",
    "send_failed_packets",
    "send_failed_bytes",
    "fanout_send_failed_packets",
    "fanout_send_failed_bytes",
    "packets_needing_reorder",
    "reordered_packets",
    "duplicate_packets",
    "expected_duplicate_packets",
    "unexpected_duplicate_packets",
}


def _scheduler_interval_path_stats(
    path_stats: dict[str, dict[str, int]],
    previous_path_stats: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """
    Return path stats shaped for scheduler pressure decisions.

    Monitor rows need cumulative counters. Scheduler feedback needs current
    pressure. For monotonic counters, use interval deltas. For queue depth and
    age, keep the current absolute value because a stable non-empty queue is
    itself a live pressure signal.
    """
    interval_stats: dict[str, dict[str, int]] = {}
    for path_name, stats in path_stats.items():
        previous = previous_path_stats.get(path_name, {})
        row = dict(stats)
        for key in _SCHEDULER_INTERVAL_COUNTER_KEYS:
            if key in row:
                row[key] = max(0, int(row.get(key, 0)) - int(previous.get(key, 0)))
        interval_stats[path_name] = row
    return interval_stats


def _path_stats_with_rx(
    path_stats: dict[str, dict[str, int]],
    rx_path_stats: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Add reverse/reply counters without changing the existing TX-oriented monitor totals."""
    return _path_stats_with_directional(path_stats, rx_path_stats, primary_direction="tx")


def _path_stats_with_directional(
    path_stats: dict[str, dict[str, int]],
    other_direction_stats: dict[str, dict[str, int]],
    *,
    primary_direction: str,
) -> dict[str, dict[str, int]]:
    """
    Annotate lab path counters with explicit local TX/RX directions.

    The forwarder primarily transmits user traffic and receives replies. The sink
    primarily receives user traffic and transmits replies. Keeping both views in
    the shared status shape lets the monitor and future Python scheduler consume
    the same directional counters without guessing from service names.
    """
    merged = {path_name: dict(stats) for path_name, stats in path_stats.items()}
    secondary_direction = "rx" if primary_direction == "tx" else "tx"
    for row in merged.values():
        row[f"{primary_direction}_packets"] = row.get("packets", 0)
        row[f"{primary_direction}_bytes"] = row.get("bytes", 0)
        row.setdefault(f"{secondary_direction}_packets", 0)
        row.setdefault(f"{secondary_direction}_bytes", 0)
    for path_name, other_stats in other_direction_stats.items():
        row = merged.setdefault(path_name, _empty_path_counter())
        row.setdefault(f"{primary_direction}_packets", row.get("packets", 0))
        row.setdefault(f"{primary_direction}_bytes", row.get("bytes", 0))
        row[f"{secondary_direction}_packets"] = other_stats.get("packets", 0)
        row[f"{secondary_direction}_bytes"] = other_stats.get("bytes", 0)
    return merged


def _total_missed_packets(path_stats: dict[str, dict[str, int]]) -> int:
    """Sum already-normalized path misses without double-counting lab qdisc drops."""
    return _sum_path_counter(path_stats, "missed_packets")


def _sum_path_counter(path_stats: dict[str, dict[str, int]], counter_name: str) -> int:
    """Sum one integer counter across all path rows."""
    return sum(int(stats.get(counter_name, 0) or 0) for stats in path_stats.values())


def _fixed_payload(payload: bytes, payload_size: int | None) -> bytes:
    if payload_size is None:
        return payload
    if len(payload) >= payload_size:
        return payload[:payload_size]
    return payload + (b"x" * (payload_size - len(payload)))


def _indexed_payload(payload: bytes, index: int, payload_size: int | None) -> bytes:
    message = payload + f"-{index + 1}".encode("ascii")
    return _fixed_payload(message, payload_size)


def _pid_file(config: LabScenarioConfig) -> Path:
    return Path(config.runtime_dir) / "service.pid"


def _log_file(config: LabScenarioConfig) -> Path:
    return Path(config.runtime_dir) / "service.log"


def _sink_log_file(config: LabScenarioConfig) -> Path:
    return Path(config.runtime_dir) / "sink.log"


def _service_user() -> str:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
    return os.environ.get("USER") or "current-user"


def _service_command(
    config: LabScenarioConfig,
    config_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    base = [sys.executable, "-m", "gatherlink.cli.main", "lab", "service", str(config_path)]
    return _namespace_service_command(config, _client_namespace(config), base, extra_env=extra_env)


def _sink_service_command(
    config: LabScenarioConfig,
    config_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    base = [sys.executable, "-m", "gatherlink.cli.main", "lab", "sink-service", str(config_path)]
    return _namespace_service_command(config, _server_namespace(config), base, extra_env=extra_env)


def _namespace_service_command(
    config: LabScenarioConfig,
    namespace: str,
    base: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    user = _service_user()
    if user == "root":
        raise RuntimeError("cannot choose an unprivileged user; rerun with SUDO_USER set")
    env_prefix = ["env", *[f"{key}={value}" for key, value in (extra_env or {}).items()]] if extra_env else []
    if config.paths:
        return ["sudo", "ip", "netns", "exec", namespace, "sudo", "-u", user, "-E", *env_prefix, *base]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return ["sudo", "-u", user, "-E", *env_prefix, *base]
    return [*env_prefix, *base]


def _lab_service_name(config: LabScenarioConfig) -> str:
    return service_name("lab", config.name)


def _lab_sink_service_name(config: LabScenarioConfig) -> str:
    return f"{_lab_service_name(config)}.sink"


def _lab_hidden_sink_service_name(config: LabScenarioConfig) -> str:
    return f"{_lab_sink_service_name(config)}.hidden"


def _register_lab_service(
    config_path: Path,
    config: LabScenarioConfig,
    pid: int,
    command: list[str],
    *,
    service_name_value: str | None = None,
    log_file: Path | None = None,
    metadata_role: str = "forwarder",
) -> ServiceRecord:
    record = ServiceRecord(
        name=service_name_value or _lab_service_name(config),
        kind="lab",
        manager="process",
        pid=pid,
        pid_file=_pid_file(config),
        log_file=log_file or _log_file(config),
        detached_from_console=True,
        command=command,
        cwd=Path.cwd(),
        metadata={
            "config": str(config_path),
            "runtime_dir": config.runtime_dir,
            "scenario": config.scenario,
            "security_mode": config.security.mode,
            "role": metadata_role,
        },
    )
    return ServiceRegistry().register(record)


def _ensure_lab_service_record(config: LabScenarioConfig) -> ServiceRecord:
    return _ensure_lab_worker_record(
        config,
        service_name_value=_lab_service_name(config),
        log_file=_log_file(config),
        role="forwarder",
    )


def _ensure_lab_sink_service_record(config: LabScenarioConfig) -> ServiceRecord:
    if os.environ.get(_LAB_HIDDEN_SINK_IPC_ENV) == "1":
        return _ensure_lab_worker_record(
            config,
            service_name_value=_lab_hidden_sink_service_name(config),
            log_file=_sink_log_file(config),
            role="sink-hidden",
        )
    return _ensure_lab_worker_record(
        config,
        service_name_value=_lab_sink_service_name(config),
        log_file=_sink_log_file(config),
        role="sink",
    )


def _ensure_lab_worker_record(
    config: LabScenarioConfig,
    *,
    service_name_value: str,
    log_file: Path,
    role: str,
) -> ServiceRecord:
    """
    Let the actual Python worker claim the service record after launcher handoff.

    `lab up` starts services through `sudo ip netns exec ... sudo -u ...`, so the first PID the launcher sees can be
    a wrapper. The worker rewrites the same per-service file with its own PID before opening IPC. That makes
    `services list`, `services close`, and stale-PID cleanup track the real long-running process.
    """
    registry = ServiceRegistry()
    metadata = {
        "runtime_dir": config.runtime_dir,
        "scenario": config.scenario,
        "security_mode": config.security.mode,
        "role": role,
    }
    try:
        existing = registry.resolve(service_name_value)
    except ValueError:
        existing = ServiceRecord(
            name=service_name_value,
            kind="lab",
            manager="process",
            log_file=log_file,
        )

    record = existing.model_copy(
        update={
            "pid": os.getpid(),
            "log_file": log_file,
            "cwd": Path.cwd(),
            "metadata": {**existing.metadata, **metadata},
        }
    )
    return registry.register(record)


def _running_sink_record(config: LabScenarioConfig) -> ServiceRecord | None:
    registry = ServiceRegistry()
    for name in [_lab_hidden_sink_service_name(config), _lab_sink_service_name(config)]:
        try:
            record = registry.resolve(name)
        except ValueError:
            continue
        if record.is_running() and record.metadata.get("role") != "remote-status-proxy":
            return record
    return None


def _running_lab_side_record(config: LabScenarioConfig, side: str) -> ServiceRecord | None:
    """Resolve operator-friendly lab side names to their managed service records."""
    if side in {"source", "client", "forwarder", "local"}:
        return _running_forwarder_record(config)
    if side in {"sink", "server", "remote"}:
        return _running_sink_record(config)
    raise RuntimeError("side must be source/client/forwarder/local or sink/server/remote")


def _running_forwarder_record(config: LabScenarioConfig) -> ServiceRecord | None:
    try:
        record = ServiceRegistry().resolve(_lab_service_name(config))
    except ValueError:
        return None
    return record if record.is_running() else None


def _resolve_runtime_service_id(runtime_config, service: str) -> int:
    """Resolve a service name or explicit numeric id inside the compiled runtime config."""
    with suppress(ValueError):
        return int(service)
    for candidate in runtime_config.services:
        if candidate.name == service:
            return int(candidate.service_id)
    known = ", ".join(candidate.name for candidate in runtime_config.services)
    raise RuntimeError(f"unknown runtime service {service!r}; known services: {known}")


def _sink_packet_count(record: ServiceRecord) -> int:
    try:
        status = request_service(record, "status")["status"]
    except ServiceIpcError:
        return 0
    return int(status.get("packets", 0))


def _pid_is_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
