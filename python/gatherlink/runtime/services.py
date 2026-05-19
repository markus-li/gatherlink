"""
Shared service registry and log attachment helpers.

The control plane starts several long-running processes over time: local labs,
core Gatherlink services, and helper daemons. This module gives them one small
registry so operators can list what exists and attach to logs without knowing
where each subsystem stores its PID file.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from typing import Any, Literal

from pydantic import Field

from gatherlink.platform.debian import default_debian_backend
from gatherlink.shared.models.base import GatherlinkBaseModel

SERVICE_REGISTRY_ENV = "GATHERLINK_SERVICE_REGISTRY"
DEFAULT_SERVICE_REGISTRY = Path(".gatherlink/services")
ServiceManager = Literal["process", "systemd"]
SERVICE_RECORD_FILE = "service.json"
SERVICE_PID_FILE = "current.pid"
SERVICE_IPC_SOCKET = "control.sock"


class ServiceRecord(GatherlinkBaseModel):
    """Registry entry for one process managed by the Python control plane."""

    name: str
    kind: str
    manager: ServiceManager = "process"
    pid: int | None = None
    pid_file: Path | None = None
    log_file: Path
    ipc_socket: Path | None = None
    systemd_unit: str | None = None
    detached_from_console: bool = True
    command: list[str] = Field(default_factory=list)
    cwd: Path = Field(default_factory=Path.cwd)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    def current_pid(self) -> int | None:
        """Return the latest PID from the service folder when available."""
        if self.pid_file and self.pid_file.exists():
            raw_pid = self.pid_file.read_text(encoding="utf-8").strip()
            return int(raw_pid) if raw_pid else None
        return self.pid

    def is_running(self) -> bool:
        """Return whether the registered PID currently exists."""
        if self.manager == "systemd":
            return bool(self.systemd_unit and default_debian_backend().systemd_is_active(self.systemd_unit))
        pid = self.current_pid()
        return pid_is_running(pid)

    def status_label(self) -> str:
        """Return a human-readable lifecycle state for listings."""
        if self.manager == "systemd":
            return "systemd:active" if self.is_running() else "systemd:inactive"
        return "running" if self.is_running() else "stopped"


class ServiceRegistry:
    """Discover and update per-service registry records."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_service_registry_path()

    def list(self) -> list[ServiceRecord]:
        """Return all known service records sorted by stable name."""
        if not self.path.exists():
            return []
        records: list[ServiceRecord] = []
        for service_file in self.path.glob(f"*/{SERVICE_RECORD_FILE}"):
            records.append(self._refresh_record(self._read_record(service_file)))
        return sorted(records, key=lambda record: record.name)

    def register(self, record: ServiceRecord, *, replace: bool = True) -> ServiceRecord:
        """
        Store a service record.

        ``replace=True`` is used for stable service names such as a lab scenario.
        Future launchers can pass ``replace=False`` to get an auto-suffixed name
        when several matching services are intentionally alive at once.
        """
        existing = self.list()
        existing_names = {service.name for service in existing}
        if not replace and record.name in existing_names:
            record = record.model_copy(update={"name": self.allocate_name(record.name, existing)})
        record = self._with_service_owned_paths(record)
        self._write_record(record)
        return record

    def mark_stopped(self, name: str) -> None:
        """Keep a stopped service discoverable while clearing its active PID."""
        service = self.resolve(name)
        if service.pid_file:
            service.pid_file.unlink(missing_ok=True)
        if service.ipc_socket:
            service.ipc_socket.unlink(missing_ok=True)
        service = service.model_copy(
            update={
                "pid": None,
                "metadata": {**service.metadata, "last_status": "stopped", "stopped_at": _utc_now_text()},
            }
        )
        self._write_record(service)

    def close(self, query: str) -> ServiceRecord:
        """
        Stop a process-managed service by PID and mark it stopped.

        Systemd-owned services are intentionally not stopped here. Their unit
        name is listed so an operator or a future systemd adapter can manage
        them through systemd's own lifecycle controls.
        """
        service = self.resolve(query)
        if service.manager == "systemd":
            raise ValueError(f"{service.name} is managed by systemd unit {service.systemd_unit or '[unknown]'}")
        pid = service.current_pid()
        if pid is not None and service.is_running():
            try:
                request_service(service, "stop")
            except ServiceIpcError:
                os.kill(pid, signal.SIGTERM)
            if pid != os.getpid() and not wait_for_pid_exit(pid, timeout_seconds=2.0):
                os.kill(pid, signal.SIGTERM)
                wait_for_pid_exit(pid, timeout_seconds=2.0)
        self.mark_stopped(service.name)
        return service

    def resolve(self, query: str) -> ServiceRecord:
        """Resolve an exact, prefix, or unique substring service name."""
        services = self.list()
        for service in services:
            if service.name == query:
                return service

        matches = [service for service in services if service.name.startswith(query)]
        if not matches:
            matches = [service for service in services if query in service.name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"unknown service: {query}")
        names = ", ".join(service.name for service in matches)
        raise ValueError(f"service name is ambiguous: {query} matched {names}")

    @staticmethod
    def allocate_name(base_name: str, services: list[ServiceRecord]) -> str:
        """Return ``base_name`` or a readable numeric suffix not already in use."""
        names = {service.name for service in services}
        if base_name not in names:
            return base_name
        suffix = 2
        while f"{base_name}-{suffix}" in names:
            suffix += 1
        return f"{base_name}-{suffix}"

    def _read_record(self, path: Path) -> ServiceRecord:
        return ServiceRecord(**json.loads(path.read_text(encoding="utf-8")))

    def _refresh_record(self, record: ServiceRecord) -> ServiceRecord:
        """Clean stale process PID/socket state while preserving the service folder."""
        if record.manager != "process":
            return record
        pid = record.current_pid()
        if pid is None or pid_is_running(pid):
            return record

        if record.pid_file:
            record.pid_file.unlink(missing_ok=True)
        if record.ipc_socket:
            record.ipc_socket.unlink(missing_ok=True)
        refreshed = record.model_copy(
            update={
                "pid": None,
                "metadata": {**record.metadata, "last_status": "stale_pid_cleaned", "cleaned_at": _utc_now_text()},
            }
        )
        self._write_record(refreshed)
        return refreshed

    def _write_record(self, record: ServiceRecord) -> None:
        service_dir = self._service_dir(record.name)
        service_dir.mkdir(parents=True, exist_ok=True)
        if record.manager == "process" and record.pid is not None and record.pid_file:
            record.pid_file.write_text(f"{record.pid}\n", encoding="utf-8")
        payload = json.dumps(record.export_dict(), indent=2, sort_keys=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=service_dir, delete=False) as handle:
            handle.write(payload)
            handle.write("\n")
            temporary_path = Path(handle.name)
        temporary_path.replace(service_dir / SERVICE_RECORD_FILE)

    def _with_service_owned_paths(self, record: ServiceRecord) -> ServiceRecord:
        service_dir = self._service_dir(record.name)
        if record.manager == "process":
            return record.model_copy(
                update={
                    "pid_file": service_dir / SERVICE_PID_FILE,
                    "ipc_socket": service_dir / SERVICE_IPC_SOCKET,
                }
            )
        return record.model_copy(update={"pid": None, "pid_file": None, "ipc_socket": None})

    def _service_dir(self, name: str) -> Path:
        return self.path / service_dir_name(name)


def default_service_registry_path() -> Path:
    """Return the registry path, allowing tests and operators to override it."""
    configured = os.environ.get(SERVICE_REGISTRY_ENV)
    return Path(configured) if configured else DEFAULT_SERVICE_REGISTRY


def service_name(kind: str, name: str) -> str:
    """Build the conventional registry name for a managed service."""
    safe_name = name.replace("_", "-").replace(" ", "-")
    return f"{kind}.{safe_name}"


def service_dir_name(name: str) -> str:
    """Return a filesystem-safe directory name for a service record."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "service"


def pid_is_running(pid: int | None) -> bool:
    """Return whether a PID exists and can be signalled by this user."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for_pid_exit(pid: int, *, timeout_seconds: float) -> bool:
    """Wait briefly for a process to exit after a graceful stop request."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not pid_is_running(pid)


class ServiceIpcError(RuntimeError):
    """Raised when service IPC cannot complete."""


class ServiceIpcServer:
    """Tiny JSON-over-Unix-socket control server for one managed service."""

    def __init__(
        self,
        record: ServiceRecord,
        *,
        status: Callable[[], dict[str, Any]],
        stop: Callable[[], None],
        commands: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        if record.ipc_socket is None:
            raise ValueError("process service records must define ipc_socket")
        self.record = record
        self.socket_path = record.ipc_socket
        self._status = status
        self._stop = stop
        self._commands = commands or {}
        self._closed = Event()
        self._thread: Thread | None = None
        self._server: socket.socket | None = None

    def start(self) -> None:
        """Start the background IPC listener."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(5)
        server.settimeout(0.2)
        self._server = server
        self._thread = Thread(target=self._serve, name=f"{self.record.name}-ipc", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop listening and remove the socket file."""
        self._closed.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.socket_path.unlink(missing_ok=True)

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                assert self._server is not None
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            Thread(
                target=self._handle_client,
                args=(client,),
                name=f"{self.record.name}-ipc-client",
                daemon=True,
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        """
        Serve one IPC client without blocking the listener.

        Some service commands intentionally run for seconds, such as lab reverse
        traffic generation. Status and stop requests must still work while those
        commands are in progress, so each accepted connection gets a tiny worker.
        """
        with client:
            response = self._handle_request(_read_ipc_request(client))
            client.sendall((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))

    def _handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if command == "status":
            return {"ok": True, "service": self.record.name, "status": self._status()}
        if command == "stop":
            self._stop()
            return {"ok": True, "service": self.record.name, "status": "stopping"}
        if isinstance(command, str) and command in self._commands:
            try:
                result = self._commands[command](request)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "service": self.record.name, "result": result}
        return {"ok": False, "error": f"unknown command: {command}"}


def request_service(
    service: ServiceRecord,
    command: str,
    *,
    timeout_seconds: float = 2.0,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one IPC command to a process-managed service."""
    if service.ipc_socket is None:
        raise ServiceIpcError(f"{service.name} does not expose an IPC socket")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(service.ipc_socket))
            request = {"command": command, **(payload or {})}
            client.sendall((json.dumps(request, sort_keys=True) + "\n").encode("utf-8"))
            raw_response = _read_ipc_response(client)
    except OSError as exc:
        raise ServiceIpcError(f"{service.name} IPC failed: {exc}") from exc

    response = json.loads(raw_response)
    if not response.get("ok"):
        raise ServiceIpcError(str(response.get("error", "service IPC failed")))
    return response


def _read_ipc_request(client: socket.socket) -> dict[str, Any]:
    return json.loads(_read_ipc_response(client))


def _read_ipc_response(client: socket.socket) -> str:
    chunks: list[bytes] = []
    while True:
        chunk = client.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    if not chunks:
        raise ServiceIpcError("empty IPC response")
    return b"".join(chunks).split(b"\n", maxsplit=1)[0].decode("utf-8")


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


def iter_log_lines(
    path: Path,
    *,
    tail: int = 80,
    follow: bool = False,
    poll_seconds: float = 0.5,
) -> Iterator[str]:
    """
    Yield existing and optionally live-appended log lines.

    This is intentionally small and dependency-free. It gives the CLI a shared
    follow mode now, and the same function can later back richer terminal views.
    """
    emitted = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
        for line in lines[-tail:]:
            emitted += 1
            yield line.rstrip("\n")

        if not follow:
            return

        while True:
            line = handle.readline()
            if line:
                emitted += 1
                yield line.rstrip("\n")
            else:
                if emitted == 0:
                    # Keep an empty log attachment visibly alive without spam.
                    emitted += 1
                    yield f"waiting for log data in {path}"
                time.sleep(poll_seconds)
