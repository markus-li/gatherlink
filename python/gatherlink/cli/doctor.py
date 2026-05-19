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
    release_artifacts: Path | None = typer.Option(
        None,
        "--release-artifacts",
        help="Validate a prepared release artifact directory.",
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
    if release_artifacts is not None:
        checks.append(_check_release_artifacts(release_artifacts))

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


def _check_release_artifacts(path: Path) -> DoctorCheck:
    """Validate that a prepared release artifact directory has the expected v0.9.1 shape."""
    expected_dirs = {
        "python-wheel": path / "python-wheel",
        "rust-binaries": path / "rust-binaries",
        "wiki-user-docs": path / "wiki-user-docs",
    }
    expected_files = {
        "checksums": path / "SHA256SUMS",
    }
    missing_dirs = [name for name, dir_path in expected_dirs.items() if not dir_path.is_dir()]
    missing_files = [name for name, file_path in expected_files.items() if not file_path.is_file()]
    source_archives = sorted(path.glob("gatherlink-*-source.tar.gz"))
    wheels = sorted((path / "python-wheel").glob("gatherlink-*.whl")) if (path / "python-wheel").is_dir() else []
    rust_binaries = (
        sorted(item.name for item in (path / "rust-binaries").iterdir() if item.is_file())
        if (path / "rust-binaries").is_dir()
        else []
    )
    wiki_docs = (
        sorted(item.name for item in (path / "wiki-user-docs").iterdir() if item.is_file())
        if (path / "wiki-user-docs").is_dir()
        else []
    )

    problems: list[str] = []
    problems.extend(f"missing directory: {name}" for name in missing_dirs)
    problems.extend(f"missing file: {name}" for name in missing_files)
    if not source_archives:
        problems.append("missing source archive")
    if not wheels:
        problems.append("missing Python wheel")
    if "gatherlink-time-helper" not in rust_binaries:
        problems.append("missing Rust gatherlink-time-helper binary")
    if "README.md" not in wiki_docs:
        problems.append("missing Wiki README.md payload")
    checksum_errors = _release_checksum_errors(path / "SHA256SUMS", source_archives, wheels, rust_binaries)
    problems.extend(checksum_errors)

    return DoctorCheck(
        name="release.artifacts",
        ok=not problems,
        message=f"release artifacts valid: {path}" if not problems else f"release artifacts incomplete: {path}",
        details={
            "path": str(path),
            "source_archives": [archive.name for archive in source_archives],
            "wheels": [wheel.name for wheel in wheels],
            "rust_binaries": rust_binaries,
            "wiki_docs": wiki_docs,
            "problems": problems,
        },
    )


def _release_checksum_errors(
    checksum_path: Path,
    source_archives: list[Path],
    wheels: list[Path],
    rust_binaries: list[str],
) -> list[str]:
    """Return missing checksum rows for expected release artifacts."""
    if not checksum_path.is_file():
        return []
    try:
        checksum_text = checksum_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read checksum file: {type(exc).__name__}"]
    expected_names = [archive.name for archive in source_archives]
    expected_names.extend(wheel.name for wheel in wheels)
    expected_names.extend(rust_binaries)
    return [f"missing checksum row: {name}" for name in expected_names if name not in checksum_text]


def _print_human(payload: dict[str, Any]) -> None:
    """Render checks as compact terminal output."""
    typer.echo("Gatherlink doctor")
    for check in payload["checks"]:
        label = "ok" if check["ok"] else "fail"
        typer.echo(f"{label:4} {check['name']}  {check['message']}")
    typer.echo(f"doctor: {'ok' if payload['ok'] else 'failed'}")
