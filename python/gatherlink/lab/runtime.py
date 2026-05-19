"""Runtime helpers for local lab scenarios."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Protocol

from gatherlink.lab.scenarios import (
    LabPathConfig,
    LabScenarioConfig,
    LabShapeConfig,
    LabShapeProfileConfig,
    LabShapeSide,
)
from gatherlink.runtime.services import (
    ServiceIpcError,
    ServiceIpcServer,
    ServiceRecord,
    ServiceRegistry,
    request_service,
    service_name,
)


class CommandRunner(Protocol):
    """Small command runner interface used by lab setup tests."""

    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one command."""


class SubprocessRunner:
    """Run lab setup commands through subprocess."""

    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, check=check, text=True, capture_output=True)


@dataclass(frozen=True)
class PathSetupResult:
    """Result for one simulated path setup operation."""

    name: str
    status: str
    client_namespace: str
    server_namespace: str
    client_interface: str
    server_interface: str
    shape_actions: list[str]


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
class ShapeApplyResult:
    """Result from applying live link shaping to one path."""

    name: str
    side: str
    client_namespace: str
    server_namespace: str
    client_interface: str
    server_interface: str
    actions: list[str]


@dataclass(frozen=True)
class LabCleanupResult:
    """Result from removing one lab-owned network namespace."""

    namespace: str
    status: str
    action: str = "delete_namespace"


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


def prepare_lab_runtime(config: LabScenarioConfig, *, runner: CommandRunner | None = None) -> list[PathSetupResult]:
    """Prepare runtime directory and root-owned simulated network paths."""
    runner = runner or SubprocessRunner()
    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "scenario.json").write_text(
        json.dumps(config.export_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )

    results: list[PathSetupResult] = []
    for path in config.paths:
        result = _ensure_path(config, path, runner=runner)
        if path.shape != LabShapeConfig():
            shape_result = apply_lab_shape(config, path.name, path.shape, runner=runner)
            result = PathSetupResult(
                name=result.name,
                status=result.status,
                client_namespace=result.client_namespace,
                server_namespace=result.server_namespace,
                client_interface=result.client_interface,
                server_interface=result.server_interface,
                shape_actions=shape_result.actions,
            )
        results.append(result)
    return results


def apply_lab_profile(
    config: LabScenarioConfig,
    profile_name: str,
    *,
    runner: CommandRunner | None = None,
) -> list[ShapeApplyResult]:
    """Apply a named live shaping profile to existing lab paths."""
    if profile_name not in config.profiles:
        raise ValueError(f"unknown lab profile: {profile_name}")
    runner = runner or SubprocessRunner()
    return [
        apply_lab_shape(config, path_name, shape, side="both", runner=runner)
        for path_name, shape in config.profiles[profile_name].items()
    ]


def apply_lab_shape_profile(
    config: LabScenarioConfig,
    profile: LabShapeProfileConfig,
    *,
    runner: CommandRunner | None = None,
) -> list[ShapeApplyResult]:
    """Apply a standalone shaping config to an existing lab."""
    runner = runner or SubprocessRunner()
    results: list[ShapeApplyResult] = []
    for target in profile.targets:
        if target.clear:
            results.append(clear_lab_shape(config, target.path, side=target.side, runner=runner))
        else:
            results.append(apply_lab_shape(config, target.path, target.shape, side=target.side, runner=runner))
    return results


def apply_lab_shape(
    config: LabScenarioConfig,
    path_name: str,
    shape: LabShapeConfig,
    *,
    side: LabShapeSide = "both",
    runner: CommandRunner | None = None,
) -> ShapeApplyResult:
    """Apply live MTU, link state, and traffic shaping to an existing path."""
    runner = runner or SubprocessRunner()
    path = _path_by_name(config, path_name)
    handle = _path_handle(config, path)
    actions: list[str] = []

    if shape.mtu is not None:
        for namespace, interface in handle.interfaces_for_side(side):
            _sudo_ip(["-n", namespace, "link", "set", interface, "mtu", str(shape.mtu)], runner=runner)
        actions.append(f"mtu={shape.mtu}")

    if shape.state is not None:
        for namespace, interface in handle.interfaces_for_side(side):
            _sudo_ip(["-n", namespace, "link", "set", interface, shape.state], runner=runner)
        actions.append(f"state={shape.state}")

    if shape.blackhole:
        for namespace, interface in handle.interfaces_for_side(side):
            _sudo_tc(
                ["-n", namespace, "qdisc", "replace", "dev", interface, "root", "netem", "loss", "100%"], runner=runner
            )
        actions.append("blackhole=true")
    elif _has_netem_shape(shape):
        netem_args = _netem_args(shape)
        for namespace, interface in handle.interfaces_for_side(side):
            _sudo_tc(
                ["-n", namespace, "qdisc", "replace", "dev", interface, "root", "netem", *netem_args], runner=runner
            )
        actions.append("tc=netem")

    if not actions:
        actions.append("no_change")

    return ShapeApplyResult(
        name=path.name,
        side=side,
        client_namespace=handle.client_namespace,
        server_namespace=handle.server_namespace,
        client_interface=handle.client_interface,
        server_interface=handle.server_interface,
        actions=actions,
    )


def clear_lab_shape(
    config: LabScenarioConfig,
    path_name: str,
    *,
    side: LabShapeSide = "both",
    runner: CommandRunner | None = None,
) -> ShapeApplyResult:
    """Clear live qdisc shaping and bring a path back up."""
    runner = runner or SubprocessRunner()
    path = _path_by_name(config, path_name)
    handle = _path_handle(config, path)
    for namespace, interface in handle.interfaces_for_side(side):
        _sudo_tc(["-n", namespace, "qdisc", "del", "dev", interface, "root"], runner=runner, check=False)
        _sudo_ip(["-n", namespace, "link", "set", interface, "up"], runner=runner, check=False)
    return ShapeApplyResult(
        name=path.name,
        side=side,
        client_namespace=handle.client_namespace,
        server_namespace=handle.server_namespace,
        client_interface=handle.client_interface,
        server_interface=handle.server_interface,
        actions=["clear_qdisc", "state=up"],
    )


def cleanup_lab_runtime(config: LabScenarioConfig, *, runner: CommandRunner | None = None) -> list[LabCleanupResult]:
    """Remove lab-owned network namespaces and the veth interfaces inside them."""
    runner = runner or SubprocessRunner()
    results: list[LabCleanupResult] = []
    seen_namespaces: set[str] = set()

    for path in config.paths:
        handle = _path_handle(config, path)
        for namespace in [handle.client_namespace, handle.server_namespace]:
            if namespace in seen_namespaces:
                continue
            seen_namespaces.add(namespace)
            # TODO(cleanup-scope): Keep cleanup intentionally namespace-focused. Deleting the namespace removes the
            # veth interfaces, qdiscs, addresses, and link state created under it while preserving logs for debugging.
            result = _sudo_ip(["netns", "del", namespace], runner=runner, check=False)
            status = "removed" if result.returncode == 0 else "absent_or_already_removed"
            results.append(LabCleanupResult(namespace=namespace, status=status))

    return results


def start_lab_service(config_path: Path, config: LabScenarioConfig) -> ServiceStartResult:
    """Start the foreground lab service as an unprivileged background process."""
    return _start_lab_process_service(
        config_path,
        config,
        service_name_value=_lab_service_name(config),
        command=_service_command(config_path),
        log_file=_log_file(config),
        metadata_role="forwarder",
    )


def start_lab_sink_service(config_path: Path, config: LabScenarioConfig) -> ServiceStartResult:
    """Start the foreground lab sink service as an unprivileged background process."""
    return _start_lab_process_service(
        config_path,
        config,
        service_name_value=_lab_sink_service_name(config),
        command=_sink_service_command(config_path),
        log_file=_sink_log_file(config),
        metadata_role="sink",
    )


def _start_lab_process_service(
    config_path: Path,
    config: LabScenarioConfig,
    *,
    service_name_value: str,
    command: list[str],
    log_file: Path,
    metadata_role: str,
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
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
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
    target = _split_host_port(config.traffic.target)
    family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    sock.bind((listen_host, listen_port))

    path_names = [path.name for path in config.paths] or ["default"]
    packet_count = 0
    byte_count = 0
    started_at = datetime.now(UTC)
    ipc = ServiceIpcServer(
        service_record,
        status=lambda: {
            "running": not stop_event.is_set(),
            "listen": config.traffic.listen,
            "target": config.traffic.target,
            "paths": path_names,
            "packets": packet_count,
            "bytes": byte_count,
            "started_at": started_at.isoformat(),
        },
        stop=stop_event.set,
    )
    ipc.start()
    print(
        f"lab service: listening udp={config.traffic.listen} target={config.traffic.target} "
        f"paths={','.join(path_names)} ipc={service_record.ipc_socket}",
        flush=True,
    )
    print("lab service: press Ctrl-C to stop", flush=True)

    try:
        while not stop_event.is_set():
            try:
                payload, source = sock.recvfrom(65535)
            except TimeoutError:
                continue
            path = path_names[packet_count % len(path_names)]
            sent = sock.sendto(payload, target)
            packet_count += 1
            byte_count += sent
            print(
                f"lab service: forwarded packet={packet_count} bytes={sent} total_bytes={byte_count} "
                f"path={path} source={source}",
                flush=True,
            )
    except KeyboardInterrupt:
        print(f"lab service: stopped packets={packet_count} bytes={byte_count}", flush=True)
    finally:
        ipc.close()
        sock.close()


def send_udp_packets(
    config: LabScenarioConfig,
    *,
    payload: str = "gatherlink-lab",
    count: int = 5,
    interval_seconds: float = 0.05,
) -> UdpSendResult:
    """Send small UDP payloads into the lab service listener."""
    target = _split_host_port(config.traffic.listen)
    family = socket.AF_INET6 if ":" in target[0] else socket.AF_INET
    encoded = payload.encode("utf-8")
    packet_bytes = 0
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        for index in range(count):
            message = encoded if count == 1 else encoded + f"-{index + 1}".encode("ascii")
            packet_bytes += sock.sendto(message, target)
            if interval_seconds > 0 and index + 1 < count:
                time.sleep(interval_seconds)
    return UdpSendResult(target=config.traffic.listen, packets=count, bytes=packet_bytes)


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
    listen_host, listen_port = _split_host_port(config.traffic.target)
    family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
    packets = 0
    packet_bytes = 0
    last_payload = ""
    last_payload_bytes = 0
    last_source = ""
    started_at = datetime.now(UTC)
    ipc = ServiceIpcServer(
        service_record,
        status=lambda: {
            "running": not stop_event.is_set(),
            "listen": config.traffic.target,
            "packets": packets,
            "bytes": packet_bytes,
            "last_payload": last_payload,
            "last_payload_bytes": last_payload_bytes,
            "last_source": last_source,
            "started_at": started_at.isoformat(),
        },
        stop=stop_event.set,
    )
    ipc.start()
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.2)
        sock.bind((listen_host, listen_port))
        print(f"lab sink service: listening udp={config.traffic.target} ipc={service_record.ipc_socket}", flush=True)
        print("lab sink service: press Ctrl-C to stop", flush=True)
        try:
            while not stop_event.is_set():
                try:
                    payload, source = sock.recvfrom(65535)
                except TimeoutError:
                    continue
                packets += 1
                packet_bytes += len(payload)
                last_payload = payload.decode("utf-8", errors="replace")
                last_payload_bytes = len(payload)
                last_source = repr(source)
                print(
                    f"lab sink service: received packet={packets} bytes={len(payload)} "
                    f"total_bytes={packet_bytes} source={source} payload={last_payload}",
                    flush=True,
                )
        except KeyboardInterrupt:
            print(f"lab sink service: stopped packets={packets} bytes={packet_bytes}", flush=True)
        finally:
            ipc.close()


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
        send_udp_packets(config, payload=payload, count=count, interval_seconds=0)
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


def inspect_lab_interfaces(config: LabScenarioConfig, *, runner: CommandRunner | None = None) -> list[str]:
    """Return `ip addr show` output for each lab namespace interface."""
    runner = runner or SubprocessRunner()
    outputs: list[str] = []
    for path in config.paths:
        handle = _path_handle(config, path)
        for namespace, interface in handle.namespace_interfaces:
            result = _sudo_ip(["-n", namespace, "addr", "show", "dev", interface], runner=runner)
            outputs.append(f"# {path.name}: {namespace}/{interface}\n{result.stdout.strip()}")
    return outputs


def _ensure_path(config: LabScenarioConfig, path: LabPathConfig, *, runner: CommandRunner) -> PathSetupResult:
    handle = _path_handle(config, path)
    client_ns = handle.client_namespace
    server_ns = handle.server_namespace
    client_if = handle.client_interface
    server_if = handle.server_interface

    _sudo_ip(["netns", "add", client_ns], runner=runner, check=False)
    _sudo_ip(["netns", "add", server_ns], runner=runner, check=False)
    client_link_exists = (
        _sudo_ip(["-n", client_ns, "link", "show", client_if], runner=runner, check=False).returncode == 0
    )
    server_link_exists = (
        _sudo_ip(["-n", server_ns, "link", "show", server_if], runner=runner, check=False).returncode == 0
    )
    status = "reused" if client_link_exists and server_link_exists else "created"

    if status == "created":
        _sudo_ip(["link", "add", client_if, "type", "veth", "peer", "name", server_if], runner=runner, check=False)
        _sudo_ip(["link", "set", client_if, "netns", client_ns], runner=runner)
        _sudo_ip(["link", "set", server_if, "netns", server_ns], runner=runner)

    prefix = ipaddress.ip_network(path.subnet, strict=False).prefixlen
    _sudo_ip(
        ["-n", client_ns, "addr", "add", f"{path.client_address}/{prefix}", "dev", client_if],
        runner=runner,
        check=False,
    )
    _sudo_ip(
        ["-n", server_ns, "addr", "add", f"{path.server_address}/{prefix}", "dev", server_if],
        runner=runner,
        check=False,
    )
    _sudo_ip(["-n", client_ns, "link", "set", "lo", "up"], runner=runner, check=False)
    _sudo_ip(["-n", server_ns, "link", "set", "lo", "up"], runner=runner, check=False)
    _sudo_ip(["-n", client_ns, "link", "set", client_if, "up"], runner=runner)
    _sudo_ip(["-n", server_ns, "link", "set", server_if, "up"], runner=runner)

    return PathSetupResult(
        name=path.name,
        status=status,
        client_namespace=client_ns,
        server_namespace=server_ns,
        client_interface=client_if,
        server_interface=server_if,
        shape_actions=[],
    )


def _sudo_ip(args: list[str], *, runner: CommandRunner, check: bool = True) -> subprocess.CompletedProcess[str]:
    return runner.run(["sudo", "ip", *args], check=check)


def _sudo_tc(args: list[str], *, runner: CommandRunner, check: bool = True) -> subprocess.CompletedProcess[str]:
    return runner.run(["sudo", "tc", *args], check=check)


def _split_host_port(value: str) -> tuple[str, int]:
    if value.startswith("["):
        host, port = value.rsplit("]:", maxsplit=1)
        return host[1:], int(port)
    host, port = value.rsplit(":", maxsplit=1)
    return host, int(port)


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


def _service_command(config_path: Path) -> list[str]:
    base = [sys.executable, "-m", "gatherlink.cli.main", "lab", "service", str(config_path)]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        user = _service_user()
        if user == "root":
            raise RuntimeError("cannot choose an unprivileged user; rerun with SUDO_USER set")
        return ["sudo", "-u", user, "-E", *base]
    return base


def _sink_service_command(config_path: Path) -> list[str]:
    base = [sys.executable, "-m", "gatherlink.cli.main", "lab", "sink-service", str(config_path)]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        user = _service_user()
        if user == "root":
            raise RuntimeError("cannot choose an unprivileged user; rerun with SUDO_USER set")
        return ["sudo", "-u", user, "-E", *base]
    return base


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
    registry = ServiceRegistry()
    try:
        return registry.resolve(_lab_service_name(config))
    except ValueError:
        record = ServiceRecord(
            name=_lab_service_name(config),
            kind="lab",
            manager="process",
            pid=os.getpid(),
            log_file=_log_file(config),
            detached_from_console=False,
            cwd=Path.cwd(),
            metadata={
                "runtime_dir": config.runtime_dir,
                "scenario": config.scenario,
                "security_mode": config.security.mode,
                "role": "forwarder",
            },
        )
        return registry.register(record)


def _ensure_lab_sink_service_record(config: LabScenarioConfig) -> ServiceRecord:
    registry = ServiceRegistry()
    try:
        return registry.resolve(_lab_sink_service_name(config))
    except ValueError:
        record = ServiceRecord(
            name=_lab_sink_service_name(config),
            kind="lab",
            manager="process",
            pid=os.getpid(),
            log_file=_sink_log_file(config),
            detached_from_console=False,
            cwd=Path.cwd(),
            metadata={
                "runtime_dir": config.runtime_dir,
                "scenario": config.scenario,
                "security_mode": config.security.mode,
                "role": "sink",
            },
        )
        return registry.register(record)


def _running_sink_record(config: LabScenarioConfig) -> ServiceRecord | None:
    try:
        record = ServiceRegistry().resolve(_lab_sink_service_name(config))
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


@dataclass(frozen=True)
class _PathHandle:
    client_namespace: str
    server_namespace: str
    client_interface: str
    server_interface: str

    @property
    def namespace_interfaces(self) -> list[tuple[str, str]]:
        return [
            (self.client_namespace, self.client_interface),
            (self.server_namespace, self.server_interface),
        ]

    def interfaces_for_side(self, side: LabShapeSide) -> list[tuple[str, str]]:
        if side == "local":
            return [(self.client_namespace, self.client_interface)]
        if side == "remote":
            return [(self.server_namespace, self.server_interface)]
        return self.namespace_interfaces


def _path_handle(config: LabScenarioConfig, path: LabPathConfig) -> _PathHandle:
    safe_path = path.name.replace("_", "-")[:6]
    return _PathHandle(
        client_namespace=f"glab-{config.name}-client",
        server_namespace=f"glab-{config.name}-server",
        client_interface=f"gl{safe_path}c"[:15],
        server_interface=f"gl{safe_path}s"[:15],
    )


def _path_by_name(config: LabScenarioConfig, path_name: str) -> LabPathConfig:
    for path in config.paths:
        if path.name == path_name:
            return path
    raise ValueError(f"unknown lab path: {path_name}")


def _has_netem_shape(shape: LabShapeConfig) -> bool:
    return any([shape.rate, shape.delay, shape.jitter, shape.loss, shape.reorder])


def _netem_args(shape: LabShapeConfig) -> list[str]:
    args: list[str] = []
    if shape.rate:
        args.extend(["rate", shape.rate])
    if shape.delay:
        args.extend(["delay", shape.delay])
        if shape.jitter:
            args.append(shape.jitter)
    if shape.loss:
        args.extend(["loss", shape.loss])
    if shape.reorder:
        args.extend(["reorder", shape.reorder])
    return args
