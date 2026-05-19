"""Service registry and log attachment CLI commands."""

from __future__ import annotations

import json
import select
import subprocess
import sys
import termios
import time
import tty
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

import typer
from pydantic import ValidationError

from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.validation import validate_config_file
from gatherlink.lab.scenarios import load_lab_scenario_file
from gatherlink.runtime.services import (
    ServiceIpcError,
    ServiceRecord,
    ServiceRegistry,
    iter_log_lines,
    request_service,
    service_name,
)

app = typer.Typer(help="List managed services and attach to their logs.")
AttachMode = Literal["raw", "aggregate"]
CONTEXT_WIDTH = 48


@app.command("list")
def list_services() -> None:
    """List services known to the local Gatherlink registry."""
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


@app.command("attach")
def attach(
    name: str,
    tail: int = typer.Option(80, help="Number of existing lines to show before following."),
    mode: AttachMode = typer.Option("raw", help="Attach mode: raw packet logs or aggregate live counters."),
    interval: float = typer.Option(1.0, help="Seconds between aggregate refreshes."),
    once: bool = typer.Option(False, "--once", help="Render aggregate status once and exit."),
) -> None:
    """Attach to a service log and keep following it."""
    try:
        service = ServiceRegistry().resolve(name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if mode == "aggregate":
        _print_aggregate([service], interval=interval, once=once)
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
) -> None:
    """Monitor one or more services as continuously refreshed aggregate counters."""
    registry = ServiceRegistry()
    try:
        services = [registry.resolve(name) for name in names]
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _print_aggregate(services, interval=interval, once=once)


@app.command("close")
def close(name: str) -> None:
    """Stop a process-managed Gatherlink service."""
    try:
        service = ServiceRegistry().close(name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"closed {service.name}")


def _print_log(path: Path, *, follow: bool, tail: int) -> None:
    for line in iter_log_lines(path, follow=follow, tail=tail):
        typer.echo(line)


def _print_aggregate(services: list[ServiceRecord], *, interval: float, once: bool) -> None:
    previous: dict[str, tuple[float, int]] = {}
    human_units = True
    speed_bits = True
    decimal_units = False
    with _KeyReader() as keys:
        while True:
            rows = []
            now = time.monotonic()
            for service in services:
                service_rows = _aggregate_rows_for_service(service, previous=previous, now=now)
                rows.extend(service_rows)
                for row in service_rows:
                    previous[str(row["row_key"])] = (now, row["bytes"])

            output = _render_aggregate_rows(
                rows,
                refreshed_at=datetime.now().strftime("%H:%M:%S"),
                human_units=human_units,
                speed_bits=speed_bits,
                decimal_units=decimal_units,
                interactive=keys.enabled and not once,
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
                if key == "q":
                    typer.echo()
                    return
                time.sleep(0.05)


def _aggregate_rows_for_service(
    service: ServiceRecord,
    *,
    previous: dict[str, tuple[float, int]],
    now: float,
) -> list[dict[str, object]]:
    if service.manager == "systemd":
        return [
            {
                "row_key": service.name,
                "service": service.name,
                "state": "systemd",
                "packets": 0,
                "bytes": 0,
                "speed_bytes_per_second": "not_reported",
                "missed": "not_reported",
                "reordered": "not_reported",
                "reorder_needed": "not_reported",
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
                "packets": 0,
                "bytes": 0,
                "speed_bytes_per_second": "not_reported",
                "missed": "not_reported",
                "reordered": "not_reported",
                "reorder_needed": "not_reported",
                "extra": str(exc),
            }
        ]

    total_bytes = int(status.get("bytes", 0))
    service_row = _row_from_status(
        row_key=service.name,
        name=service.name,
        state="running" if status.get("running", service.is_running()) else "stopped",
        packets=int(status.get("packets", 0)),
        total_bytes=total_bytes,
        status=status,
        previous=previous.get(service.name),
        now=now,
        extra=_status_context(status),
    )
    rows = [service_row]
    path_stats = status.get("path_stats", {})
    if isinstance(path_stats, dict):
        for path_name, path_status in path_stats.items():
            if not isinstance(path_status, dict):
                continue
            path_key = f"{service.name}::{path_name}"
            rows.append(
                _row_from_status(
                    row_key=path_key,
                    name=f"  path:{path_name}",
                    state="path",
                    packets=int(path_status.get("packets", 0)),
                    total_bytes=int(path_status.get("bytes", 0)),
                    status=path_status,
                    previous=previous.get(path_key),
                    now=now,
                    extra=f"parent={service.name}",
                )
            )
    return rows


def _row_from_status(
    *,
    row_key: str,
    name: str,
    state: str,
    packets: int,
    total_bytes: int,
    status: dict[str, object],
    previous: tuple[float, int] | None,
    now: float,
    extra: str,
) -> dict[str, object]:
    if "current_speed_bps" in status:
        speed_bytes_per_second: int | str = int(status["current_speed_bps"])
    elif previous is None:
        speed_bytes_per_second = 0
    else:
        elapsed = max(now - previous[0], 0.001)
        speed_bytes_per_second = int((total_bytes - previous[1]) / elapsed)
    # TODO(rust-stats): Populate these from Rust dataplane status once the Rust side reports loss and reorder metrics.
    return {
        "row_key": row_key,
        "service": name,
        "state": state,
        "packets": packets,
        "bytes": total_bytes,
        "speed_bytes_per_second": speed_bytes_per_second,
        "missed": status.get("missed_packets", "not_reported"),
        "reordered": status.get("reordered_packets", "not_reported"),
        "reorder_needed": status.get("packets_needing_reorder", "not_reported"),
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
) -> str:
    headers = ["service", "state", "pkts", "bytes", "speed", "miss", "ooo", "reord", "context"]
    rendered_rows = [
        [
            str(row["service"]),
            str(row["state"]),
            str(row["packets"]),
            _format_bytes(int(row["bytes"]), human_units=human_units, decimal_units=decimal_units),
            _format_rate(
                row["speed_bytes_per_second"],
                human_units=human_units,
                speed_bits=speed_bits,
                decimal_units=decimal_units,
            ),
            _compact_counter(row["missed"]),
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
    keys = " | h units | b speed | m base | q quit" if interactive else ""
    title = f"Gatherlink service monitor | refreshed {refreshed_at} | {mode} | {unit_base} | speed {speed_mode}{keys}"
    lines = [title, "=" * max(table_width, len(title))]
    lines.append(" ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    lines.append(" ".join("-" * width for width in widths))
    lines.extend(" ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rendered_rows)
    lines.append("")
    lines.extend(_aggregate_legend())
    return "\n".join(lines) + "\n"


def _compact_counter(value: object) -> str:
    return "-" if value == "not_reported" else str(value)


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


def _status_context(status: dict[str, object]) -> str:
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


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


def _aggregate_legend() -> list[str]:
    return [
        "legend: - means the running service/dataplane has not reported that counter yet",
        "  service registered service name",
        "  state   service lifecycle reported by IPC, systemd, or ipc_error",
        "  pkts    packets observed since service start",
        "  bytes   payload data since service start; press m to toggle binary/decimal human units",
        "  speed   sampled rate; b toggles bit/s vs byte/s, m toggles Kibit/Mibit vs Kbit/Mbit",
        "  miss    missed packets",
        "  ooo     out-of-order packets",
        "  reord   packets that required reorder buffering",
        "  context service-specific context such as target, listen, latest payload, or systemd unit",
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
    command = ["journalctl", "-u", unit, "--no-pager", "-n", str(tail)]
    if follow:
        command.append("-f")
    try:
        subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        typer.echo("journalctl is not available on this host", err=True)
        raise typer.Exit(1) from exc


def _systemd_record_from_config(path: Path, *, unit: str | None) -> ServiceRecord:
    try:
        lab_config = load_lab_scenario_file(path)
    except (OSError, ValueError, ValidationError):
        lab_config = None
    if lab_config is not None:
        unit_name = unit or f"gatherlink-lab@{lab_config.name}.service"
        return ServiceRecord(
            name=service_name("lab", lab_config.name),
            kind="lab",
            manager="systemd",
            systemd_unit=unit_name,
            detached_from_console=False,
            log_file=Path(f"journal:{unit_name}"),
            metadata={
                "config": str(path),
                "runtime_dir": lab_config.runtime_dir,
                "scenario": lab_config.scenario,
                "security_mode": lab_config.security.mode,
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
