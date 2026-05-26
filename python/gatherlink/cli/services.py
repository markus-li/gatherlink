"""Service registry and log attachment CLI commands."""

from __future__ import annotations

import json
import select
import sys
import termios
import time
import tty
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

import typer

from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.validation import validate_config_file
from gatherlink.control import MONITOR_CONTROL_REQUEST_REFRESH_SECONDS, MONITOR_CONTROL_REQUEST_TTL_SECONDS
from gatherlink.platform.debian import default_debian_backend
from gatherlink.runtime.services import (
    ServiceIpcError,
    ServiceRecord,
    ServiceRegistry,
    iter_log_lines,
    request_service,
    service_name,
)
from gatherlink.scheduling.metrics import PathSchedulerMetrics
from gatherlink.scheduling.scoring import score_path

app = typer.Typer(help="List managed services and attach to their logs.")
AttachMode = Literal["raw", "aggregate"]
MonitorView = Literal["table", "graph"]
CONTEXT_WIDTH = 48
CTRL_WIDTH = 64
DETAIL_CTRL_WIDTH = 120


@app.command("list")
def list_services() -> None:
    """List local services and remote services learned through discovery."""
    services = ServiceRegistry().list()
    if not services:
        typer.echo("services: none")
        return

    for service in services:
        typer.echo(
            f"{service.name} kind={service.kind} manager={service.manager} state={service.status_label()} "
            f"pid={service.current_pid()} systemd_unit={service.systemd_unit} "
            f"detached={service.detached_from_console} log={service.log_file} cwd={service.cwd}"
        )
        for remote in _learned_remote_services(service):
            typer.echo(
                f"{remote['name']} kind=remote manager=remote state={remote['state']} "
                f"via={service.name} service_id={remote['service_id']} readonly=true"
            )


@app.command("register-systemd", hidden=True)
def register_systemd(
    name: str,
    unit: str,
    kind: str = typer.Option("core", help="Service kind to show in listings."),
) -> None:
    """Compatibility command for manually marking a systemd-owned service."""
    record = ServiceRegistry().register(
        ServiceRecord(
            name=name,
            kind=kind,
            manager="systemd",
            systemd_unit=unit,
            detached_from_console=False,
            log_file=Path(f"journal:{unit}"),
            metadata={"log_source": "journalctl"},
        )
    )
    typer.echo(f"registered {record.name} manager=systemd unit={unit}")


@app.command("register")
def register_config(
    path: Path,
    systemd: bool = typer.Option(False, "--systemd", help="Register the config as systemd-managed."),
    unit: str | None = typer.Option(None, help="Override the derived systemd unit name."),
) -> None:
    """Register a Gatherlink service from the same config used to run it."""
    if not systemd:
        typer.echo("only --systemd registration is supported until process launchers own this command", err=True)
        raise typer.Exit(1)
    try:
        record = _systemd_record_from_config(path, unit=unit)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    record = ServiceRegistry().register(record)
    typer.echo(f"registered {record.name} manager=systemd unit={record.systemd_unit} config={path}")


@app.command("logs")
def logs(
    name: str,
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep streaming appended log lines."),
    tail: int = typer.Option(80, help="Number of existing lines to show before following."),
) -> None:
    """Print logs for a registered service."""
    try:
        service = ServiceRegistry().resolve(name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if service.manager == "systemd":
        _print_systemd_log(service.systemd_unit, follow=follow, tail=tail)
        return
    _print_log(service.log_file, follow=follow, tail=tail)


@app.command("status")
def status(name: str) -> None:
    """Ask a process-managed service for live status over IPC."""
    try:
        service = ServiceRegistry().resolve(name)
        if service.manager == "systemd":
            typer.echo(f"{service.name} is managed by systemd unit {service.systemd_unit}")
            return
        response = request_service(service, "status")
    except (ValueError, ServiceIpcError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(response["status"], indent=2, sort_keys=True))


@app.command("outcome")
def outcome(
    name: str,
    service: str = typer.Option(..., "--service", help="Runtime service name the outcome belongs to."),
    degraded: bool = typer.Option(False, "--degraded/--ok", help="Whether the service outcome is degraded."),
    reason: str = typer.Option("", "--reason", help="Short operator-facing degradation reason."),
) -> None:
    """
    Push live service outcome facts to a process-managed service over IPC.

    This is for helper and benchmark tooling that knows application outcomes
    such as TCP retransmit pain or UDP helper loss. The core runner keeps the
    fact in Python memory and may use it in Python-owned service budgeting; Rust
    only sees any compiled primitive changes.
    """
    try:
        record = ServiceRegistry().resolve(name)
        if record.manager == "systemd":
            typer.echo(f"{record.name} is managed by systemd unit {record.systemd_unit}", err=True)
            raise typer.Exit(1)
        response = request_service(
            record,
            "service-outcome",
            payload={"outcomes": [{"service": service, "degraded": degraded, "reason": reason}]},
        )
    except (ValueError, ServiceIpcError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(response["result"], indent=2, sort_keys=True))


@app.command("attach")
def attach(
    name: str,
    tail: int = typer.Option(80, help="Number of existing lines to show before following."),
    mode: AttachMode = typer.Option("raw", help="Attach mode: raw packet logs or aggregate live counters."),
    interval: float = typer.Option(1.0, help="Seconds between aggregate refreshes."),
    once: bool = typer.Option(False, "--once", help="Render aggregate status once and exit."),
    view: MonitorView = typer.Option("table", help="Aggregate view: table or graph."),
) -> None:
    """Attach to a service log and keep following it."""
    try:
        service = ServiceRegistry().resolve(name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if mode == "aggregate":
        _print_aggregate([service], interval=interval, once=once, view=view)
        return
    typer.echo(f"attaching to {service.name} log={service.log_file}")
    if service.manager == "systemd":
        _print_systemd_log(service.systemd_unit, follow=True, tail=tail)
        return
    _print_log(service.log_file, follow=True, tail=tail)


@app.command("monitor")
def monitor(
    names: list[str] = typer.Argument(..., help="Service names, prefixes, or unique substrings to monitor."),
    interval: float = typer.Option(1.0, help="Seconds between refreshes."),
    once: bool = typer.Option(False, "--once", help="Render aggregate status once and exit."),
    view: MonitorView = typer.Option("table", help="Monitor view: table or graph. Press g to toggle interactively."),
) -> None:
    """Monitor one or more services as continuously refreshed aggregate counters."""
    registry = ServiceRegistry()
    try:
        services = [registry.resolve(name) for name in names]
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _print_aggregate(services, interval=interval, once=once, view=view)


@app.command("close")
def close(name: str) -> None:
    """Stop a process-managed Gatherlink service."""
    try:
        service = ServiceRegistry().close(name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"closed {service.name}")


@app.command("prune")
def prune() -> None:
    """Remove stopped process-managed service records from the registry."""
    removed = ServiceRegistry().prune_stopped()
    if not removed:
        typer.echo("pruned: none")
        return
    for name in removed:
        typer.echo(f"pruned {name}")


def _print_log(path: Path, *, follow: bool, tail: int) -> None:
    for line in iter_log_lines(path, follow=follow, tail=tail):
        typer.echo(line)


def _print_aggregate(
    services: list[ServiceRecord], *, interval: float, once: bool, view: MonitorView = "table"
) -> None:
    previous: dict[str, tuple[float, int, int]] = {}
    next_cadence_request_at: dict[str, float] = {}
    human_units = True
    speed_bits = True
    decimal_units = False
    with _KeyReader() as keys:
        while True:
            rows = []
            now = time.monotonic()
            for service in services:
                if not once and now >= next_cadence_request_at.get(service.name, 0.0):
                    _request_monitor_control_cadence(service)
                    _request_remote_status(service)
                    next_cadence_request_at[service.name] = now + MONITOR_CONTROL_REQUEST_REFRESH_SECONDS
                service_rows = _aggregate_rows_for_service(service, previous=previous, now=now)
                rows.extend(service_rows)
                for row in service_rows:
                    previous[str(row["row_key"])] = (now, int(row["tx_bytes"]), int(row["rx_bytes"]))

            output = _render_aggregate_rows(
                rows,
                refreshed_at=datetime.now().strftime("%H:%M:%S"),
                human_units=human_units,
                speed_bits=speed_bits,
                decimal_units=decimal_units,
                interactive=keys.enabled and not once,
                view=view,
            )
            if once:
                typer.echo(output)
                return
            typer.echo("\033[H\033[J" + output, nl=False)
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                key = keys.read()
                if key == "h":
                    human_units = not human_units
                    break
                if key == "b":
                    speed_bits = not speed_bits
                    break
                if key == "m":
                    decimal_units = not decimal_units
                    break
                if key == "g":
                    view = "graph" if view == "table" else "table"
                    break
                if key == "q":
                    typer.echo()
                    return
                time.sleep(0.05)


def _request_monitor_control_cadence(service: ServiceRecord) -> None:
    """Ask a service to temporarily publish higher-rate control metadata for diagnostics."""
    if service.manager == "systemd":
        return
    try:
        request_service(
            service,
            "control-cadence",
            payload={"profile": "monitor", "ttl_seconds": MONITOR_CONTROL_REQUEST_TTL_SECONDS},
        )
    except ServiceIpcError:
        # Older or narrower services may not implement the optional diagnostics
        # command yet. Monitoring counters should still work from their normal
        # status payload instead of making observability all-or-nothing.
        return


def _request_remote_status(service: ServiceRecord) -> None:
    """Ask a service to temporarily request read-only remote status snapshots."""
    if service.manager == "systemd":
        return
    try:
        request_service(
            service,
            "remote-status",
            payload={"ttl_seconds": MONITOR_CONTROL_REQUEST_TTL_SECONDS},
        )
    except ServiceIpcError:
        return


def _learned_remote_services(service: ServiceRecord) -> list[dict[str, object]]:
    """Return read-only service entries learned through production control metadata."""
    if service.manager == "systemd":
        return []
    try:
        status_payload = request_service(service, "status")["status"]
    except ServiceIpcError:
        return []
    control_metadata = status_payload.get("control_metadata")
    if not isinstance(control_metadata, dict):
        return []
    service_metadata = control_metadata.get("service_metadata")
    if not isinstance(service_metadata, dict):
        return []
    rows = []
    for service_id, remote_name in sorted(service_metadata.items(), key=lambda item: str(item[0])):
        rows.append(
            {
                "name": f"remote.{service.name}.{remote_name}",
                "service_id": service_id,
                "state": "learned",
            }
        )
    return rows


def _aggregate_rows_for_service(
    service: ServiceRecord,
    *,
    previous: dict[str, tuple[float, int, int]],
    now: float,
) -> list[dict[str, object]]:
    if service.manager == "systemd":
        return [
            {
                "row_key": service.name,
                "service": service.name,
                "state": "systemd",
                "tx_packets": 0,
                "tx_bytes": 0,
                "tx_speed_bytes_per_second": "not_reported",
                "rx_packets": 0,
                "rx_bytes": 0,
                "rx_speed_bytes_per_second": "not_reported",
                "missed": "not_reported",
                "expected_duplicate_packets": "not_reported",
                "duplicate_packets": "not_reported",
                "send_failed_packets": "not_reported",
                "fanout_send_failed_packets": "not_reported",
                "queue_pressure": "not_reported",
                "scheduler_health": "not_reported",
                "reordered": "not_reported",
                "reorder_needed": "not_reported",
                "row_type": "service",
                "parent": "",
                "path": "",
                "control_metadata": {},
                "system_time": _short_wall_now(),
                "gatherlink_time": "-",
                "ntp": "-",
                "extra": service.systemd_unit or "",
            }
        ]
    try:
        status = request_service(service, "status")["status"]
    except ServiceIpcError as exc:
        return [
            {
                "row_key": service.name,
                "service": service.name,
                "state": "ipc_error",
                "tx_packets": 0,
                "tx_bytes": 0,
                "tx_speed_bytes_per_second": "not_reported",
                "rx_packets": 0,
                "rx_bytes": 0,
                "rx_speed_bytes_per_second": "not_reported",
                "missed": "not_reported",
                "expected_duplicate_packets": "not_reported",
                "duplicate_packets": "not_reported",
                "send_failed_packets": "not_reported",
                "fanout_send_failed_packets": "not_reported",
                "queue_pressure": "not_reported",
                "scheduler_health": "not_reported",
                "reordered": "not_reported",
                "reorder_needed": "not_reported",
                "row_type": "service",
                "parent": "",
                "path": "",
                "control_metadata": {},
                "system_time": _short_wall_now(),
                "gatherlink_time": "-",
                "ntp": "-",
                "extra": str(exc),
            }
        ]

    service_row = _row_from_status(
        row_key=service.name,
        name=service.name,
        state="running" if status.get("running", service.is_running()) else "stopped",
        status=status,
        previous=previous.get(service.name),
        now=now,
        extra=_status_context(status),
    )
    service_row["row_type"] = "service"
    service_row["parent"] = ""
    service_row["path"] = ""
    control_metadata = status.get("control_metadata")
    service_row["control_metadata"] = control_metadata if isinstance(control_metadata, dict) else {}
    rows = [service_row]
    path_stats = status.get("path_stats", {})
    if isinstance(path_stats, dict):
        for path_name, path_status in path_stats.items():
            if not isinstance(path_status, dict):
                continue
            path_key = f"{service.name}::{path_name}"
            path_row = _row_from_status(
                row_key=path_key,
                name=f"  path {path_name}",
                state="path",
                status=path_status,
                previous=previous.get(path_key),
                now=now,
                extra=f"parent={service.name}",
            )
            path_row["row_type"] = "path"
            path_row["parent"] = service.name
            path_row["path"] = str(path_name)
            path_row["control_metadata"] = control_metadata if isinstance(control_metadata, dict) else {}
            rows.append(path_row)
    remote_status = status.get("remote_status")
    if isinstance(remote_status, dict):
        rows.extend(_remote_status_rows(service, remote_status, previous=previous, now=now))
    return rows


def _remote_status_rows(
    service: ServiceRecord,
    remote_status: dict[str, object],
    *,
    previous: dict[str, tuple[float, int, int]],
    now: float,
) -> list[dict[str, object]]:
    """Render remote IPC/status snapshots that another service copied over Gatherlink."""
    rows: list[dict[str, object]] = []
    for remote_name, envelope in remote_status.items():
        if not isinstance(envelope, dict):
            continue
        status = envelope.get("status")
        if not isinstance(status, dict):
            continue
        row_key = f"{service.name}::remote::{remote_name}"
        row = _row_from_status(
            row_key=row_key,
            name=f"  remote {remote_name}",
            state="remote",
            status=status,
            previous=previous.get(row_key),
            now=now,
            extra=(
                f"via={service.name} req={envelope.get('request_id', '-')} "
                f"path={envelope.get('source_path_id', '-')} rx={envelope.get('received_at', '-')}"
            ),
        )
        row["row_type"] = "service"
        row["parent"] = service.name
        row["path"] = ""
        row["control_metadata"] = (
            status.get("control_metadata") if isinstance(status.get("control_metadata"), dict) else {}
        )
        rows.append(row)
        path_stats = status.get("path_stats")
        if not isinstance(path_stats, dict):
            continue
        for path_name, path_status in path_stats.items():
            if not isinstance(path_status, dict):
                continue
            path_key = f"{row_key}::{path_name}"
            path_row = _row_from_status(
                row_key=path_key,
                name=f"    path {path_name}",
                state="path",
                status=path_status,
                previous=previous.get(path_key),
                now=now,
                extra=f"parent=remote {remote_name}",
            )
            path_row["row_type"] = "path"
            path_row["parent"] = f"remote {remote_name}"
            path_row["path"] = str(path_name)
            path_row["control_metadata"] = row["control_metadata"]
            rows.append(path_row)
    return rows


def _row_from_status(
    *,
    row_key: str,
    name: str,
    state: str,
    status: dict[str, object],
    previous: tuple[float, int, int] | None,
    now: float,
    extra: str,
) -> dict[str, object]:
    tx_packets, tx_bytes, rx_packets, rx_bytes = _directional_counters(status)
    if "current_tx_speed_bps" in status:
        tx_speed_bytes_per_second: int | str = int(status["current_tx_speed_bps"])
    elif "current_speed_bps" in status:
        tx_speed_bytes_per_second = int(status["current_speed_bps"])
    elif previous is None:
        tx_speed_bytes_per_second = 0
    else:
        elapsed = max(now - previous[0], 0.001)
        tx_speed_bytes_per_second = int((tx_bytes - previous[1]) / elapsed)
    if "current_rx_speed_bps" in status:
        rx_speed_bytes_per_second: int | str = int(status["current_rx_speed_bps"])
    elif previous is None:
        rx_speed_bytes_per_second = 0
    else:
        elapsed = max(now - previous[0], 0.001)
        rx_speed_bytes_per_second = int((rx_bytes - previous[2]) / elapsed)
    # Rust-backed and lab services both report this status shape. Python keeps the monitor generic so future
    # schedulers can consume the same real counters without special-casing the service implementation.
    return {
        "row_key": row_key,
        "service": name,
        "state": state,
        "tx_packets": tx_packets,
        "tx_bytes": tx_bytes,
        "tx_speed_bytes_per_second": tx_speed_bytes_per_second,
        "rx_packets": rx_packets,
        "rx_bytes": rx_bytes,
        "rx_speed_bytes_per_second": rx_speed_bytes_per_second,
        "missed": status.get("missed_packets", "not_reported"),
        "expected_duplicate_packets": status.get("expected_duplicate_packets", "not_reported"),
        "duplicate_packets": status.get("duplicate_packets", "not_reported"),
        "send_failed_packets": status.get("send_failed_packets", "not_reported"),
        "fanout_send_failed_packets": status.get("fanout_send_failed_packets", "not_reported"),
        "queue_pressure": _queue_pressure_context(status),
        "scheduler_health": _scheduler_health_context(status),
        "reordered": status.get("reordered_packets", "not_reported"),
        "reorder_needed": status.get("packets_needing_reorder", "not_reported"),
        "row_type": "service",
        "parent": "",
        "path": "",
        "control_metadata": status.get("control_metadata") if isinstance(status.get("control_metadata"), dict) else {},
        "service_config": status.get("service_config") if isinstance(status.get("service_config"), list) else [],
        "service_budget": status.get("service_budget") if isinstance(status.get("service_budget"), dict) else {},
        "auth_crypto_messages": (
            status.get("auth_crypto_messages") if isinstance(status.get("auth_crypto_messages"), list) else []
        ),
        "system_time": _system_time_context(status.get("control_metadata")),
        "gatherlink_time": _gatherlink_time_context(status.get("control_metadata")),
        "ntp": _ntp_context(status.get("control_metadata")),
        "extra": extra,
    }


def _render_aggregate_rows(
    rows: list[dict[str, object]],
    *,
    refreshed_at: str,
    human_units: bool,
    speed_bits: bool,
    decimal_units: bool,
    interactive: bool,
    view: MonitorView = "table",
) -> str:
    headers = [
        "service",
        "state",
        "txp",
        "txB",
        "tx/s",
        "rxp",
        "rxB",
        "rx/s",
        "miss",
        "xdup",
        "dup",
        "fail",
        "ffail",
        "queue",
        "sch",
        "ooo",
        "reord",
        "context",
    ]
    rendered_rows = [
        [
            str(row["service"]),
            str(row["state"]),
            str(row["tx_packets"]),
            _format_bytes(int(row["tx_bytes"]), human_units=human_units, decimal_units=decimal_units),
            _format_rate(
                row["tx_speed_bytes_per_second"],
                human_units=human_units,
                speed_bits=speed_bits,
                decimal_units=decimal_units,
            ),
            str(row["rx_packets"]),
            _format_bytes(int(row["rx_bytes"]), human_units=human_units, decimal_units=decimal_units),
            _format_rate(
                row["rx_speed_bytes_per_second"],
                human_units=human_units,
                speed_bits=speed_bits,
                decimal_units=decimal_units,
            ),
            _compact_counter(row["missed"]),
            _compact_counter(row["expected_duplicate_packets"]),
            _compact_counter(row["duplicate_packets"]),
            _compact_counter(row["send_failed_packets"]),
            _compact_counter(row["fanout_send_failed_packets"]),
            _compact_counter(row["queue_pressure"]),
            _compact_counter(row["scheduler_health"]),
            _compact_counter(row["reordered"]),
            _compact_counter(row["reorder_needed"]),
            _truncate(str(row["extra"]), CONTEXT_WIDTH),
        ]
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rendered_rows)) if rendered_rows else len(header)
        for index, header in enumerate(headers)
    ]
    table_width = sum(widths) + len(widths) - 1
    mode = "human units" if human_units else "raw bytes"
    speed_mode = "bit/s" if speed_bits else "byte/s"
    unit_base = "decimal" if decimal_units else "binary"
    keys = " | h units | b speed | m base | g graph/table | q quit" if interactive else ""
    title = (
        f"Gatherlink service monitor | refreshed {refreshed_at} | view {view} | "
        f"{mode} | {unit_base} | speed {speed_mode}{keys}"
    )
    lines = [title, "=" * max(table_width, len(title))]
    if view == "graph":
        lines.extend(_render_dependency_graph(rows))
    else:
        lines.append(" ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
        lines.append(" ".join("-" * width for width in widths))
        lines.extend(" ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rendered_rows)
    service_control = _render_service_control_rows(rows)
    if service_control:
        lines.append("")
        lines.extend(service_control)
    service_policy = _render_service_policy_rows(rows)
    if service_policy:
        lines.append("")
        lines.extend(service_policy)
    service_budget = _render_service_budget_rows(rows)
    if service_budget:
        lines.append("")
        lines.extend(service_budget)
    auth_crypto = _render_auth_crypto_rows(rows)
    if auth_crypto:
        lines.append("")
        lines.extend(auth_crypto)
    path_control = _render_path_control_rows(rows)
    if path_control:
        lines.append("")
        lines.extend(path_control)
    lines.append("")
    lines.extend(_aggregate_legend())
    return "\n".join(lines) + "\n"


def _render_dependency_graph(rows: list[dict[str, object]]) -> list[str]:
    """Render service, remote-status, and path relationships as an operator tree."""
    if not rows:
        return ["dependency graph", "(no services)"]
    children: dict[str, list[dict[str, object]]] = {}
    roots: list[dict[str, object]] = []
    by_service: dict[str, dict[str, object]] = {}
    for row in rows:
        service = str(row.get("service") or "")
        if service:
            by_service[service] = row
    for row in rows:
        parent = str(row.get("parent") or "")
        if parent and parent in by_service:
            children.setdefault(parent, []).append(row)
        else:
            roots.append(row)

    lines = ["dependency graph"]
    seen: set[str] = set()
    for row in roots:
        lines.extend(_render_graph_branch(row, children, prefix="", branch="", seen=seen))
    return lines


def _render_graph_branch(
    row: dict[str, object],
    children: dict[str, list[dict[str, object]]],
    *,
    prefix: str,
    branch: str,
    seen: set[str],
) -> list[str]:
    service = str(row.get("service") or "")
    row_key = str(row.get("row_key") or service)
    if row_key in seen:
        return [f"{prefix}{service} [cycle]"]
    seen.add(row_key)
    state = str(row.get("state") or "-")
    context = _truncate(str(row.get("extra") or ""), DETAIL_CTRL_WIDTH)
    label = f"{service} [{state}]"
    if context:
        label += f" {context}"
    lines = [f"{prefix}{branch}{label}"]
    child_rows = children.get(service, [])
    for index, child in enumerate(child_rows):
        is_last = index == len(child_rows) - 1
        lines.extend(
            _render_graph_branch(
                child,
                children,
                prefix=f"{prefix}{'   ' if branch == '`- ' else '|  ' if branch == '|- ' else ''}",
                branch="`- " if is_last else "|- ",
                seen=seen,
            )
        )
    return lines


def _render_service_control_rows(rows: list[dict[str, object]]) -> list[str]:
    service_rows = [row for row in rows if row.get("row_type") == "service"]
    if not service_rows:
        return []
    headers = [
        "service",
        "sys",
        "gl",
        "ntp",
        "lsch",
        "psch",
        "ctx",
        "crx",
        "clock",
        "paths",
        "svc",
        "pol",
        "err",
        "off",
        "lat",
        "last",
    ]
    rendered_rows = [_service_control_cells(row) for row in service_rows]
    return _render_named_table("service time/control", headers, rendered_rows)


def _service_control_cells(row: dict[str, object]) -> list[str]:
    metadata = row.get("control_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return [
        str(row["service"]),
        str(row["system_time"]),
        str(row["gatherlink_time"]),
        str(row["ntp"]),
        _scheduler_mode_context(metadata.get("local_scheduler")),
        _scheduler_mode_context(metadata.get("peer_scheduler")),
        _control_counter_summary(metadata.get("sent")),
        _control_counter_summary(metadata.get("received")),
        _truncate(_internal_clock_context(metadata.get("internal_clock")) or "-", 36),
        str(metadata.get("path_metadata_count", 0) or 0),
        str(metadata.get("service_metadata_count", 0) or 0),
        str(metadata.get("service_scheduler_policy_count", 0) or 0),
        str(metadata.get("service_endpoint_mismatch_count", 0) or 0),
        str(metadata.get("service_disable_count", 0) or 0),
        str(metadata.get("path_latency_count", 0) or 0),
        _control_last_time(metadata),
    ]


def _render_path_control_rows(rows: list[dict[str, object]]) -> list[str]:
    path_rows = [row for row in rows if row.get("row_type") == "path" and row.get("control_metadata")]
    if not path_rows:
        return []
    headers = ["service", "path", "ctx", "crx", "cap", "mtu", "lat"]
    rendered_rows = [_path_control_cells(row) for row in path_rows]
    return _render_named_table("path control", headers, rendered_rows)


def _render_service_policy_rows(rows: list[dict[str, object]]) -> list[str]:
    """Render Python-owned service meaning used by scheduler policy."""
    rendered_rows: list[list[str]] = []
    for row in rows:
        if row.get("row_type") != "service":
            continue
        service_config = row.get("service_config")
        if not isinstance(service_config, list):
            continue
        for item in service_config:
            if not isinstance(item, dict):
                continue
            rendered_rows.append(
                [
                    str(row["service"]),
                    str(item.get("name") or "-"),
                    str(item.get("traffic_class") or "unknown"),
                    str(item.get("priority") or "normal"),
                    _truncate(str(item.get("listen") or "-"), 28),
                    _truncate(str(item.get("target") or "-"), 28),
                ]
            )
    if not rendered_rows:
        return []
    return _render_named_table(
        "service policy",
        ["runner", "service", "class", "prio", "listen", "target"],
        rendered_rows,
    )


def _render_service_budget_rows(rows: list[dict[str, object]]) -> list[str]:
    """Render Python-owned service budget/QoS decisions."""
    rendered_rows: list[list[str]] = []
    for row in rows:
        if row.get("row_type") != "service":
            continue
        budget = row.get("service_budget")
        if not isinstance(budget, dict) or not budget:
            continue
        packet_overrides = budget.get("packet_budget_overrides")
        byte_overrides = budget.get("byte_budget_overrides")
        samples = budget.get("samples")
        rendered_rows.append(
            [
                str(row["service"]),
                "yes" if budget.get("active") else "no",
                _truncate(_service_budget_overrides_context(packet_overrides), 30),
                _truncate(_service_budget_overrides_context(byte_overrides), 30),
                _truncate(_service_budget_samples_context(samples), 40),
                _truncate(str(budget.get("reason") or "-"), 56),
            ]
        )
    if not rendered_rows:
        return []
    return _render_named_table(
        "service budget",
        ["runner", "active", "pkt", "bytes", "samples", "reason"],
        rendered_rows,
    )


def _service_budget_overrides_context(value: object) -> str:
    """Return compact service-budget override text."""
    if not isinstance(value, dict) or not value:
        return "-"
    return ",".join(f"{key}={value[key]}" for key in sorted(value))


def _service_budget_samples_context(value: object) -> str:
    """Return compact service sample-rate text for monitor tables."""
    if not isinstance(value, list) or not value:
        return "-"
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        service = str(item.get("service") or "?")
        bytes_per_second = _float_or_zero(item.get("tx_bytes_per_second"))
        packets_per_second = _float_or_zero(item.get("tx_packets_per_second"))
        parts.append(f"{service}:{bytes_per_second:.0f}B/s/{packets_per_second:.0f}pps")
    return ",".join(parts) if parts else "-"


def _float_or_zero(value: object) -> float:
    """Parse monitor-only floating counters without trusting runtime status."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _render_auth_crypto_rows(rows: list[dict[str, object]]) -> list[str]:
    """Render operator-safe reserved auth/crypto facts."""
    rendered_rows: list[list[str]] = []
    for row in rows:
        if row.get("row_type") != "service":
            continue
        messages = row.get("auth_crypto_messages")
        if not isinstance(messages, list):
            continue
        for item in messages[-4:]:
            if not isinstance(item, dict):
                continue
            rendered_rows.append(
                [
                    str(row["service"]),
                    _truncate(str(item.get("type") or "-"), 18),
                    _truncate(str(item.get("peer") or item.get("sender_node_id") or "-"), 24),
                    str(item.get("topology_generation") or "-"),
                    str(item.get("current_receiver_index") or "-"),
                    "yes" if item.get("has_noise") else "no",
                    str(item.get("path_id") or "-"),
                    str(item.get("sequence") or "-"),
                    _truncate(str(item.get("reason") or "-"), 40),
                ]
            )
    if not rendered_rows:
        return []
    return _render_named_table(
        "auth/rekey control",
        ["runner", "type", "peer", "gen", "recv", "noise", "path", "seq", "reason"],
        rendered_rows,
    )


def _path_control_cells(row: dict[str, object]) -> list[str]:
    metadata = row.get("control_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    path_name = str(row.get("path") or str(row["service"]).strip().removeprefix("path "))
    tx, rx = _path_control_counters(metadata, path_name)
    return [
        str(row.get("parent") or ""),
        path_name,
        tx,
        rx,
        _path_capacity_only_context(metadata, path_name),
        _path_mtu_context(metadata, path_name),
        _path_latency_context(metadata, path_name),
    ]


def _scheduler_mode_context(value: object) -> str:
    """Return configured/effective TX scheduler context for monitor tables."""
    if not isinstance(value, dict):
        return "-"
    configured = str(value.get("configured_mode") or "-")
    effective = str(value.get("effective_mode") or configured)
    rust_mode = str(value.get("rust_mode") or "-")
    if configured == effective:
        return _truncate(configured, 22)
    return _truncate(f"{configured}->{effective}/{rust_mode}", 22)


def _render_named_table(title: str, headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    lines = [title]
    lines.append(" ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    lines.append(" ".join("-" * width for width in widths))
    lines.extend(" ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rows)
    return lines


def _compact_counter(value: object) -> str:
    return "-" if value == "not_reported" else str(value)


def _directional_counters(status: dict[str, object]) -> tuple[int, int, int, int]:
    """Return local-view TX/RX counters from either explicit or legacy status fields."""
    tx_packets = int(status.get("tx_packets", status.get("packets", 0)) or 0)
    tx_bytes = int(status.get("tx_bytes", status.get("bytes", 0)) or 0)
    rx_packets = int(status.get("rx_packets", status.get("reply_packets", 0)) or 0)
    rx_bytes = int(status.get("rx_bytes", status.get("reply_bytes", 0)) or 0)
    if "listen" in status and "tx_bytes" not in status and "rx_bytes" not in status:
        # Legacy sink statuses reported received traffic as `bytes`. New lab
        # workers publish explicit directions, but this fallback keeps older
        # running services readable while they are being restarted.
        tx_packets = int(status.get("reply_packets", 0) or 0)
        tx_bytes = int(status.get("reply_bytes", 0) or 0)
        rx_packets = int(status.get("packets", 0) or 0)
        rx_bytes = int(status.get("bytes", 0) or 0)
    return tx_packets, tx_bytes, rx_packets, rx_bytes


def _format_bytes(value: int, *, human_units: bool, decimal_units: bool) -> str:
    if not human_units:
        return str(value)
    return _format_human_quantity(value, units=_data_units(decimal_units), base=_unit_base(decimal_units))


def _format_rate(value: object, *, human_units: bool, speed_bits: bool, decimal_units: bool) -> str:
    if value == "not_reported":
        return "-"
    numeric = int(value)
    if not speed_bits:
        if not human_units:
            return str(numeric)
        return f"{_format_human_quantity(numeric, units=_data_units(decimal_units), base=_unit_base(decimal_units))}/s"
    if not human_units:
        return str(numeric * 8)
    return f"{_format_human_quantity(numeric * 8, units=_bit_units(decimal_units), base=_unit_base(decimal_units))}/s"


def _format_human_quantity(value: int, *, units: list[str], base: int) -> str:
    amount = float(value)
    unit = units[0]
    for unit in units:
        if abs(amount) < base or unit == units[-1]:
            break
        amount /= base
    if unit in {"B", "bit"}:
        return f"{int(amount)}{unit}"
    return f"{amount:.1f}{unit}"


def _unit_base(decimal_units: bool) -> int:
    return 1000 if decimal_units else 1024


def _data_units(decimal_units: bool) -> list[str]:
    return ["B", "KB", "MB", "GB", "TB"] if decimal_units else ["B", "KiB", "MiB", "GiB", "TiB"]


def _bit_units(decimal_units: bool) -> list[str]:
    return ["bit", "Kbit", "Mbit", "Gbit", "Tbit"] if decimal_units else ["bit", "Kibit", "Mibit", "Gibit", "Tibit"]


def _queue_pressure_context(status: dict[str, object]) -> str:
    """Return compact queue pressure from status facts, or not_reported."""
    packets = status.get("queue_depth_packets")
    bytes_ = status.get("queue_depth_bytes")
    age_us = status.get("queue_oldest_age_us")
    if packets is None and bytes_ is None and age_us is None:
        return "not_reported"
    pieces = []
    if packets is not None:
        pieces.append(f"p={int(packets)}")
    if bytes_ is not None:
        pieces.append(f"b={_format_bytes(int(bytes_), human_units=True, decimal_units=False)}")
    if age_us is not None:
        pieces.append(f"a={_format_latency_us(int(age_us))}")
    return "/".join(pieces) if pieces else "not_reported"


def _scheduler_health_context(status: dict[str, object]) -> str:
    """Return compact Python scheduler health for monitor rows when facts exist."""
    if not _has_scheduler_status_facts(status):
        return "not_reported"
    packets = _status_int(status.get("packets"))
    missed = _status_int(status.get("missed_packets")) + _status_int(status.get("qdisc_dropped_packets"))
    denominator = packets + missed
    loss_ppm = _status_int(status.get("loss_ppm"))
    if denominator > 0 and "loss_ppm" not in status:
        loss_ppm = min(1_000_000, missed * 1_000_000 // denominator)
    metrics = PathSchedulerMetrics(
        path_name=str(status.get("path") or "path"),
        path_id=0,
        tx_capacity_bps=_optional_status_int(status.get("tx_capacity_bps")),
        rx_capacity_bps=_optional_status_int(status.get("rx_capacity_bps")),
        tx_latency_current_us=_optional_status_int(status.get("tx_latency_current_us")),
        tx_latency_mean_us=_optional_status_int(status.get("tx_latency_mean_us")),
        rx_latency_current_us=_optional_status_int(status.get("rx_latency_current_us")),
        rx_latency_mean_us=_optional_status_int(status.get("rx_latency_mean_us")),
        loss_ppm=min(1_000_000, max(0, loss_ppm)),
        queue_depth_packets=_status_int(status.get("queue_depth_packets")),
        queue_depth_bytes=_status_int(status.get("queue_depth_bytes")),
        queue_oldest_age_us=_status_int(status.get("queue_oldest_age_us")),
        send_failures=_status_int(status.get("send_failed_packets")),
        receive_gaps=_status_int(status.get("packets_needing_reorder")),
        reorder_depth_packets=_status_int(status.get("reorder_depth_packets")),
        local_drops=_status_int(status.get("qdisc_dropped_packets")) + _status_int(status.get("security_drop_packets")),
        scheduler_in_flight_packets=_status_int(status.get("scheduler_in_flight_packets")),
        scheduler_in_flight_bytes=_status_int(status.get("scheduler_in_flight_bytes")),
        scheduler_predicted_delivery_us=_status_int(status.get("scheduler_predicted_delivery_us")),
        reorder_buffer_packets=_status_int(status.get("reorder_buffer_packets")),
        reorder_buffer_oldest_age_us=_status_int(status.get("reorder_buffer_oldest_age_us")),
        socket_receive_buffer_bytes=_status_int(status.get("socket_receive_buffer_bytes")),
        socket_send_buffer_bytes=_status_int(status.get("socket_send_buffer_bytes")),
        socket_drain_quantum=_status_int(status.get("socket_drain_quantum")),
        stale_control_age_us=_status_int(status.get("stale_control_age_us")),
    )
    score = score_path(metrics)
    label = {"alive": "ok", "degraded": "deg", "down": "down"}[score.health]
    reasons = [reason for reason in score.reasons if reason != "healthy"]
    suffix = f":{','.join(reasons[:2])}" if reasons else ""
    return f"{label}/s{score.score}/w{score.weight}{suffix}"


def _has_scheduler_status_facts(status: dict[str, object]) -> bool:
    """Return whether a status row carries enough facts to score without guesswork."""
    return any(
        key in status
        for key in [
            "tx_capacity_bps",
            "rx_capacity_bps",
            "tx_latency_current_us",
            "tx_latency_mean_us",
            "rx_latency_current_us",
            "rx_latency_mean_us",
            "loss_ppm",
            "queue_depth_packets",
            "queue_depth_bytes",
            "queue_oldest_age_us",
            "send_failed_packets",
            "packets_needing_reorder",
            "reorder_depth_packets",
            "qdisc_dropped_packets",
            "security_drop_packets",
            "stale_control_age_us",
            "scheduler_in_flight_packets",
            "scheduler_in_flight_bytes",
            "scheduler_predicted_delivery_us",
            "reorder_buffer_packets",
            "reorder_buffer_oldest_age_us",
            "socket_receive_buffer_bytes",
            "socket_send_buffer_bytes",
            "socket_drain_quantum",
        ]
    )


def _status_int(value: object) -> int:
    """Convert monitor status facts to a non-negative integer."""
    converted = _optional_status_int(value)
    return converted if converted is not None else 0


def _optional_status_int(value: object) -> int | None:
    """Convert monitor status facts to an optional non-negative integer."""
    if value is None or value == "not_reported":
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, converted)


def _status_context(status: dict[str, object]) -> str:
    if status.get("kind") == "relay":
        context = f"relay listen={status.get('listen', '-')}"
        if status.get("next_hop"):
            context += f" next={status['next_hop']}"
        if status.get("direction"):
            context += f" dir={status['direction']}"
        if status.get("exit_to_inner_packet"):
            context += " exit=inner"
        return context
    if status.get("target"):
        return f"target={status['target']}"
    if status.get("listen"):
        context = f"listen={status['listen']}"
        if status.get("last_payload_bytes") is not None:
            context += f" last={status['last_payload_bytes']}B"
        if status.get("last_source"):
            context += f" source={status['last_source']}"
        return context
    if status.get("last_payload_bytes") is not None:
        return f"last={status['last_payload_bytes']}B"
    if status.get("last_payload"):
        return "last_payload=present"
    return ""


def _control_last_time(control_metadata: dict[str, object]) -> str:
    sent = control_metadata.get("sent")
    received = control_metadata.get("received")
    last_at = received.get("last_at") if isinstance(received, dict) else None
    if not last_at and isinstance(sent, dict):
        last_at = sent.get("last_at")
    return _short_time(last_at) if isinstance(last_at, str) and last_at else "-"


def _system_time_context(control_metadata: object) -> str:
    sink_time = _sink_time_dict(control_metadata)
    value = sink_time.get("system_unix_us") if sink_time else None
    return _format_unix_us(int(value)) if value is not None else _short_wall_now()


def _gatherlink_time_context(control_metadata: object) -> str:
    sink_time = _sink_time_dict(control_metadata)
    if not sink_time:
        return "-"
    value = sink_time.get("gatherlink_unix_us")
    sent = sink_time.get("sink_sent_unix_us")
    received = sink_time.get("received_at")
    if value is None:
        return "-"
    parts = [_format_unix_us(int(value))]
    if sent is not None:
        parts.append(f"sent={_format_time_only_unix_us(int(sent))}")
    if isinstance(received, str) and received:
        parts.append(f"rx={_short_time(received)}")
    return " ".join(parts)


def _ntp_context(control_metadata: object) -> str:
    sink_time = _sink_time_dict(control_metadata)
    if not sink_time:
        return "-"
    state = sink_time.get("ntp_state") or "unknown"
    source = sink_time.get("ntp_source")
    source_type = sink_time.get("ntp_source_type")
    if isinstance(source, str) and source:
        if isinstance(source_type, str) and source_type and source_type != "ntp":
            return f"{state}/{source_type}:{source}"
        return f"{state}/{source}"
    return str(state)


def _sink_time_dict(control_metadata: object) -> dict[str, object] | None:
    if not isinstance(control_metadata, dict):
        return None
    sink_time = control_metadata.get("sink_time")
    return sink_time if isinstance(sink_time, dict) else None


def _path_capacity_summary(path_capacity: object) -> str:
    if not isinstance(path_capacity, dict) or not path_capacity:
        return "0"
    samples = []
    for path_name, capacity in list(path_capacity.items())[:2]:
        if not isinstance(capacity, dict):
            continue
        tx_bps = capacity.get("tx_bps")
        rx_bps = capacity.get("rx_bps")
        pieces = []
        if tx_bps is not None:
            pieces.append(f"tx{_format_compact_bps(int(tx_bps))}")
        if rx_bps is not None:
            pieces.append(f"rx{_format_compact_bps(int(rx_bps))}")
        if pieces:
            samples.append(f"{path_name}:{'/'.join(pieces)}")
    remaining = len(path_capacity) - len(samples)
    suffix = f"+{remaining}" if remaining > 0 else ""
    return ",".join(samples) + suffix if samples else str(len(path_capacity))


def _internal_clock_context(internal_clock: object) -> str:
    if not isinstance(internal_clock, dict):
        return ""
    role = internal_clock.get("role")
    offset_us = internal_clock.get("offset_us")
    mean_offset_us = internal_clock.get("mean_offset_us")
    rtt_us = internal_clock.get("rtt_us")
    error_budget_us = internal_clock.get("error_budget_us")
    confidence = internal_clock.get("confidence")
    samples = internal_clock.get("samples")
    if role == "sink-authoritative":
        return "clk=sink"
    if offset_us is None and rtt_us is None:
        return ""
    offset = _format_signed_latency_pair(
        int(offset_us) if offset_us is not None else None,
        int(mean_offset_us) if mean_offset_us is not None else None,
    )
    rtt = _format_latency_us(int(rtt_us)) if rtt_us is not None else "-"
    sample_text = f" n={samples}" if samples else ""
    error_text = f" err={_format_latency_us(int(error_budget_us))}" if error_budget_us is not None else ""
    confidence_text = f" {confidence}" if confidence else ""
    return f"clk=off{offset} rtt={rtt}{error_text}{sample_text}{confidence_text}"


def _path_control_context(control_metadata: object, path_name: str) -> str:
    if not isinstance(control_metadata, dict):
        return "not_reported"
    pieces = []
    path_control = control_metadata.get("path_control")
    if isinstance(path_control, dict):
        control = path_control.get(path_name)
        if isinstance(control, dict):
            tx = _control_counter_summary(control.get("tx"))
            rx = _control_counter_summary(control.get("rx"))
            if tx != "-":
                pieces.append(f"ctx={tx}")
            if rx != "-":
                pieces.append(f"crx={rx}")
    capacity = _path_capacity_context(control_metadata, path_name)
    if capacity != "not_reported":
        pieces.append(capacity)
    return " ".join(pieces) if pieces else "not_reported"


def _path_control_counters(control_metadata: dict[str, object], path_name: str) -> tuple[str, str]:
    path_control = control_metadata.get("path_control")
    if not isinstance(path_control, dict):
        return "-", "-"
    control = path_control.get(path_name)
    if not isinstance(control, dict):
        return "-", "-"
    return _control_counter_summary(control.get("tx")), _control_counter_summary(control.get("rx"))


def _control_counter_summary(counter: object) -> str:
    if not isinstance(counter, dict) or int(counter.get("frames", 0) or 0) == 0:
        return "-"
    frames = int(counter.get("frames", 0) or 0)
    bytes_seen = int(counter.get("bytes", 0) or 0)
    gap_us = int(counter.get("last_gap_us", 0) or 0)
    gap = f"/g={_format_latency_us(gap_us)}" if gap_us > 0 else ""
    return f"{frames}/{_format_bytes(bytes_seen, human_units=True, decimal_units=False)}{gap}"


def _path_capacity_context(control_metadata: object, path_name: str) -> str:
    if not isinstance(control_metadata, dict):
        return "not_reported"
    path_capacity = control_metadata.get("path_capacity")
    if not isinstance(path_capacity, dict):
        return "not_reported"
    capacity = path_capacity.get(path_name)
    if not isinstance(capacity, dict):
        return "not_reported"
    tx_bps = capacity.get("tx_bps")
    rx_bps = capacity.get("rx_bps")
    tx = _format_compact_bps(int(tx_bps)) if tx_bps is not None else "-"
    rx = _format_compact_bps(int(rx_bps)) if rx_bps is not None else "-"
    latency = _path_latency_for(control_metadata, path_name)
    if latency is None:
        return f"tx={tx} rx={rx}"
    return f"tx={tx} rx={rx} tl={_latency_pair(latency, 'tx')} rl={_latency_pair(latency, 'rx')}"


def _path_capacity_only_context(control_metadata: dict[str, object], path_name: str) -> str:
    path_capacity = control_metadata.get("path_capacity")
    if not isinstance(path_capacity, dict):
        return "-"
    capacity = path_capacity.get(path_name)
    if not isinstance(capacity, dict):
        return "-"
    tx_bps = capacity.get("tx_bps")
    rx_bps = capacity.get("rx_bps")
    tx = _format_compact_bps(int(tx_bps)) if tx_bps is not None else "-"
    rx = _format_compact_bps(int(rx_bps)) if rx_bps is not None else "-"
    return f"tx={tx} rx={rx}"


def _path_mtu_context(control_metadata: dict[str, object], path_name: str) -> str:
    path_mtu = control_metadata.get("path_mtu")
    if not isinstance(path_mtu, dict):
        return "-"
    mtu = path_mtu.get(path_name)
    if not isinstance(mtu, dict):
        return "-"
    tx = _mtu_direction_context(mtu, "tx")
    rx = _mtu_direction_context(mtu, "rx")
    status = mtu.get("status")
    parts = []
    if tx:
        parts.append(f"tx={tx}")
    if rx:
        parts.append(f"rx={rx}")
    if status and status != "ok":
        parts.append(str(status))
    return " ".join(parts) if parts else "-"


def _mtu_direction_context(mtu: dict[str, object], direction: str) -> str:
    frame_mtu = mtu.get(f"{direction}_frame_mtu")
    payload_mtu = mtu.get(f"{direction}_payload_mtu")
    link_mtu = mtu.get(f"{direction}_link_mtu")
    if direction == "tx":
        frame_mtu = frame_mtu if frame_mtu is not None else mtu.get("frame_mtu")
        payload_mtu = payload_mtu if payload_mtu is not None else mtu.get("payload_mtu")
        link_mtu = link_mtu if link_mtu is not None else mtu.get("link_mtu")
    if frame_mtu is None and payload_mtu is None and link_mtu is None:
        return ""
    frame_text = str(frame_mtu) if frame_mtu is not None else "-"
    payload_text = str(payload_mtu) if payload_mtu is not None else "-"
    link_text = str(link_mtu) if link_mtu is not None else "-"
    return f"frm:{frame_text}/pay:{payload_text}/lnk:{link_text}"


def _path_latency_context(control_metadata: dict[str, object], path_name: str) -> str:
    latency = _path_latency_for(control_metadata, path_name)
    if latency is None:
        return "-"
    source = str(latency.get("source") or "unknown")
    if source == "rejected":
        reason = str(latency.get("rejection_reason") or "sample")
        return f"src=reject reason={reason} tl={_latency_pair(latency, 'tx')} rl={_latency_pair(latency, 'rx')}"
    confidence = str(latency.get("confidence") or "-")
    return (
        f"src={_short_latency_source(source)} conf={confidence} "
        f"tl={_latency_pair(latency, 'tx')} rl={_latency_pair(latency, 'rx')}"
    )


def _short_latency_source(source: str) -> str:
    if source == "clock-synced-one-way":
        return "clock"
    if source == "data-traffic-one-way":
        return "data"
    if source == "reply-rtt-half":
        return "rtt/2"
    if source == "peer":
        return "peer"
    return source


def _path_latency_for(control_metadata: dict[str, object], path_name: str) -> dict[str, object] | None:
    path_latency = control_metadata.get("path_latency")
    if not isinstance(path_latency, dict):
        return None
    latency = path_latency.get(path_name)
    return latency if isinstance(latency, dict) else None


def _latency_pair(latency: dict[str, object], direction: str) -> str:
    current = latency.get(f"{direction}_current_us")
    mean = latency.get(f"{direction}_mean_us")
    return _format_latency_pair(int(current) if current is not None else None, int(mean) if mean is not None else None)


def _format_latency_pair(current_us: int | None, mean_us: int | None) -> str:
    if current_us is None and mean_us is None:
        return "-/-"
    if current_us is None:
        return f"-/{_format_latency_us(mean_us)}"
    if mean_us is None:
        return f"{_format_latency_us(current_us)}/-"
    unit = _shared_latency_unit(current_us, mean_us)
    return f"{_format_latency_value(current_us, unit)}/{_format_latency_value(mean_us, unit)}{unit}"


def _format_signed_latency_pair(current_us: int | None, mean_us: int | None) -> str:
    if current_us is None and mean_us is None:
        return "-"
    if mean_us is None:
        return _format_signed_latency_us(current_us or 0)
    if current_us is None:
        return f"-/{_format_signed_latency_us(mean_us)}"
    unit = _shared_latency_unit(current_us, mean_us)
    return f"{_format_signed_latency_value(current_us, unit)}/{_format_signed_latency_value(mean_us, unit)}{unit}"


def _format_latency_us(value: int) -> str:
    if value < 1000:
        return f"{value}us"
    if value < 1_000_000:
        return f"{value / 1000:.1f}ms"
    return f"{value / 1_000_000:.2f}s"


def _format_signed_latency_us(value: int) -> str:
    sign = "+" if value >= 0 else "-"
    return sign + _format_latency_us(abs(value))


def _shared_latency_unit(left_us: int, right_us: int) -> str:
    largest = max(abs(left_us), abs(right_us))
    if largest < 1000:
        return "us"
    if largest < 1_000_000:
        return "ms"
    return "s"


def _format_latency_value(value_us: int, unit: str) -> str:
    if unit == "us":
        return str(value_us)
    if unit == "ms":
        return f"{value_us / 1000:.1f}"
    return f"{value_us / 1_000_000:.2f}"


def _format_signed_latency_value(value_us: int, unit: str) -> str:
    sign = "+" if value_us >= 0 else "-"
    return sign + _format_latency_value(abs(value_us), unit)


def _format_compact_bps(value: int) -> str:
    amount = float(value)
    unit = "b"
    for unit in ["b", "Kb", "Mb", "Gb", "Tb"]:
        if abs(amount) < 1000 or unit == "Tb":
            break
        amount /= 1000
    if unit == "b":
        return f"{int(amount)}{unit}"
    return f"{amount:.1f}{unit}"


def _short_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%H:%M:%S")


def _short_wall_now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _format_unix_us(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000).strftime("%H:%M:%S.%f")[:-3]


def _format_time_only_unix_us(value: int) -> str:
    return _format_unix_us(value)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


def _aggregate_legend() -> list[str]:
    return [
        "legend: - means the running service/dataplane has not reported that counter yet",
        "  service  registered service name; indented path rows are per transport path",
        "  state    service lifecycle reported by IPC, systemd, ipc_error, or path for path rows",
        "  txp/rxp  packets transmitted/received from this service or path's local point of view",
        "  txB/rxB  payload bytes transmitted/received; press m to toggle binary/decimal human units",
        "  tx/s rx/s sampled rates; b toggles bit/s vs byte/s, m toggles Kibit/Mibit vs Kbit/Mbit",
        "  graph    press g in interactive monitor mode to toggle between table and dependency graph views",
        "  miss     missed packets; lab path rows include qdisc drops and receiver missing-sequence facts",
        "  xdup     expected fanout duplicates suppressed before application UDP emit",
        "  dup      unexpected duplicate user-service frames suppressed before application UDP emit",
        "  fail     path send failures observed by the Rust UDP transport",
        "  ffail    fanout-copy send failures; useful when an expected duplicate copy cannot be sent",
        "  queue    scheduler-visible queue pressure as packets, bytes, and oldest queued age when known",
        "  sch      Python scheduler health summary: ok/deg/down, score, weight, and leading reasons",
        "  ooo      out-of-order packets observed by the receiver",
        "  reord    packets that required reorder buffering",
        "  context  service-specific context such as target, listen, latest payload, or systemd unit",
        "  sys      local wall time reported by that service process",
        "  gl       Gatherlink time derived from latest sink-authoritative control metadata",
        "  ntp      sink-side NTP state, with source shown as state/source when direct NTP is active",
        "  lsch     local TX scheduler status as configured/effective/Rust mode when they differ",
        "  psch     peer TX scheduler status learned through control metadata; diagnostic only",
        "  ctx/crx  control metaband frames/bytes transmitted/received; aggregate in service table, per path below",
        "  clock    internal monotonic clock role, offset, mean offset, RTT, and sample count",
        "  paths    number of path-id/name mappings learned through control metadata",
        "  svc      number of service-id/name mappings learned through control metadata; endpoints stay in config",
        "  pol      number of peer service scheduler policies learned for Python-owned receive expectations",
        "  class    Python-owned service traffic class used to compile scheduler policy; Rust receives only primitives",
        "  budget  Python-owned service budget/QoS status; Rust receives only drain quantum and byte caps",
        "  auth     operator-safe reserved auth/crypto facts; Noise payload bytes and traffic keys are never shown",
        "  err      endpoint assertion mismatches that stopped traffic for a service",
        "  off      peer service-disable assertions currently advertised through control metadata",
        "  svc_off  compact context summary of peer-disabled services",
        "  cap      directional path capacity metadata; tx/rx are from the row's local point of view",
        "  mtu      directional path MTU; tx/rx each show frame, max payload, and link MTU from local view",
        "  lat      directional latency metadata; current/mean pairs use tx and rx local point of view",
        "  last     latest control send/receive activity time for that service",
    ]


class _KeyReader:
    """Read single-key monitor commands when stdin is an interactive terminal."""

    def __init__(self) -> None:
        self.enabled = False
        self._previous_settings: list[int | bytes] | None = None

    def __enter__(self) -> _KeyReader:
        if sys.stdin.isatty():
            with suppress(termios.error):
                self._previous_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
                self.enabled = True
        return self

    def __exit__(self, *_args: object) -> None:
        if self.enabled and self._previous_settings is not None:
            with suppress(termios.error):
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._previous_settings)

    def read(self) -> str | None:
        if not self.enabled:
            return None
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if not readable:
            return None
        return sys.stdin.read(1).lower()


def _print_systemd_log(unit: str | None, *, follow: bool, tail: int) -> None:
    if not unit:
        typer.echo("systemd service has no unit name recorded", err=True)
        raise typer.Exit(1)
    try:
        default_debian_backend().run_journalctl(unit, follow=follow, tail=tail)
    except FileNotFoundError as exc:
        typer.echo("journalctl is not available on this host", err=True)
        raise typer.Exit(1) from exc


def _systemd_record_from_config(path: Path, *, unit: str | None) -> ServiceRecord:
    lab_facts = _lab_registration_facts(path)
    if lab_facts is not None:
        unit_name = unit or f"gatherlink-lab@{lab_facts['name']}.service"
        return ServiceRecord(
            name=service_name("lab", lab_facts["name"]),
            kind="lab",
            manager="systemd",
            systemd_unit=unit_name,
            detached_from_console=False,
            log_file=Path(f"journal:{unit_name}"),
            metadata={
                "config": str(path),
                "runtime_dir": lab_facts["runtime_dir"],
                "scenario": lab_facts["scenario"],
                "security_mode": lab_facts["security_mode"],
            },
        )

    try:
        config = validate_config_file(path)
    except ConfigValidationError as exc:
        raise ValueError(f"could not load Gatherlink config for systemd registration: {exc.message}") from exc

    unit_name = unit or f"gatherlink@{config.node}.service"
    return ServiceRecord(
        name=service_name("core", config.node),
        kind="core",
        manager="systemd",
        systemd_unit=unit_name,
        detached_from_console=False,
        log_file=Path(f"journal:{unit_name}"),
        metadata={
            "config": str(path),
            "node": config.node,
            "role": config.role,
            "security_mode": config.security.mode,
        },
    )


def _lab_registration_facts(path: Path) -> dict[str, str] | None:
    """
    Return the small lab facts needed for service registration.

    The generic services CLI should not import lab scenario models or planning
    behavior. It only needs enough structured information to mark a systemd
    service as lab-owned in the registry.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or "scenario" not in raw:
        return None
    name = raw.get("name")
    scenario = raw.get("scenario")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(scenario, str) or not scenario:
        return None
    security = raw.get("security")
    security_mode = "none"
    if isinstance(security, dict) and isinstance(security.get("mode"), str):
        security_mode = security["mode"]
    runtime_dir = raw.get("runtime_dir")
    return {
        "name": name,
        "runtime_dir": runtime_dir if isinstance(runtime_dir, str) else ".lab",
        "scenario": scenario,
        "security_mode": security_mode,
    }
