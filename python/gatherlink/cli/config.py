"""
Config-related CLI commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.expansion import expand_config
from gatherlink.config.loader import load_config_dict
from gatherlink.config.migration import migrate_config_dict
from gatherlink.config.validation import detect_config_format, validate_config_file
from gatherlink.config.versions import CURRENT_SCHEMA_VERSION
from gatherlink.persistence.store import redact_secrets

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


def _validation_summary(path: Path, *, as_json: bool) -> str:
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
        typer.echo(_validation_summary(path, as_json=as_json))
    except ConfigValidationError as exc:
        _render_error(exc, as_json=as_json)


@app.command("migrate")
def migrate(
    path: Path,
    target_version: int = typer.Option(
        CURRENT_SCHEMA_VERSION,
        "--to-schema-version",
        help="Target config schema version.",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--write",
        help="Print migrated config by default; --write replaces the input file.",
    ),
) -> None:
    """Migrate a config through explicit version-to-version transforms."""
    try:
        data = load_config_dict(path)
        source_format = detect_config_format(data)
        result = migrate_config_dict(data, target_version=target_version, source_format=source_format)
    except ConfigValidationError as exc:
        _render_error(exc, as_json=True)
    payload = result.export_dict()
    if dry_run:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    path.write_text(json.dumps(result.config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    typer.echo(json.dumps({**payload, "written": str(path)}, indent=2, sort_keys=True))


@app.command("show")
def show(
    path: Path,
    runtime: Annotated[
        bool,
        typer.Option("--runtime/--canonical", help="Print expanded runtime config instead of canonical user config."),
    ] = True,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON output. This is the default format today."),
    ] = False,
) -> None:
    """Validate and print a Gatherlink config file."""
    try:
        config = validate_config_file(path)
        output = expand_config(config) if runtime else config
    except ConfigValidationError as exc:
        _render_error(exc, as_json=as_json)
    typer.echo(json.dumps(redact_secrets(output.export_dict()), indent=2, sort_keys=True))


def runtime_summary_payload(runtime: Any) -> dict[str, Any]:
    """
    Return a stable, redacted summary of the compiled runtime contract.

    This is intentionally less detailed than `config show --runtime`: scripts can
    compare it across releases without depending on every internal DTO field,
    while operators still get the important path, service, helper, security, and
    scheduler facts that explain what Python will hand to the runner.
    """
    security = runtime.security
    security_session_count = len(security.sessions) or int(security.mode != "none")
    return {
        "schema_version": 1,
        "runtime_model": runtime.metadata.get("runtime_model", runtime.__class__.__name__),
        "node": runtime.node,
        "role": runtime.role,
        "peer": runtime.peer,
        "security": {
            "mode": security.mode,
            "source_mode": security.source_mode,
            "session_count": security_session_count,
            "receiver_indexes": [session.local_receiver_index for session in security.sessions]
            or [security.local_receiver_index],
            "remote_receiver_indexes": [session.remote_receiver_index for session in security.sessions]
            or [security.remote_receiver_index],
            "packet_overhead": security.packet_overhead,
        },
        "paths": [
            {
                "name": path.name,
                "path_id": path.scheduler.path_id,
                "interface": path.interface,
                "carrier": path.carrier,
                "transport_bind": path.transport_bind,
                "transport_remote": path.transport_remote,
                "carrier_max_datagram_size": path.carrier_max_datagram_size,
                "scheduler": {
                    "enabled": path.scheduler.enabled,
                    "state": path.scheduler.state,
                    "weight": path.scheduler.weight,
                    "mtu": path.scheduler.mtu,
                    "tx_capacity_bps": path.scheduler.tx_capacity_bps,
                    "rx_capacity_bps": path.scheduler.rx_capacity_bps,
                    "latency_us": path.scheduler.latency_us,
                    "loss_ppm": path.scheduler.loss_ppm,
                    "reorder_hold_us": path.scheduler.reorder_hold_us,
                    "max_in_flight_packets": path.scheduler.max_in_flight_packets,
                    "max_in_flight_bytes": path.scheduler.max_in_flight_bytes,
                    "pacing_budget_bps": path.scheduler.pacing_budget_bps,
                    "queue_depth_packets": path.scheduler.queue_depth_packets,
                    "queue_depth_bytes": path.scheduler.queue_depth_bytes,
                    "queue_oldest_age_us": path.scheduler.queue_oldest_age_us,
                },
                "relay_enabled": path.relay is not None,
            }
            for path in runtime.paths
        ],
        "services": [
            {
                "name": service.name,
                "service_id": service.service_id,
                "service_id_explicit": service.service_id_explicit,
                "protocol": service.protocol,
                "listen": service.listen,
                "target": service.target,
                "priority": service.priority,
                "priority_value": service.priority_value,
                "return_mode": service.return_mode,
                "scheduler_fanout": service.scheduler_fanout,
                "scheduler_fanout_below_bytes": service.scheduler_fanout_below_bytes,
                "scheduler_flowlet_idle_us": service.scheduler_flowlet_idle_us,
                "scheduler_flowlet_max_hold_us": service.scheduler_flowlet_max_hold_us,
                "scheduler_poll_batch_packets": service.scheduler_poll_batch_packets,
                "scheduler_path_run_datagrams": service.scheduler_path_run_datagrams,
                "scheduler_path_policy": service.scheduler_path_policy,
                "scheduler_allowed_path_ids": service.scheduler_allowed_path_ids,
                "scheduler_path_weights": service.scheduler_path_weights,
            }
            for service in runtime.services
        ],
        "helpers": [
            {
                "kind": helper.kind,
                "enabled": helper.enabled,
                "service": getattr(helper, "service", None),
                "listen": getattr(helper, "listen", None),
            }
            for helper in runtime.helpers
        ],
        "scheduler": {
            "mode": runtime.scheduler.mode,
            "path_count": len(runtime.scheduler.paths),
            "active_paths": sum(1 for path in runtime.scheduler.paths if path.enabled and path.state == "active"),
            "draining_paths": sum(1 for path in runtime.scheduler.paths if path.state == "drain"),
            "disabled_paths": sum(1 for path in runtime.scheduler.paths if not path.enabled or path.state == "down"),
        },
        "counts": {
            "paths": len(runtime.paths),
            "services": len(runtime.services),
            "helpers": len(runtime.helpers),
            "security_sessions": security_session_count,
        },
    }


@app.command("summary")
def summary(path: Path) -> None:
    """Print a stable redacted summary of compiled runtime facts."""
    try:
        runtime = expand_config(validate_config_file(path))
    except ConfigValidationError as exc:
        _render_error(exc, as_json=True)
    typer.echo(json.dumps(runtime_summary_payload(runtime), indent=2, sort_keys=True))


@app.command("diff")
def diff(
    current: Path,
    candidate: Path,
    runtime: Annotated[
        bool,
        typer.Option(
            "--runtime/--canonical", help="Compare expanded runtime configs instead of canonical user configs."
        ),
    ] = True,
) -> None:
    """Compare two Gatherlink configs after validation and redaction."""
    try:
        current_config = validate_config_file(current)
        candidate_config = validate_config_file(candidate)
        current_output = expand_config(current_config) if runtime else current_config
        candidate_output = expand_config(candidate_config) if runtime else candidate_config
    except ConfigValidationError as exc:
        _render_error(exc, as_json=True)
    payload = config_diff_payload(
        redact_secrets(current_output.export_dict()),
        redact_secrets(candidate_output.export_dict()),
        current_path=current,
        candidate_path=candidate,
        mode="runtime" if runtime else "canonical",
    )
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def config_diff_payload(
    current: dict[str, Any],
    candidate: dict[str, Any],
    *,
    current_path: Path,
    candidate_path: Path,
    mode: str,
) -> dict[str, Any]:
    """Return an operator-safe structural diff between two redacted config dicts."""
    current_flat = _flatten_json(current)
    candidate_flat = _flatten_json(candidate)
    current_keys = set(current_flat)
    candidate_keys = set(candidate_flat)
    added = sorted(candidate_keys - current_keys)
    removed = sorted(current_keys - candidate_keys)
    changed = sorted(key for key in current_keys & candidate_keys if current_flat[key] != candidate_flat[key])
    return {
        "schema_version": 1,
        "mode": mode,
        "current_path": str(current_path),
        "candidate_path": str(candidate_path),
        "changed": [{"path": key, "current": current_flat[key], "candidate": candidate_flat[key]} for key in changed],
        "added": [{"path": key, "candidate": candidate_flat[key]} for key in added],
        "removed": [{"path": key, "current": current_flat[key]} for key in removed],
        "summary": {
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
            "different": bool(changed or added or removed),
        },
    }


def _flatten_json(value: Any, *, prefix: str = "") -> dict[str, Any]:
    """Flatten JSON-compatible values into stable dotted paths for review diffs."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key in sorted(value):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            output.update(_flatten_json(value[key], prefix=child_prefix))
        return output
    if isinstance(value, list):
        output = {}
        for index, item in enumerate(value):
            output.update(_flatten_json(item, prefix=f"{prefix}[{index}]"))
        if not value:
            output[prefix] = []
        return output
    return {prefix: value}
