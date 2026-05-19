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
