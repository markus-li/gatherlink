"""
Config-related CLI commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.expansion import expand_config
from gatherlink.config.loader import load_config_dict
from gatherlink.config.validation import detect_config_format, validate_config_file

app = typer.Typer(help="Validate and inspect Gatherlink configuration files.")


def _render_error(exc: ConfigValidationError, *, as_json: bool) -> None:
    """Render a validation error consistently for text and JSON CLI modes."""
    if as_json:
        typer.echo(json.dumps(exc.export_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(f"invalid: {exc.message}", err=True)
        for detail in exc.details:
            location = ".".join(detail.location) if detail.location else "config"
            typer.echo(f"  - {location}: {detail.message}", err=True)
    raise typer.Exit(1)


def _config_summary(path: Path, *, as_json: bool) -> str:
    """Build validation output without mixing CLI rendering and config logic."""
    source_format = detect_config_format(load_config_dict(path))
    config = validate_config_file(path, source_format=source_format)
    if as_json:
        return json.dumps(
            {
                "valid": True,
                "path": str(path),
                "source_format": source_format,
                "schema_version": config.schema_version,
                "role": config.role,
                "node": config.node,
            },
            indent=2,
            sort_keys=True,
        )
    return f"valid: {path} ({source_format}, schema v{config.schema_version}, {config.role})"


@app.command("detect")
def detect(path: Path) -> None:
    """Detect the input config format without fully rendering the config."""
    try:
        source_format = detect_config_format(load_config_dict(path))
    except ConfigValidationError as exc:
        _render_error(exc, as_json=False)
    typer.echo(source_format)


@app.command("validate")
def validate(
    path: Path,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable validation output.")] = False,
) -> None:
    """Validate a Gatherlink config file."""
    try:
        typer.echo(_config_summary(path, as_json=as_json))
    except ConfigValidationError as exc:
        _render_error(exc, as_json=as_json)


@app.command("show")
def show(
    path: Path,
    runtime: Annotated[
        bool,
        typer.Option("--runtime/--canonical", help="Print expanded runtime config instead of canonical user config."),
    ] = True,
) -> None:
    """Validate and print a Gatherlink config file."""
    try:
        config = validate_config_file(path)
        output = expand_config(config) if runtime else config
    except ConfigValidationError as exc:
        _render_error(exc, as_json=False)
    typer.echo(json.dumps(output.export_dict(), indent=2, sort_keys=True))
