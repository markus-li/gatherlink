"""
Operational readiness checks for Gatherlink v0.9.

The doctor command is intentionally a Python control-plane tool. It validates
operator-facing facts such as config readability, diagnostics JSONL shape,
state layout, service registry health, and whether the compiled Rust dataplane
binding is importable. It does not inspect packet payloads or make runtime
policy decisions.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from gatherlink.config.errors import ConfigValidationError
from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.dataplane.rust_backend import RustDataplaneUnavailableError, _load_bindings
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.persistence.store import GatherlinkStatePaths, PersistentStateStore, redact_secrets
from gatherlink.runtime.plan import runtime_warnings
from gatherlink.runtime.services import ServiceRegistry


@dataclass
class DoctorCheck:
    """One operator-safe doctor check result."""

    name: str
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def export_dict(self) -> dict[str, Any]:
        """Return a JSON-safe and secret-redacted check payload."""
        return {
            "name": self.name,
            "ok": self.ok,
            "message": self.message,
            "details": redact_secrets(self.details),
        }


def doctor(
    config_paths: list[Path] | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Config file to validate; repeat for multiple configs.",
    ),
    diagnostics_jsonl: list[Path] | None = typer.Option(
        None,
        "--diagnostics-jsonl",
        help="Diagnostics JSONL file to validate; repeat for multiple files.",
    ),
    state_dir: Path | None = typer.Option(
        None,
        "--state-dir",
        help="Override the persistent state directory used for state-layout checks.",
    ),
    service_registry: Path | None = typer.Option(
        None,
        "--service-registry",
        help="Override the service registry directory used for registry checks.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable doctor results."),
) -> None:
    """Run local Gatherlink v0.9 readiness checks."""
    checks: list[DoctorCheck] = [
        _check_python_runtime(),
        _check_rust_binding(),
        _check_state_layout(state_dir),
        _check_service_registry(service_registry),
    ]
    checks.extend(_check_config(path) for path in config_paths or [])
    checks.extend(_check_diagnostics_jsonl(path) for path in diagnostics_jsonl or [])

    payload = {"ok": all(check.ok for check in checks), "checks": [check.export_dict() for check in checks]}
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload)
    if not payload["ok"]:
        raise typer.Exit(1)


def _check_python_runtime() -> DoctorCheck:
    """Validate the Python runtime against the package support contract."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 12)
    return DoctorCheck(
        name="python.runtime",
        ok=ok,
        message=f"Python {version}" if ok else f"Python {version} is below the supported >=3.12 runtime",
        details={"executable": sys.executable, "version": version, "minimum": "3.12"},
    )


def _check_rust_binding() -> DoctorCheck:
    """Validate that the optional PyO3 Rust dataplane API is importable."""
    try:
        bindings = _load_bindings()
    except RustDataplaneUnavailableError as exc:
        return DoctorCheck(
            name="rust.dataplane_binding",
            ok=False,
            message=str(exc),
            details={"module": "gatherlink_pybindings"},
        )

    required_symbols = ["CoreDataplane", "RelayHopForwarder", "PathConfig", "UdpServiceConfig"]
    missing = [symbol for symbol in required_symbols if not hasattr(bindings, symbol)]
    return DoctorCheck(
        name="rust.dataplane_binding",
        ok=not missing,
        message="Rust dataplane binding is importable" if not missing else "Rust binding is missing expected symbols",
        details={"module": "gatherlink_pybindings", "missing_symbols": missing},
    )


def _check_state_layout(state_dir: Path | None) -> DoctorCheck:
    """Validate that the state store summary can be produced without leaking secrets."""
    try:
        if state_dir is None:
            store = PersistentStateStore.debian()
        else:
            paths = GatherlinkStatePaths(
                config_dir=state_dir / "config",
                state_dir=state_dir,
                runtime_dir=state_dir / "run",
                log_dir=state_dir / "logs",
            )
            store = PersistentStateStore(paths)
        summary = _state_summary_for_doctor(store.public_summary())
    except OSError as exc:
        return DoctorCheck(
            name="state.layout",
            ok=False,
            message=f"cannot inspect state layout: {exc}",
            details={"state_dir": str(state_dir) if state_dir else None, "error_type": type(exc).__name__},
        )
    return DoctorCheck(
        name="state.layout",
        ok=True,
        message=f"state layout readable at {summary['state_dir']}",
        details=summary,
    )


def _state_summary_for_doctor(summary: dict[str, Any]) -> dict[str, Any]:
    """Return state facts useful for doctor output without secret-adjacent names."""
    artifact_counts = {
        "identities": len(summary.get("identities", [])),
        "trust_roots": len(summary.get("trust_roots", [])),
        "bundles": len(summary.get("bundles", [])),
        "hints": len(summary.get("hints", [])),
        "sealed_artifacts": len(summary.get("sealed_secrets", [])),
    }
    return {
        "config_dir": summary["config_dir"],
        "state_dir": summary["state_dir"],
        "runtime_dir": summary["runtime_dir"],
        "log_dir": summary["log_dir"],
        "artifact_counts": artifact_counts,
    }


def _check_service_registry(service_registry: Path | None) -> DoctorCheck:
    """Validate that the service registry can be listed and stale PIDs cleaned."""
    try:
        registry = ServiceRegistry(path=service_registry)
        services = registry.list()
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            name="service.registry",
            ok=False,
            message=f"cannot inspect service registry: {exc}",
            details={"path": str(service_registry) if service_registry else None, "error_type": type(exc).__name__},
        )
    return DoctorCheck(
        name="service.registry",
        ok=True,
        message=f"service registry readable with {len(services)} service(s)",
        details={
            "path": str(registry.path),
            "services": [
                {
                    "name": service.name,
                    "kind": service.kind,
                    "manager": service.manager,
                    "state": service.status_label(),
                }
                for service in services
            ],
        },
    )


def _check_config(path: Path) -> DoctorCheck:
    """Validate one config and expand it into runtime state."""
    try:
        config = validate_config_file(path)
        runtime_config = expand_config(config)
    except ConfigValidationError as exc:
        return DoctorCheck(
            name="config.validate",
            ok=False,
            message=f"invalid config: {path}",
            details={
                "path": str(path),
                "source_format": exc.source_format,
                "errors": [detail.export_dict() for detail in exc.details],
            },
        )
    warnings = runtime_warnings(runtime_config)
    return DoctorCheck(
        name="config.validate",
        ok=True,
        message=f"valid config: {path}" if not warnings else f"valid config with {len(warnings)} warning(s): {path}",
        details={
            "path": str(path),
            "node": runtime_config.node,
            "role": runtime_config.role,
            "security_mode": runtime_config.security.mode,
            "security_source_mode": runtime_config.security.source_mode,
            "services": len(runtime_config.services),
            "paths": len(runtime_config.paths),
            "warnings": warnings,
        },
    )


def _check_diagnostics_jsonl(path: Path) -> DoctorCheck:
    """Validate one diagnostics JSONL file against the normalized event DTO."""
    event_count = 0
    invalid_rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    DiagnosticEvent(**json.loads(line))
                    event_count += 1
                except (json.JSONDecodeError, ValidationError) as exc:
                    invalid_rows.append({"line": line_number, "error_type": type(exc).__name__, "message": str(exc)})
    except OSError as exc:
        return DoctorCheck(
            name="diagnostics.jsonl",
            ok=False,
            message=f"cannot read diagnostics JSONL: {path}",
            details={"path": str(path), "error_type": type(exc).__name__, "error": str(exc)},
        )
    return DoctorCheck(
        name="diagnostics.jsonl",
        ok=not invalid_rows,
        message=f"diagnostics JSONL valid: {path}" if not invalid_rows else f"diagnostics JSONL invalid: {path}",
        details={"path": str(path), "events": event_count, "invalid_rows": invalid_rows},
    )


def _print_human(payload: dict[str, Any]) -> None:
    """Render checks as compact terminal output."""
    typer.echo("Gatherlink doctor")
    for check in payload["checks"]:
        label = "ok" if check["ok"] else "fail"
        typer.echo(f"{label:4} {check['name']}  {check['message']}")
    typer.echo(f"doctor: {'ok' if payload['ok'] else 'failed'}")
