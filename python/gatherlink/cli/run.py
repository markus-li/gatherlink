"""
Runtime CLI commands.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.dataplane.rust_backend import RustDataplaneUnavailableError, RustRuntimeBridgeError
from gatherlink.runtime.runner import run_core_service
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
) -> None:
    """Run a foreground Rust-backed core service from a config file."""
    try:
        runtime_config = expand_config(validate_config_file(path))
        result = run_core_service(runtime_config, max_iterations=max_iterations, batch_size=batch_size)
    except ConfigValidationError as exc:
        _render_error(exc)
    except (RustDataplaneUnavailableError, RustRuntimeBridgeError, ValueError) as exc:
        typer.echo(f"cannot start core service: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        "core service stopped: "
        f"iterations={result.iterations} "
        f"forwarded_packets={result.forwarded_packets} forwarded_bytes={result.forwarded_bytes} "
        f"delivered_packets={result.delivered_packets} delivered_bytes={result.delivered_bytes}"
    )
