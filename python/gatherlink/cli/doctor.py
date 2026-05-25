"""
Operational readiness checks for Gatherlink.

The doctor command is intentionally a Python control-plane tool. It validates
operator-facing facts such as config readability, diagnostics JSONL shape,
state layout, service registry health, and whether the compiled Rust dataplane
binding is importable. It does not inspect packet payloads or make runtime
policy decisions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
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

DEFAULT_GATHERLINK_LISTEN_PORT = 53820
COMMON_WIREGUARD_PORT = 51820
OPTIONAL_OPERATOR_TOOLS = {
    "wg": "WireGuard helper inspection",
    "wg-quick": "WireGuard helper lifecycle",
    "traefik": "QUIC/H3 carrier proxy labs",
}


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
    """Run local Gatherlink readiness checks."""
    checks: list[DoctorCheck] = [
        _check_python_runtime(),
        _check_rust_binding(),
        _check_package_versions(Path.cwd()),
        _check_state_layout(state_dir),
        _check_service_registry(service_registry),
        _check_release_hygiene(Path.cwd()),
        _check_optional_tools(),
    ]
    checks.extend(_check_config(path) for path in config_paths or [])
    checks.extend(_check_diagnostics_jsonl(path) for path in diagnostics_jsonl or [])
    if release_artifacts is not None:
        checks.append(_check_release_artifacts(release_artifacts))

    payload = {
        "schema_version": 1,
        "tool": "gatherlink doctor",
        "ok": all(check.ok for check in checks),
        "check_count": len(checks),
        "checks": [check.export_dict() for check in checks],
    }
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


def _check_package_versions(repo_root: Path) -> DoctorCheck:
    """Check that Python and Rust package manifests advertise one release version."""
    manifest_paths = [repo_root / "pyproject.toml"]
    manifest_paths.extend(sorted((repo_root / "crates").glob("*/Cargo.toml")))
    versions: dict[str, str] = {}
    problems: list[str] = []

    for manifest in manifest_paths:
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except OSError as exc:
            problems.append(f"cannot read {manifest}: {type(exc).__name__}")
            continue
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"cannot parse {manifest}: {exc}")
            continue

        label = str(manifest.relative_to(repo_root))
        version = _manifest_version(data)
        if version is None:
            problems.append(f"missing package version: {label}")
        else:
            versions[label] = version

    unique_versions = sorted(set(versions.values()))
    if len(unique_versions) > 1:
        problems.append(f"manifest versions differ: {', '.join(unique_versions)}")

    return DoctorCheck(
        name="package.versions",
        ok=not problems,
        message=(
            f"package manifests agree on {unique_versions[0]}"
            if not problems and unique_versions
            else "package manifest versions need attention"
        ),
        details={"repo_root": str(repo_root), "versions": versions, "problems": problems},
    )


def _manifest_version(data: dict[str, Any]) -> str | None:
    """Extract a version from Python or Rust manifest TOML data."""
    project = data.get("project")
    if isinstance(project, dict) and isinstance(project.get("version"), str):
        return project["version"]
    package = data.get("package")
    if isinstance(package, dict) and isinstance(package.get("version"), str):
        return package["version"]
    return None


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


def _check_release_hygiene(repo_root: Path) -> DoctorCheck:
    """
    Check tracked repository files for release-blocking private/local artifacts.

    Local `.gatherlink` state is expected during labs, so this check only looks
    at tracked files. That keeps doctor useful on active developer machines
    without hiding accidental commits of inventories, secrets, or generated
    reports.
    """
    tracked_files = _tracked_repo_files(repo_root)
    problems: list[str] = []
    for tracked in tracked_files:
        lower = tracked.lower()
        parts = Path(tracked).parts
        if parts and parts[0] == ".gatherlink":
            problems.append(f"tracked local state path: {tracked}")
        if any(part in {".ssh", ".gnupg"} for part in parts):
            problems.append(f"tracked private directory path: {tracked}")
        if _looks_like_secret_artifact(tracked):
            allowed_docs_or_tests = tracked.startswith(("docs/", "tests/")) or tracked.endswith(".example.env")
            if not allowed_docs_or_tests:
                problems.append(f"secret-like tracked path needs review: {tracked}")
        if tracked.startswith("docs/reports/") and (
            lower.endswith(".jsonl") or "/hyperv-" in lower or "acceptance/" in lower
        ):
            problems.append(f"generated report appears tracked: {tracked}")

    return DoctorCheck(
        name="release.hygiene",
        ok=not problems,
        message="tracked release hygiene looks clean" if not problems else "tracked release hygiene needs attention",
        details={
            "repo_root": str(repo_root),
            "tracked_files_checked": len(tracked_files),
            "problems": problems,
        },
    )


def _tracked_repo_files(repo_root: Path) -> list[str]:
    """Return Git-tracked files when available, otherwise an empty list."""
    if not (repo_root / ".git").exists():
        return []
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _looks_like_secret_artifact(tracked: str) -> bool:
    """Return whether a tracked path looks like private material, not code about secrets."""
    path = Path(tracked)
    lower_name = path.name.lower()
    lower = tracked.lower()
    secret_suffixes = (".pem", ".key", ".p12", ".pfx", ".age", ".gpg")
    if lower_name.endswith(secret_suffixes):
        return True
    return any(marker in lower for marker in ("private-key", "identity-key", "authorized_keys", "known_hosts"))


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
    warnings.extend(_config_operator_warnings(runtime_config))
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
            "operator_facts": _config_operator_facts(runtime_config),
            "warnings": warnings,
        },
    )


def _check_optional_tools() -> DoctorCheck:
    """
    Report optional host tools without failing readiness.

    These tools are not core Gatherlink dependencies. Doctor still reports them
    because missing `wg`, `wg-quick`, or Traefik explains why a helper or carrier
    lab cannot run on a given host.
    """
    tools = {
        name: {"purpose": purpose, "path": shutil.which(name)}
        for name, purpose in sorted(OPTIONAL_OPERATOR_TOOLS.items())
    }
    missing = [name for name, facts in tools.items() if facts["path"] is None]
    message = "optional operator tools available" if not missing else f"optional tools missing: {', '.join(missing)}"
    return DoctorCheck(
        name="operator.optional_tools",
        ok=True,
        message=message,
        details={"tools": tools, "missing": missing},
    )


def _config_operator_facts(runtime_config: Any) -> dict[str, Any]:
    """Return listener and carrier facts useful for operator review."""
    path_binds = [
        {
            "path": path.name,
            "carrier": path.carrier,
            "transport_bind": path.transport_bind,
            "transport_remote": path.transport_remote,
            "carrier_max_datagram_size": path.carrier_max_datagram_size,
            "effective_datagram_mtu": _path_effective_datagram_mtu(path, runtime_config.security.packet_overhead),
            "requires_python_carrier_supervision": path.carrier != "udp",
        }
        for path in runtime_config.paths
    ]
    service_listens = [
        {
            "service": service.name,
            "listen": service.listen,
            "target": service.target,
            "return_mode": service.return_mode,
        }
        for service in runtime_config.services
    ]
    return {
        "default_gatherlink_udp_port": DEFAULT_GATHERLINK_LISTEN_PORT,
        "wireguard_common_udp_port": COMMON_WIREGUARD_PORT,
        "path_binds": path_binds,
        "service_listens": service_listens,
    }


def _path_effective_datagram_mtu(path: Any, packet_overhead: int) -> int:
    """Return the operator-visible payload ceiling after local carrier/security overhead."""
    carrier_ceiling = path.carrier_max_datagram_size or path.scheduler.mtu
    return max(0, min(path.scheduler.mtu, carrier_ceiling) - packet_overhead)


def _config_operator_warnings(runtime_config: Any) -> list[str]:
    """
    Return non-fatal operator warnings for common local port mistakes.

    This is intentionally a check, not policy. Gatherlink endpoints are
    configurable and doctor must not mutate the host or reject deliberate labs.
    """
    warnings: list[str] = []
    endpoints: list[tuple[str, str]] = []
    for path in runtime_config.paths:
        if path.transport_bind:
            endpoints.append((f"path:{path.name}", path.transport_bind))
        if path.carrier != "udp":
            warnings.extend(_carrier_config_warnings(path, role=runtime_config.role))
    for service in runtime_config.services:
        if service.listen:
            endpoints.append((f"service:{service.name}", service.listen))

    seen: dict[str, str] = {}
    for owner, endpoint in endpoints:
        previous = seen.get(endpoint)
        if previous is not None:
            warnings.append(f"duplicate local endpoint {endpoint} used by {previous} and {owner}")
        else:
            seen[endpoint] = owner
        port = _endpoint_port(endpoint)
        if port == COMMON_WIREGUARD_PORT and owner.startswith("path:"):
            warnings.append(
                f"{owner} binds UDP {COMMON_WIREGUARD_PORT}, which commonly belongs to WireGuard; "
                f"Gatherlink carrier listeners usually use UDP {DEFAULT_GATHERLINK_LISTEN_PORT} or explicit lab ports"
            )
    return warnings


def _carrier_config_warnings(path: Any, *, role: str) -> list[str]:
    """Return doctor warnings for standard carriers that need Python supervision."""
    warnings = [
        f"path:{path.name} uses {path.carrier}; Python carrier supervision must start before Rust receives a local UDP endpoint"
    ]
    if not path.transport_bind:
        warnings.append(f"path:{path.name} uses {path.carrier} but has no transport_bind for the carrier listener")
    if role == "client" and not path.transport_remote:
        warnings.append(f"path:{path.name} uses {path.carrier} but client configs need transport_remote to connect")
    return warnings


def _endpoint_port(endpoint: str) -> int | None:
    """Parse the final numeric port from IPv4, hostname, or bracketed IPv6 endpoints."""
    try:
        return int(endpoint.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


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
    """Validate that a prepared release artifact directory has the expected release shape."""
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
