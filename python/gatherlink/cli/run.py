"""
Runtime CLI commands.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from threading import Event

import typer

from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.dataplane.rust_backend import RustDataplaneUnavailableError, RustRuntimeBridgeError
from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.diagnostics.sinks import JsonlDiagnosticSink
from gatherlink.runtime.runner import CoreRunnerState, run_core_service
from gatherlink.runtime.services import ServiceIpcServer, ServiceRecord, ServiceRegistry, service_name
from gatherlink.runtime.supervisor import plan_runtime_start

app = typer.Typer(help="Plan and run Gatherlink runtime services.")


def _render_error(exc: ConfigValidationError) -> None:
    """Render config errors for runtime commands without stack traces."""
    typer.echo(f"invalid: {exc.message}", err=True)
    for detail in exc.details:
        location = ".".join(detail.location) if detail.location else "config"
        typer.echo(f"  - {location}: {detail.message}", err=True)
    raise typer.Exit(1)


@app.command("plan")
def plan(path: Path) -> None:
    """Print the userland UDP startup plan for a config file."""
    try:
        runtime_config = expand_config(validate_config_file(path))
        runtime_plan = plan_runtime_start(runtime_config)
    except ConfigValidationError as exc:
        _render_error(exc)
    typer.echo(json.dumps(runtime_plan.export_dict(), indent=2, sort_keys=True))


@app.command("service")
def service(
    path: Path,
    max_iterations: int | None = typer.Option(
        None,
        help="Stop after this many runner loop iterations; intended for smoke tests.",
    ),
    batch_size: int = typer.Option(32, help="Maximum UDP datagrams to drain per service step."),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        help="Append structured diagnostics events to this JSONL file.",
    ),
    service_name_override: str | None = typer.Option(
        None,
        "--service-name",
        help="Register this foreground service under a specific process-managed service name.",
    ),
    service_log: Path | None = typer.Option(
        None,
        "--service-log",
        help="Log file path recorded in the service registry when --service-name is used.",
    ),
    scheduler_reapply_interval: float | None = typer.Option(
        None,
        "--scheduler-reapply-interval",
        help="Seconds between Python-owned status-to-scheduler hot reapply passes.",
    ),
) -> None:
    """Run a foreground Rust-backed core service from a config file."""
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    ipc: ServiceIpcServer | None = None
    runner_state: CoreRunnerState | None = None
    try:
        source_config = validate_config_file(path)
        runtime_config = expand_config(source_config)
        if service_name_override is not None:
            runner_state = CoreRunnerState(
                node=runtime_config.node,
                security_mode=runtime_config.security.mode,
                service_names=[service.name for service in runtime_config.services if service.listen],
                stop_event=Event(),
            )
            record = ServiceRegistry().register(
                ServiceRecord(
                    name=service_name_override,
                    kind="core",
                    pid=os.getpid(),
                    log_file=service_log or Path(".gatherlink/logs") / f"{service_name_override}.log",
                    detached_from_console=False,
                    command=sys.argv,
                    metadata={
                        "config": str(path),
                        "node": runtime_config.node,
                        "role": runtime_config.role,
                        "security_mode": runtime_config.security.mode,
                    },
                )
            )
            ipc = ServiceIpcServer(record, status=runner_state.snapshot, stop=runner_state.stop)
            ipc.start()
        result = run_core_service(
            runtime_config,
            max_iterations=max_iterations,
            batch_size=batch_size,
            diagnostics_bus=diagnostics_bus,
            stop_event=runner_state.stop_event if runner_state is not None else None,
            runner_state=runner_state,
            source_config=source_config,
            scheduler_reapply_interval_seconds=scheduler_reapply_interval,
        )
    except ConfigValidationError as exc:
        _publish_start_failed(
            diagnostics_bus,
            message=f"invalid config: {exc.message}",
            details={"path": str(path), "errors": [detail.export_dict() for detail in exc.details]},
        )
        _render_error(exc)
    except (RustDataplaneUnavailableError, RustRuntimeBridgeError, ValueError) as exc:
        _publish_start_failed(
            diagnostics_bus,
            message=f"cannot start core service: {exc}",
            details={"path": str(path), "error_type": type(exc).__name__},
        )
        typer.echo(f"cannot start core service: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        if ipc is not None:
            ipc.close()
        if sink is not None:
            sink.close()
    typer.echo(
        "core service stopped: "
        f"iterations={result.iterations} "
        f"forwarded_packets={result.forwarded_packets} forwarded_bytes={result.forwarded_bytes} "
        f"delivered_packets={result.delivered_packets} delivered_bytes={result.delivered_bytes}"
    )


@app.command("start")
def start(
    path: Path,
    name: str | None = typer.Option(None, "--name", help="Override the managed service name."),
    batch_size: int = typer.Option(32, help="Maximum UDP datagrams to drain per service step."),
    diagnostics_jsonl: Path | None = typer.Option(None, help="Append structured diagnostics events to this JSONL file."),
    scheduler_reapply_interval: float | None = typer.Option(
        None,
        "--scheduler-reapply-interval",
        help="Seconds between Python-owned status-to-scheduler hot reapply passes.",
    ),
) -> None:
    """Start a process-managed Rust-backed core service in the background."""
    try:
        runtime_config = expand_config(validate_config_file(path))
    except ConfigValidationError as exc:
        _render_error(exc)
    service_record_name = name or service_name("core", runtime_config.node)
    registry = ServiceRegistry()
    with suppress(ValueError):
        existing = registry.resolve(service_record_name)
        if existing.manager != "process":
            typer.echo(
                f"cannot replace {existing.name}: service is managed by {existing.manager}",
                err=True,
            )
            raise typer.Exit(1)
        registry.close(existing.name)
    service_dir = registry.path / service_record_name
    log_path = service_dir / "service.log"
    diagnostics_path = diagnostics_jsonl or service_dir / "diagnostics.jsonl"
    command = [
        sys.executable,
        "-m",
        "gatherlink.cli.main",
        "run",
        "service",
        str(path),
        "--batch-size",
        str(batch_size),
        "--diagnostics-jsonl",
        str(diagnostics_path),
        "--service-name",
        service_record_name,
        "--service-log",
        str(log_path),
    ]
    if scheduler_reapply_interval is not None:
        command.extend(["--scheduler-reapply-interval", str(scheduler_reapply_interval)])
    service_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    registry.register(
        ServiceRecord(
            name=service_record_name,
            kind="core",
            pid=process.pid,
            log_file=log_path,
            command=command,
            metadata={
                "config": str(path),
                "node": runtime_config.node,
                "role": runtime_config.role,
                "security_mode": runtime_config.security.mode,
                "diagnostics_jsonl": str(diagnostics_path),
            },
        )
    )
    typer.echo(f"started {service_record_name} pid={process.pid} log={log_path}")


def _publish_start_failed(
    diagnostics_bus: DiagnosticsBus | None,
    *,
    message: str,
    details: dict[str, object],
) -> None:
    """Persist startup failures through diagnostics when a sink is configured."""
    if diagnostics_bus is None:
        return
    diagnostics_bus.publish(DiagnosticEvent.runtime_start_failed(message=message, details=details))
    diagnostics_bus.drain()
