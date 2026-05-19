"""Runtime helpers for local lab scenarios."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from gatherlink.control import ControlCadenceState
from gatherlink.control import metadata as _control_metadata
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
from gatherlink.paths.telemetry import PathLatencyTracker, prune_outstanding_latency_packets
from gatherlink.protocol import (
    SEQUENCE_SPACE as _SEQUENCE_SPACE,
)
from gatherlink.protocol import DataFrame
from gatherlink.protocol import (
    decode_control_frame as _decode_control_frame,
)
from gatherlink.protocol import (
    decode_data_frame as _decode_data_frame,
)
from gatherlink.protocol import (
    encode_control_frame as _encode_control_frame,
)
from gatherlink.protocol import (
    encode_control_payload as _encode_control_payload,
)
from gatherlink.protocol import (
    encode_data_frame as _encode_data_frame,
)
from gatherlink.runtime.services import (
    ServiceIpcError,
    ServiceIpcServer,
    ServiceRecord,
    ServiceRegistry,
    request_service,
    service_name,
)
from gatherlink.time.offset import (
    InternalClockSyncClient,
    InternalClockSyncMessage,
    SinkTimeMessage,
    internal_monotonic_us,
)
from gatherlink.time.sink import (
    SINK_TIME_BOOTSTRAP_ENV as _SINK_TIME_BOOTSTRAP_ENV,
)
from gatherlink.time.sink import (
    encode_sink_time_sample as _encode_sink_time_sample,
)
from gatherlink.time.sink import (
    read_sink_ntp_sample as _read_sink_ntp_sample,
)
from gatherlink.time.sources.direct_ntp import DirectNtpSample

_SINK_TIME_ADVERTISE_INTERVAL_SECONDS = 5.0
_NTP_STATUS_REFRESH_INTERVAL_SECONDS = 30.0
_PATH_CAPACITY_CACHE_SCHEMA_VERSION = 1
_PATH_CAPACITY_CACHE_FILE = "path-capacity-cache.json"
_PATH_CAPACITY_DETECTION_WINDOW_SECONDS = 5.0
_PATH_CAPACITY_INCREASE_SUSTAIN_SECONDS = 15.0
_PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS = 60.0
_PATH_CAPACITY_MIN_CHANGE_RATIO = 0.15
_PATH_CAPACITY_HEADROOM_RATIO = 1.05
_PATH_CAPACITY_ADJUSTMENT_RATIO = 0.25
_PATH_CAPACITY_MIN_SAMPLE_BYTES = 16 * 1024
_PATH_CAPACITY_DEFAULT_BPS = 50_000_000

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


def ensure_service_not_root() -> None:
    """Refuse to run Gatherlink service behavior as root."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("refusing to run Gatherlink lab service as root; run as a normal user")


def start_lab_service(config_path: Path, config: LabScenarioConfig) -> ServiceStartResult:
    """Start the foreground lab service as an unprivileged background process."""
    return _start_lab_process_service(
        config_path,
        config,
        service_name_value=_lab_service_name(config),
        command=_service_command(config, config_path),
        log_file=_log_file(config),
        metadata_role="forwarder",
    )


def start_lab_sink_service(config_path: Path, config: LabScenarioConfig) -> ServiceStartResult:
    """Start the foreground lab sink service as an unprivileged background process."""
    bootstrap_sample = _read_sink_ntp_sample()
    extra_env = (
        {_SINK_TIME_BOOTSTRAP_ENV: _encode_sink_time_sample(bootstrap_sample)}
        if bootstrap_sample is not None and config.paths
        else None
    )
    return _start_lab_process_service(
        config_path,
        config,
        service_name_value=_lab_sink_service_name(config),
        command=_sink_service_command(config, config_path, extra_env=extra_env),
        log_file=_sink_log_file(config),
        metadata_role="sink",
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
    for name in [_lab_service_name(config), _lab_sink_service_name(config)]:
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
    """Run the first foreground UDP lab service until interrupted."""
    ensure_service_not_root()
    service_record = _ensure_lab_service_record(config)
    stop_event = Event()
    listen_host, listen_port = _split_host_port(config.traffic.listen)
    _target_host, target_port = _split_host_port(config.traffic.target)
    family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    sock.bind((listen_host, listen_port))

    path_names = [path.name for path in config.paths] or ["default"]
    path_ids = _path_ids(path_names)
    path_targets = _forwarder_path_targets(config, target_port)
    path_sockets = _forwarder_path_sockets(config)
    path_stats = {_path_name: _empty_path_counter() for _path_name in path_names}
    reply_path_stats = {_path_name: _empty_path_counter() for _path_name in path_names}
    control_metadata = _control_metadata.empty_control_metadata()
    qdisc_baseline = _lab_qdisc_stats(config, side="local")
    capacity_detector = _PathCapacityDetector(config, path_names=path_names, direction="tx")
    latency_tracker = PathLatencyTracker(path_names)
    clock_sync = InternalClockSyncClient(path_names)
    outstanding_packets: dict[int, tuple[str, float]] = {}
    _control_metadata.merge_control_path_capacity(control_metadata, capacity_detector.snapshot())
    packet_count = 0
    byte_count = 0
    started_at = datetime.now(UTC)
    control_cadence = ControlCadenceState()

    def status_payload() -> dict[str, object]:
        _control_metadata.refresh_gatherlink_time(control_metadata)
        qdisc_stats = _qdisc_delta(_lab_qdisc_stats(config, side="local"), qdisc_baseline)
        return {
            "running": not stop_event.is_set(),
            "listen": config.traffic.listen,
            "target": config.traffic.target,
            "path_targets": {name: _format_socket_addr(target) for name, target in path_targets.items()},
            "paths": path_names,
            "packets": packet_count,
            "bytes": byte_count,
            "tx_packets": packet_count,
            "tx_bytes": byte_count,
            "rx_packets": sum(stats.get("packets", 0) for stats in reply_path_stats.values()),
            "rx_bytes": sum(stats.get("bytes", 0) for stats in reply_path_stats.values()),
            "missed_packets": _total_missed_packets(path_stats, qdisc_stats),
            "path_stats": _path_stats_with_directional(
                _path_stats_with_qdisc(path_stats, qdisc_stats),
                reply_path_stats,
                primary_direction="tx",
            ),
            "control_metadata": control_metadata,
            "control_cadence": control_cadence.status(),
            "started_at": started_at.isoformat(),
        }

    def request_control_cadence(request: dict[str, object]) -> dict[str, object]:
        profile = str(request.get("profile") or "monitor")
        ttl_seconds = float(request.get("ttl_seconds") or 120.0)
        if profile != "monitor":
            raise RuntimeError(f"unsupported control cadence profile: {profile}")
        return control_cadence.request_monitor_profile(ttl_seconds=ttl_seconds)

    ipc = ServiceIpcServer(
        service_record,
        status=status_payload,
        stop=stop_event.set,
        commands={"control-cadence": request_control_cadence},
    )
    ipc.start()
    print(
        f"lab service: listening udp={config.traffic.listen} target={config.traffic.target} "
        f"paths={','.join(path_names)} path_targets="
        f"{','.join(f'{name}->{_format_socket_addr(target)}' for name, target in path_targets.items())} "
        f"ipc={service_record.ipc_socket}",
        flush=True,
    )
    print("lab service: press Ctrl-C to stop", flush=True)
    next_control_metadata_at = 0.0
    sockets = [sock, *path_sockets.values()]
    socket_to_path = {path_socket: path_name for path_name, path_socket in path_sockets.items()}

    try:
        while not stop_event.is_set():
            qdisc_stats = _qdisc_delta(_lab_qdisc_stats(config, side="local"), qdisc_baseline)
            changed_capacity = capacity_detector.observe(path_stats, qdisc_stats)
            if changed_capacity:
                _control_metadata.merge_control_path_capacity(control_metadata, changed_capacity)
            prune_outstanding_latency_packets(outstanding_packets)
            if time.monotonic() >= next_control_metadata_at:
                _send_lab_control_metadata(
                    path_names,
                    path_ids,
                    path_sockets,
                    path_targets,
                    control_metadata,
                    path_capacity=changed_capacity or capacity_detector.dirty_snapshot(),
                    path_latency=latency_tracker.dirty_snapshot(),
                    clock_sync=clock_sync.create_requests(path_names, path_ids),
                )
                capacity_detector.mark_sent()
                latency_tracker.mark_sent()
                traffic_total = packet_count + sum(stats.get("packets", 0) for stats in reply_path_stats.values())
                next_control_metadata_at = time.monotonic() + control_cadence.next_interval(traffic_total)

            readable, _writable, _errors = select.select(sockets, [], [], 0.2)
            if not readable:
                continue
            for ready_socket in readable:
                if ready_socket is sock:
                    payload, source = sock.recvfrom(65535)
                    path = path_names[packet_count % len(path_names)]
                    packet_count += 1
                    sequence = packet_count
                    frame_payload = _encode_data_frame(sequence, path_ids[path], payload)
                    frame_bytes = path_sockets[path].sendto(frame_payload, path_targets[path])
                    outstanding_packets[sequence] = (path, time.monotonic())
                    byte_count += len(payload)
                    path_stats[path]["packets"] += 1
                    path_stats[path]["bytes"] += len(payload)
                    print(
                        f"lab service: forwarded packet={packet_count} seq={sequence} bytes={len(payload)} "
                        f"frame_bytes={frame_bytes} total_bytes={byte_count} path={path} source={source}",
                        flush=True,
                    )
                    continue

                raw_payload, source = ready_socket.recvfrom(65535)
                path = socket_to_path[ready_socket]
                control_frame = _decode_control_frame(raw_payload)
                if control_frame is not None:
                    _control_metadata.record_control_metadata_received(
                        control_metadata,
                        len(raw_payload),
                        control_frame,
                        source,
                        path_name=path,
                    )
                    _control_metadata.record_control_path_capacity(
                        control_metadata,
                        control_frame.path_capacity_bps,
                        {},
                        _path_names_by_id(path_names),
                    )
                    _control_metadata.record_control_path_latency(
                        control_metadata,
                        control_frame.path_latency_us,
                        {},
                        _path_names_by_id(path_names),
                    )
                    _control_metadata.record_sink_time(
                        control_metadata,
                        control_frame.sink_time,
                        _path_names_by_id(path_names),
                        received_at_internal_us=internal_monotonic_us(),
                    )
                    clock_sync_updates = clock_sync.observe_control_frame(
                        control_frame.internal_clock_sync,
                        path_names_by_id=_path_names_by_id(path_names),
                    )
                    _control_metadata.merge_internal_clock_sync(control_metadata, clock_sync_updates)
                    print(
                        "lab service: received control "
                        f"path_capacity={','.join(f'{path_id}:tx={capacity[0]} rx={capacity[1]}' for path_id, capacity in control_frame.path_capacity_bps.items())} "
                        f"path_latency={','.join(f'{path_id}:tx={latency[0]}/{latency[1]} rx={latency[2]}/{latency[3]}' for path_id, latency in control_frame.path_latency_us.items())} "
                        f"clock_sync={len(control_frame.internal_clock_sync)} "
                        f"sink_time={len(control_frame.sink_time)} "
                        f"path={path} source={source}",
                        flush=True,
                    )
                    continue

                frame = _decode_data_frame(raw_payload)
                payload = frame.payload if frame is not None else raw_payload
                if frame is not None:
                    sent = outstanding_packets.pop(frame.sequence, None)
                    if sent is not None:
                        sent_path, sent_at = sent
                        one_way_us = max(int((time.monotonic() - sent_at) * 500_000), 1)
                        changed_latency = latency_tracker.observe(sent_path, one_way_us)
                        _control_metadata.merge_control_path_latency(control_metadata, changed_latency)
                reply_path_stats[path]["packets"] += 1
                reply_path_stats[path]["bytes"] += len(payload)
                print(
                    f"lab service: received reply bytes={len(payload)} path={path} source={source}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print(f"lab service: stopped packets={packet_count} bytes={byte_count}", flush=True)
    finally:
        ipc.close()
        sock.close()
        for path_socket in path_sockets.values():
            path_socket.close()


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
    """Run the foreground UDP receiver service until interrupted."""
    ensure_service_not_root()
    service_record = _ensure_lab_sink_service_record(config)
    stop_event = Event()
    _listen_host, listen_port = _split_host_port(config.traffic.target)
    listeners = _sink_path_listeners(config, listen_port)
    configured_path_names_by_id = _path_names_by_id(list(listeners))
    learned_path_names_by_id: dict[int, str] = {}
    packets = 0
    packet_bytes = 0
    path_stats = {_path_name: _empty_path_counter() for _path_name in listeners}
    reply_path_stats = {_path_name: _empty_path_counter() for _path_name in listeners}
    control_metadata = _control_metadata.empty_control_metadata()
    qdisc_baseline = _lab_qdisc_stats(config, side="remote")
    path_ids = _path_ids(list(listeners))
    capacity_detector = _PathCapacityDetector(config, path_names=list(listeners), direction="tx")
    _control_metadata.merge_control_path_capacity(control_metadata, capacity_detector.snapshot())
    sequence_tracker = _LabSequenceTracker()
    last_source_by_path: dict[str, tuple[str, int]] = {}
    reply_packets = 0
    reply_bytes = 0
    last_payload = ""
    last_payload_bytes = 0
    last_source = ""
    started_at = datetime.now(UTC)
    ntp_sample = _read_sink_ntp_sample()
    next_ntp_status_at = time.monotonic() + _NTP_STATUS_REFRESH_INTERVAL_SECONDS
    control_cadence = ControlCadenceState()

    def status_payload() -> dict[str, object]:
        _control_metadata.refresh_gatherlink_time(control_metadata)
        qdisc_stats = _qdisc_delta(_lab_qdisc_stats(config, side="remote"), qdisc_baseline)
        path_stats_with_missing = _path_stats_with_pending_missing(
            path_stats,
            sequence_tracker.pending_missing_by_path(list(listeners)),
        )
        return {
            "running": not stop_event.is_set(),
            "listen": config.traffic.target,
            "path_listeners": {name: _format_socket_addr(addr) for name, (_sock, addr) in listeners.items()},
            "packets": packets,
            "bytes": packet_bytes,
            "tx_packets": reply_packets,
            "tx_bytes": reply_bytes,
            "rx_packets": packets,
            "rx_bytes": packet_bytes,
            "missed_packets": _total_missed_packets(path_stats_with_missing, qdisc_stats),
            "reordered_packets": sequence_tracker.out_of_order_packets,
            "packets_needing_reorder": sequence_tracker.reorder_needed_packets,
            "path_stats": _path_stats_with_directional(
                _path_stats_with_qdisc(path_stats_with_missing, qdisc_stats),
                reply_path_stats,
                primary_direction="rx",
            ),
            "control_metadata": control_metadata,
            "control_cadence": control_cadence.status(),
            "reply_packets": reply_packets,
            "reply_bytes": reply_bytes,
            "last_payload": last_payload,
            "last_payload_bytes": last_payload_bytes,
            "last_source": last_source,
            "started_at": started_at.isoformat(),
        }

    def send_reverse_traffic(request: dict[str, object]) -> dict[str, object]:
        nonlocal reply_packets, reply_bytes
        if not last_source_by_path:
            raise RuntimeError("sink has no learned forwarder sources yet; send at least one packet to the sink first")
        payload_text = str(request.get("payload") or "gatherlink-reverse")
        count = int(request.get("count") or 5)
        interval_seconds = float(request.get("interval_seconds") or 0.05)
        duration_raw = request.get("duration_seconds")
        duration_seconds = float(duration_raw) if duration_raw is not None else None
        bandwidth = request.get("bandwidth")
        payload_size_raw = request.get("payload_size")
        payload_size = int(payload_size_raw) if payload_size_raw is not None else None
        bps = _bandwidth_to_bps(str(bandwidth)) if bandwidth else None
        base_payload = _fixed_payload(payload_text.encode("utf-8"), payload_size)
        path_names = [path_name for path_name in listeners if path_name in last_source_by_path]
        packets_sent = 0
        bytes_sent = 0

        def emit(index: int) -> None:
            nonlocal packets_sent, bytes_sent, reply_packets, reply_bytes
            path_name = path_names[index % len(path_names)]
            listener_socket, _addr = listeners[path_name]
            target = last_source_by_path[path_name]
            packet_payload = base_payload if count == 1 else _indexed_payload(base_payload, index, payload_size)
            sequence = packets + reply_packets + 1
            frame_payload = _encode_data_frame(
                sequence,
                path_ids.get(path_name, path_ids[socket_to_path[listener_socket]]),
                packet_payload,
            )
            listener_socket.sendto(frame_payload, target)
            packets_sent += 1
            bytes_sent += len(packet_payload)
            reply_packets += 1
            reply_bytes += len(packet_payload)
            reply_path_stats.setdefault(path_name, _empty_path_counter())
            reply_path_stats[path_name]["packets"] += 1
            reply_path_stats[path_name]["bytes"] += len(packet_payload)

        if duration_seconds is not None and bps is not None:
            interval = len(base_payload) * 8 / bps
            started = time.monotonic()
            next_send = started
            while time.monotonic() - started < duration_seconds:
                emit(packets_sent)
                next_send += interval
                if (sleep_for := next_send - time.monotonic()) > 0:
                    time.sleep(sleep_for)
        else:
            for index in range(count):
                emit(index)
                if interval_seconds > 0 and index + 1 < count:
                    time.sleep(interval_seconds)
        return {"target": "learned-forwarder-sources", "packets": packets_sent, "bytes": bytes_sent}

    def request_control_cadence(request: dict[str, object]) -> dict[str, object]:
        profile = str(request.get("profile") or "monitor")
        ttl_seconds = float(request.get("ttl_seconds") or 120.0)
        if profile != "monitor":
            raise RuntimeError(f"unsupported control cadence profile: {profile}")
        return control_cadence.request_monitor_profile(ttl_seconds=ttl_seconds)

    ipc = ServiceIpcServer(
        service_record,
        status=status_payload,
        stop=stop_event.set,
        commands={"send-reverse": send_reverse_traffic, "control-cadence": request_control_cadence},
    )
    ipc.start()
    sockets = [listener[0] for listener in listeners.values()]
    socket_to_path = {listener[0]: path_name for path_name, listener in listeners.items()}
    try:
        print(
            "lab sink service: listening "
            f"{','.join(f'{name}={_format_socket_addr(addr)}' for name, (_sock, addr) in listeners.items())} "
            f"target={config.traffic.target} ipc={service_record.ipc_socket}",
            flush=True,
        )
        print("lab sink service: press Ctrl-C to stop", flush=True)
        next_control_metadata_at = 0.0
        try:
            while not stop_event.is_set():
                if time.monotonic() >= next_ntp_status_at:
                    ntp_sample = _read_sink_ntp_sample()
                    next_ntp_status_at = time.monotonic() + _NTP_STATUS_REFRESH_INTERVAL_SECONDS
                qdisc_stats = _qdisc_delta(_lab_qdisc_stats(config, side="remote"), qdisc_baseline)
                changed_capacity = capacity_detector.observe(reply_path_stats, qdisc_stats)
                if changed_capacity:
                    _control_metadata.merge_control_path_capacity(control_metadata, changed_capacity)
                if time.monotonic() >= next_control_metadata_at and last_source_by_path:
                    control_path_names = [path_name for path_name in listeners if path_name in last_source_by_path]
                    sink_time = _control_metadata.sink_time_messages(control_path_names, path_ids, ntp_sample)
                    _control_metadata.record_sink_time(
                        control_metadata,
                        list(sink_time.values()),
                        _path_names_by_id(list(listeners)),
                        received_at_internal_us=internal_monotonic_us(),
                        local_sink=True,
                        ntp_sample=ntp_sample,
                    )
                    _send_lab_control_metadata(
                        control_path_names,
                        path_ids,
                        {path_name: listeners[path_name][0] for path_name in control_path_names},
                        {path_name: last_source_by_path[path_name] for path_name in control_path_names},
                        control_metadata,
                        path_capacity=changed_capacity or capacity_detector.dirty_snapshot(),
                        sink_time=sink_time,
                        ntp_sample=ntp_sample,
                    )
                    capacity_detector.mark_sent()
                    traffic_total = packets + reply_packets
                    next_control_metadata_at = time.monotonic() + control_cadence.next_interval(traffic_total)

                readable, _writable, _errors = select.select(sockets, [], [], 0.2)
                if not readable:
                    continue
                for ready_socket in readable:
                    raw_payload, source = ready_socket.recvfrom(65535)
                    path = socket_to_path[ready_socket]
                    control_frame = _decode_control_frame(raw_payload)
                    if control_frame is not None:
                        _control_metadata.record_control_metadata_received(
                            control_metadata,
                            len(raw_payload),
                            control_frame,
                            source,
                            path_name=path,
                        )
                        for path_id, path_name in control_frame.path_metadata.items():
                            previous_name = learned_path_names_by_id.get(path_id) or configured_path_names_by_id.get(
                                path_id
                            )
                            learned_path_names_by_id[path_id] = path_name
                            _rename_path_counter(path_stats, previous_name, path_name)
                        _control_metadata.record_control_path_capacity(
                            control_metadata,
                            control_frame.path_capacity_bps,
                            learned_path_names_by_id,
                            configured_path_names_by_id,
                        )
                        _control_metadata.record_control_path_latency(
                            control_metadata,
                            control_frame.path_latency_us,
                            learned_path_names_by_id,
                            configured_path_names_by_id,
                        )
                        _control_metadata.record_sink_time(
                            control_metadata,
                            control_frame.sink_time,
                            {**configured_path_names_by_id, **learned_path_names_by_id},
                            received_at_internal_us=internal_monotonic_us(),
                            local_sink=True,
                        )
                        clock_sync_responses = _control_metadata.sink_clock_sync_responses(
                            control_frame.internal_clock_sync,
                            received_at_us=internal_monotonic_us(),
                        )
                        _control_metadata.merge_internal_clock_sync(control_metadata, {"role": "sink-authoritative"})
                        for response in clock_sync_responses:
                            response_payload = _encode_control_payload(
                                {},
                                path_clock_sync=[response],
                            )
                            response_frame = _encode_control_frame(response.path_id, response_payload)
                            ready_socket.sendto(response_frame, source)
                            _control_metadata.record_control_metadata_sent(
                                control_metadata,
                                len(response_frame),
                                message_count=1,
                                path_metadata={},
                                path_capacity={},
                                path_latency={},
                                internal_clock={"role": "sink-authoritative"},
                                sink_time=[],
                                path_name=path,
                            )
                        print(
                            "lab sink service: control path_metadata="
                            f"{','.join(f'{path_id}:{name}' for path_id, name in control_frame.path_metadata.items())} "
                            "path_capacity="
                            f"{','.join(f'{path_id}:tx={capacity[0]} rx={capacity[1]}' for path_id, capacity in control_frame.path_capacity_bps.items())} "
                            "path_latency="
                            f"{','.join(f'{path_id}:tx={latency[0]}/{latency[1]} rx={latency[2]}/{latency[3]}' for path_id, latency in control_frame.path_latency_us.items())} "
                            f"clock_sync={len(control_frame.internal_clock_sync)} "
                            f"sink_time={len(control_frame.sink_time)} "
                            f"socket_path={path} source={source}",
                            flush=True,
                        )
                        continue
                    frame = _decode_data_frame(raw_payload)
                    payload = frame.payload if frame is not None else raw_payload
                    sequence = frame.sequence if frame is not None else None
                    frame_path = (
                        _path_name_for_frame(frame, learned_path_names_by_id, configured_path_names_by_id)
                        if frame is not None
                        else path
                    )
                    path_stats.setdefault(frame_path, _empty_path_counter())
                    observation = sequence_tracker.observe(sequence) if sequence is not None else _SequenceObservation()
                    packets += 1
                    packet_bytes += len(payload)
                    path_stats[frame_path]["packets"] += 1
                    path_stats[frame_path]["bytes"] += len(payload)
                    path_stats[frame_path]["reordered_packets"] += 1 if observation.out_of_order else 0
                    path_stats[frame_path]["packets_needing_reorder"] += observation.reorder_needed_packets
                    last_source_by_path[frame_path] = source
                    reply_payload = _reply_payload(payload)
                    reply_sequence = sequence if sequence is not None else packets
                    reply_frame = _encode_data_frame(reply_sequence, path_ids.get(frame_path, path_ids[path]), reply_payload)
                    ready_socket.sendto(reply_frame, source)
                    reply_packets += 1
                    reply_bytes += len(reply_payload)
                    reply_path_stats.setdefault(frame_path, _empty_path_counter())
                    reply_path_stats[frame_path]["packets"] += 1
                    reply_path_stats[frame_path]["bytes"] += len(reply_payload)
                    last_payload = payload.decode("utf-8", errors="replace")
                    last_payload_bytes = len(payload)
                    last_source = repr(source)
                    print(
                        f"lab sink service: received packet={packets} seq={sequence} bytes={len(payload)} "
                        f"total_bytes={packet_bytes} path={frame_path} socket_path={path} source={source} payload={last_payload}",
                        flush=True,
                    )
        except KeyboardInterrupt:
            print(f"lab sink service: stopped packets={packets} bytes={packet_bytes}", flush=True)
    finally:
        ipc.close()
        for listener_socket in sockets:
            listener_socket.close()


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


def _forwarder_path_targets(config: LabScenarioConfig, target_port: int) -> dict[str, tuple[str, int]]:
    if not config.paths:
        return {"default": _split_host_port(config.traffic.target)}
    return {path.name: (path.server_address, target_port) for path in config.paths}


def _forwarder_path_sockets(config: LabScenarioConfig) -> dict[str, socket.socket]:
    if not config.paths:
        target_host, _target_port = _split_host_port(config.traffic.target)
        family = socket.AF_INET6 if ":" in target_host else socket.AF_INET
        return {"default": socket.socket(family, socket.SOCK_DGRAM)}

    sockets: dict[str, socket.socket] = {}
    try:
        for path in config.paths:
            family = socket.AF_INET6 if ":" in path.client_address else socket.AF_INET
            path_socket = socket.socket(family, socket.SOCK_DGRAM)
            path_socket.bind((path.client_address, 0))
            sockets[path.name] = path_socket
    except Exception:
        for path_socket in sockets.values():
            path_socket.close()
        raise
    return sockets


def _sink_path_listeners(
    config: LabScenarioConfig, listen_port: int
) -> dict[str, tuple[socket.socket, tuple[str, int]]]:
    if not config.paths:
        listen_host, _listen_port = _split_host_port(config.traffic.target)
        family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
        listen_socket = socket.socket(family, socket.SOCK_DGRAM)
        listen_addr = (listen_host, listen_port)
        listen_socket.bind(listen_addr)
        return {"default": (listen_socket, listen_addr)}

    listeners: dict[str, tuple[socket.socket, tuple[str, int]]] = {}
    try:
        for path in config.paths:
            family = socket.AF_INET6 if ":" in path.server_address else socket.AF_INET
            listen_socket = socket.socket(family, socket.SOCK_DGRAM)
            listen_addr = (path.server_address, listen_port)
            listen_socket.bind(listen_addr)
            listeners[path.name] = (listen_socket, listen_addr)
    except Exception:
        for listen_socket, _addr in listeners.values():
            listen_socket.close()
        raise
    return listeners


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


def _send_lab_control_metadata(
    path_names: list[str],
    path_ids: dict[str, int],
    path_sockets: dict[str, socket.socket],
    path_targets: dict[str, tuple[str, int]],
    control_metadata: dict[str, object],
    *,
    path_capacity: dict[str, dict[str, int | str | None]] | None = None,
    path_latency: dict[str, dict[str, int | str | None]] | None = None,
    clock_sync: dict[str, InternalClockSyncMessage] | None = None,
    sink_time: dict[str, SinkTimeMessage] | None = None,
    ntp_sample: DirectNtpSample | None = None,
) -> None:
    """
    Send sparse peer metadata over the control metaband.

    Path names are refreshed periodically because this early lab control path is UDP-based. Capacity estimates are
    included only at startup or after the detector marks a path as changed, keeping the normal control chatter small.
    """
    metadata = {path_ids[path_name]: path_name for path_name in path_names}
    capacity_by_id = _control_metadata.capacity_by_path_id(path_capacity or {}, path_ids)
    latency_by_id = _control_metadata.latency_by_path_id(path_latency or {}, path_ids)
    all_clock_sync = list((clock_sync or {}).values())
    all_sink_time = list((sink_time or {}).values())
    payload = _encode_control_payload(
        metadata,
        capacity_by_id,
        latency_by_id,
        path_clock_sync=all_clock_sync,
        sink_time=all_sink_time,
    )
    message_count = len(metadata) + len(capacity_by_id) + len(latency_by_id) + len(all_clock_sync) + len(all_sink_time)
    for path_name in path_names:
        frame = _encode_control_frame(path_ids[path_name], payload)
        path_sockets[path_name].sendto(frame, path_targets[path_name])
        _control_metadata.record_control_metadata_sent(
            control_metadata,
            len(frame),
            message_count=message_count,
            path_metadata=metadata,
            path_capacity=path_capacity or {},
            path_latency=path_latency or {},
            internal_clock=_control_metadata.clock_sync_sent_status(all_clock_sync),
            sink_time=all_sink_time,
            ntp_sample=ntp_sample,
            path_name=path_name,
        )
        print(
            f"lab service: sent control path_metadata path={path_name} capacity_updates={len(capacity_by_id)} "
            f"latency_updates={len(latency_by_id)} clock_sync={len(all_clock_sync)} "
            f"sink_time={len(all_sink_time)} bytes={len(frame)} duplicated=all-paths",
            flush=True,
        )


class _PathCapacityDetector:
    """
    Detect directional path capacity from real lab traffic and cache it between runs.

    The first estimate comes from lab config. After traffic flows, qdisc counters tell us how much actually left the
    shaped interface and whether the kernel dropped excess packets. Python owns this interpretation because the
    scheduler will eventually turn these estimates into path weights; Rust should only receive the resulting policy.
    """

    def __init__(self, config: LabScenarioConfig, *, path_names: list[str], direction: str) -> None:
        self._config = config
        self._path_names = path_names
        self._direction = direction
        self._cache = _load_path_capacity_cache(config)
        self._estimates = self._initial_estimates(config, path_names)
        self._dirty = set(path_names)
        self._last_sample_at = time.monotonic()
        self._last_bytes = {path_name: 0 for path_name in path_names}
        self._last_payload_bytes = {path_name: 0 for path_name in path_names}
        self._last_drops = {path_name: 0 for path_name in path_names}
        self._sustained = {path_name: _empty_capacity_observation() for path_name in path_names}

    def snapshot(self) -> dict[str, dict[str, int | str | None]]:
        return {path_name: dict(self._estimates[path_name]) for path_name in self._path_names}

    def dirty_snapshot(self) -> dict[str, dict[str, int | str | None]]:
        return {
            path_name: dict(self._estimates[path_name]) for path_name in self._path_names if path_name in self._dirty
        }

    def mark_sent(self) -> None:
        self._dirty.clear()

    def observe(
        self,
        path_stats: dict[str, dict[str, int]],
        qdisc_stats: dict[str, dict[str, int]],
    ) -> dict[str, dict[str, int | str | None]]:
        now = time.monotonic()
        elapsed = now - self._last_sample_at
        if elapsed < _PATH_CAPACITY_DETECTION_WINDOW_SECONDS:
            return {}

        changed: dict[str, dict[str, int | str | None]] = {}
        for path_name in self._path_names:
            qdisc_rate_bps = _control_metadata.int_or_none(qdisc_stats.get(path_name, {}).get("rate_bps"))
            capacity_key = f"{self._direction}_bps"
            current_bps = int(self._estimates[path_name].get(capacity_key) or _PATH_CAPACITY_DEFAULT_BPS)
            current_bytes = _path_capacity_sample_bytes(path_name, path_stats, qdisc_stats)
            current_payload_bytes = path_stats.get(path_name, {}).get("bytes", 0)
            current_drops = qdisc_stats.get(path_name, {}).get("dropped", 0)
            delta_bytes = max(current_bytes - self._last_bytes.get(path_name, 0), 0)
            delta_payload_bytes = max(current_payload_bytes - self._last_payload_bytes.get(path_name, 0), 0)
            delta_drops = max(current_drops - self._last_drops.get(path_name, 0), 0)
            self._last_bytes[path_name] = current_bytes
            self._last_payload_bytes[path_name] = current_payload_bytes
            self._last_drops[path_name] = current_drops

            if delta_bytes < _PATH_CAPACITY_MIN_SAMPLE_BYTES or delta_payload_bytes < _PATH_CAPACITY_MIN_SAMPLE_BYTES:
                self._reset_sustained(path_name)
                continue

            observed_bps = max(int((delta_bytes * 8) / elapsed), 1)
            sample_bps = qdisc_rate_bps if qdisc_rate_bps is not None and qdisc_rate_bps > current_bps else observed_bps
            candidate_bps = self._sustained_candidate(path_name, current_bps, sample_bps, delta_bytes, elapsed, delta_drops)

            if candidate_bps is None or not _capacity_changed(current_bps, candidate_bps):
                continue

            self._estimates[path_name][capacity_key] = _step_capacity(current_bps, candidate_bps)
            self._estimates[path_name]["source"] = "detected"
            self._estimates[path_name]["updated_at"] = datetime.now(UTC).isoformat()
            self._dirty.add(path_name)
            changed[path_name] = dict(self._estimates[path_name])
            self._reset_sustained(path_name)

        self._last_sample_at = now
        if changed:
            _save_path_capacity_cache(self._config, self.snapshot())
        return changed

    def _initial_estimates(
        self,
        config: LabScenarioConfig,
        path_names: list[str],
    ) -> dict[str, dict[str, int | str | None]]:
        estimates: dict[str, dict[str, int | str | None]] = {}
        for path_name in path_names:
            cached = self._cache.get(path_name, {})
            path = next((candidate for candidate in config.paths if candidate.name == path_name), None)
            default_bps = _default_path_capacity_bps(path) if path is not None else _PATH_CAPACITY_DEFAULT_BPS
            estimates[path_name] = {
                "tx_bps": _control_metadata.int_or_none(cached.get("tx_bps")) if cached else None,
                "rx_bps": _control_metadata.int_or_none(cached.get("rx_bps")) if cached else None,
                "source": "cache" if cached else "config",
                "updated_at": cached.get("updated_at") if isinstance(cached.get("updated_at"), str) else None,
            }
            if estimates[path_name][self._direction + "_bps"] is None:
                estimates[path_name][self._direction + "_bps"] = default_bps
        return estimates

    def _sustained_candidate(
        self,
        path_name: str,
        current_bps: int,
        sample_bps: int,
        sample_bytes: int,
        elapsed: float,
        dropped_packets: int,
    ) -> int | None:
        direction = _capacity_sample_direction(current_bps, sample_bps, dropped_packets)
        if direction is None:
            self._reset_sustained(path_name)
            return None

        sustained = self._sustained[path_name]
        if sustained["direction"] != direction:
            sustained.update(_empty_capacity_observation(direction=direction))

        sustained["seconds"] = float(sustained["seconds"]) + elapsed
        sustained["bytes"] = int(sustained["bytes"]) + sample_bytes
        sustained["drops"] = int(sustained["drops"]) + dropped_packets

        required_seconds = (
            _PATH_CAPACITY_INCREASE_SUSTAIN_SECONDS
            if direction == "increase"
            else _PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS
        )
        if float(sustained["seconds"]) < required_seconds:
            return None
        if direction == "decrease" and int(sustained["drops"]) <= 0:
            return None

        average_bps = int((int(sustained["bytes"]) * 8) / max(float(sustained["seconds"]), 0.001))
        return int(average_bps * _PATH_CAPACITY_HEADROOM_RATIO)

    def _reset_sustained(self, path_name: str) -> None:
        self._sustained[path_name] = _empty_capacity_observation()


def _path_capacity_sample_bytes(
    path_name: str,
    path_stats: dict[str, dict[str, int]],
    qdisc_stats: dict[str, dict[str, int]],
) -> int:
    qdisc_row = qdisc_stats.get(path_name)
    if qdisc_row is not None and "sent_bytes" in qdisc_row:
        return qdisc_row["sent_bytes"]
    return path_stats.get(path_name, {}).get("bytes", 0)


def _empty_capacity_observation(*, direction: str | None = None) -> dict[str, float | int | str | None]:
    return {"direction": direction, "seconds": 0.0, "bytes": 0, "drops": 0}


def _capacity_sample_direction(current_bps: int, sample_bps: int, dropped_packets: int) -> str | None:
    if sample_bps > current_bps * (1 + _PATH_CAPACITY_MIN_CHANGE_RATIO):
        return "increase"
    if dropped_packets > 0 and sample_bps < current_bps * (1 - _PATH_CAPACITY_MIN_CHANGE_RATIO):
        return "decrease"
    return None


def _step_capacity(current_bps: int, candidate_bps: int) -> int:
    delta = candidate_bps - current_bps
    if delta == 0:
        return current_bps
    stepped = int(current_bps + (delta * _PATH_CAPACITY_ADJUSTMENT_RATIO))
    if delta > 0:
        return max(current_bps + 1, min(stepped, candidate_bps))
    return min(current_bps - 1, max(stepped, candidate_bps))


def _capacity_changed(current_bps: int, candidate_bps: int) -> bool:
    if current_bps <= 0:
        return True
    return abs(candidate_bps - current_bps) / current_bps >= _PATH_CAPACITY_MIN_CHANGE_RATIO


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


def _path_ids(path_names: list[str]) -> dict[str, int]:
    """Assign stable compact path ids for one lab service instance."""
    if len(path_names) > (1 << 16) - 1:
        raise ValueError("too many lab paths for the v1 u16 path id field")
    return {path_name: index + 1 for index, path_name in enumerate(path_names)}


def _path_names_by_id(path_names: list[str]) -> dict[int, str]:
    return {path_id: path_name for path_name, path_id in _path_ids(path_names).items()}


def _path_name_for_frame(
    frame: DataFrame,
    learned_path_names_by_id: dict[int, str],
    configured_path_names_by_id: dict[int, str],
) -> str:
    return (
        learned_path_names_by_id.get(frame.path_id)
        or configured_path_names_by_id.get(frame.path_id)
        or f"path-id:{frame.path_id}"
    )


def _rename_path_counter(path_stats: dict[str, dict[str, int]], old_name: str | None, new_name: str) -> None:
    """Move counters when control metadata teaches a nicer name after packets arrived."""
    path_stats.setdefault(new_name, _empty_path_counter())
    if old_name is None or old_name == new_name or old_name not in path_stats:
        return
    old_stats = path_stats.pop(old_name)
    for key, value in old_stats.items():
        path_stats[new_name][key] = path_stats[new_name].get(key, 0) + value


@dataclass(frozen=True)
class _SequenceObservation:
    out_of_order: bool = False
    reorder_needed_packets: int = 0


class _LabSequenceTracker:
    """
    Wrap-safe global sequence tracker used by the Python lab service.

    This mirrors the protocol contract while the first lab still runs in Python.
    Missing packets remain pending until the delayed sequence arrives, so pure
    reorder does not permanently inflate `miss`.
    """

    def __init__(self) -> None:
        self.next_expected: int | None = None
        self.pending_missing: set[int] = set()
        self.out_of_order_packets = 0
        self.reorder_needed_packets = 0

    def observe(self, sequence: int) -> _SequenceObservation:
        if self.next_expected is None:
            self.next_expected = _wrap_sequence(sequence + 1)
            return _SequenceObservation()

        if sequence in self.pending_missing:
            self.pending_missing.remove(sequence)
            self.out_of_order_packets += 1
            self.reorder_needed_packets += 1
            return _SequenceObservation(out_of_order=True, reorder_needed_packets=1)

        expected = self.next_expected
        if sequence == expected:
            self.next_expected = _wrap_sequence(expected + 1)
            return _SequenceObservation()

        forward_distance = _sequence_distance(expected, sequence)
        if 0 < forward_distance < (1 << 63):
            self._mark_missing(expected, forward_distance)
            self.next_expected = _wrap_sequence(sequence + 1)
            return _SequenceObservation(reorder_needed_packets=forward_distance)

        self.out_of_order_packets += 1
        return _SequenceObservation(out_of_order=True)

    def pending_missing_by_path(self, path_names: list[str]) -> dict[str, int]:
        pending = {path_name: 0 for path_name in path_names}
        if not path_names:
            return pending
        for sequence in self.pending_missing:
            pending[_path_for_sequence(sequence, path_names)] += 1
        return pending

    def _mark_missing(self, first_sequence: int, count: int) -> None:
        # TODO(receiver-window): Replace this bounded lab set with the Rust receive window once the
        # dataplane owns buffering. The lab sends small enough test runs that explicit tracking keeps
        # the behavior easy to inspect.
        for offset in range(count):
            self.pending_missing.add(_wrap_sequence(first_sequence + offset))
        self.reorder_needed_packets += count


def _wrap_sequence(value: int) -> int:
    return value % _SEQUENCE_SPACE


def _sequence_distance(start: int, end: int) -> int:
    return (end - start) % _SEQUENCE_SPACE


def _path_for_sequence(sequence: int, path_names: list[str]) -> str:
    return path_names[(sequence - 1) % len(path_names)]


def _path_stats_with_pending_missing(
    path_stats: dict[str, dict[str, int]],
    pending_missing_by_path: dict[str, int],
) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for path_name, stats in path_stats.items():
        row = dict(stats)
        row["missed_packets"] = row.get("missed_packets", 0) + pending_missing_by_path.get(path_name, 0)
        merged[path_name] = row
    return merged


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


def _total_missed_packets(
    path_stats: dict[str, dict[str, int]],
    qdisc_stats: dict[str, dict[str, int]],
) -> int:
    return sum(
        stats.get("missed_packets", 0) + qdisc_stats.get(path_name, {}).get("dropped", 0)
        for path_name, stats in path_stats.items()
    )


def _fixed_payload(payload: bytes, payload_size: int | None) -> bytes:
    if payload_size is None:
        return payload
    if len(payload) >= payload_size:
        return payload[:payload_size]
    return payload + (b"x" * (payload_size - len(payload)))


def _reply_payload(payload: bytes) -> bytes:
    """Build a reply payload with the same size as the packet that triggered it."""
    return _fixed_payload(b"gatherlink-reply", len(payload))


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


def _service_command(config: LabScenarioConfig, config_path: Path) -> list[str]:
    base = [sys.executable, "-m", "gatherlink.cli.main", "lab", "service", str(config_path)]
    return _namespace_service_command(config, _client_namespace(config), base)


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
    try:
        record = ServiceRegistry().resolve(_lab_sink_service_name(config))
    except ValueError:
        return None
    return record if record.is_running() else None


def _running_forwarder_record(config: LabScenarioConfig) -> ServiceRecord | None:
    try:
        record = ServiceRegistry().resolve(_lab_service_name(config))
    except ValueError:
        return None
    return record if record.is_running() else None


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
