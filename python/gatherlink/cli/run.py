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
from gatherlink.runtime.helper_supervisor import HelperLaunchPlan, build_helper_launch_plans
from gatherlink.runtime.rekey import live_rekey_context_from_runtime
from gatherlink.runtime.relay_runner import RelayRunnerState, RelayRuntimeConfig, run_relay_service
from gatherlink.runtime.runner import DEFAULT_DATAPLANE_BATCH_SIZE, CoreRunnerState, run_core_service
from gatherlink.runtime.services import ServiceIpcServer, ServiceRecord, ServiceRegistry, service_name
from gatherlink.runtime.supervisor import plan_runtime_start
from gatherlink.secrets.bundles import SignedDocument
from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord
from gatherlink.secrets.provisioning import load_verified_topology_bundle

app = typer.Typer(help="Plan and run Gatherlink runtime services.")


def _render_error(exc: ConfigValidationError) -> None:
    """Render config errors for runtime commands without stack traces."""
    typer.echo(f"invalid: {exc.message}", err=True)
    for detail in exc.details:
        location = ".".join(detail.location) if detail.location else "config"
        typer.echo(f"  - {location}: {detail.message}", err=True)
    raise typer.Exit(1)


def _load_live_rekey_context(
    runtime_config,
    *,
    local_identity: Path | None,
    peer_identity: Path | None,
    topology: Path | None,
    trust_root: Path | None,
):
    """
    Load optional live-rekey context from explicit operator-provided files.

    Packet execution does not need these files; autonomous rekey does. Requiring
    all four paths keeps startup behavior predictable and avoids silently
    guessing trust roots or peer identities from unrelated local state.
    """
    values = [local_identity, peer_identity, topology, trust_root]
    if not any(values):
        return None
    if not all(values):
        raise ValueError(
            "autonomous rekey requires --rekey-local-identity, --rekey-peer-identity, "
            "--rekey-topology, and --rekey-trust-root together"
        )
    assert local_identity is not None
    assert peer_identity is not None
    assert topology is not None
    assert trust_root is not None
    local_record = IdentityRecord.load(local_identity)
    peer_record = IdentityPublicRecord.load(peer_identity)
    trust_record = IdentityPublicRecord.load(trust_root)
    topology_body = load_verified_topology_bundle(
        SignedDocument.load(topology),
        trusted_issuer=trust_record,
    )
    return live_rekey_context_from_runtime(
        local_record.to_identity(),
        peer_record,
        topology_body,
        runtime_config,
    )


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
    batch_size: int = typer.Option(
        DEFAULT_DATAPLANE_BATCH_SIZE,
        help="Maximum UDP datagrams to drain per service step.",
    ),
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
    rekey_local_identity: Path | None = typer.Option(
        None,
        "--rekey-local-identity",
        help="Local private identity JSON used to originate/accept autonomous authenticated rekey.",
    ),
    rekey_peer_identity: Path | None = typer.Option(
        None,
        "--rekey-peer-identity",
        help="Expected peer public or private identity JSON for autonomous authenticated rekey.",
    ),
    rekey_topology: Path | None = typer.Option(
        None,
        "--rekey-topology",
        help="Signed topology bundle used to validate autonomous authenticated rekey.",
    ),
    rekey_trust_root: Path | None = typer.Option(
        None,
        "--rekey-trust-root",
        help="Trusted topology issuer identity JSON for autonomous authenticated rekey.",
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
        live_rekey_context = _load_live_rekey_context(
            runtime_config,
            local_identity=rekey_local_identity,
            peer_identity=rekey_peer_identity,
            topology=rekey_topology,
            trust_root=rekey_trust_root,
        )
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
                        "security_source_mode": runtime_config.security.source_mode,
                    },
                )
            )
            ipc = ServiceIpcServer(
                record,
                status=runner_state.snapshot,
                stop=runner_state.stop,
                commands={
                    "control-cadence": runner_state.request_control_cadence,
                    "remote-status": runner_state.request_remote_status,
                    "service-outcome": runner_state.request_service_outcome,
                },
            )
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
            live_rekey_context=live_rekey_context,
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


@app.command("relay-service")
def relay_service(
    path: Path,
    max_iterations: int | None = typer.Option(
        None,
        help="Stop after this many relay poll iterations; intended for smoke tests.",
    ),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        help="Append structured diagnostics events to this JSONL file.",
    ),
    service_name_override: str | None = typer.Option(
        None,
        "--service-name",
        help="Register this foreground relay under a specific process-managed service name.",
    ),
    service_log: Path | None = typer.Option(
        None,
        "--service-log",
        help="Log file path recorded in the service registry when --service-name is used.",
    ),
) -> None:
    """Run a foreground Rust-backed secure relay-hop service from compiled runtime config."""
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    ipc: ServiceIpcServer | None = None
    runner_state: RelayRunnerState | None = None
    try:
        relay_config = RelayRuntimeConfig.load(path)
        if service_name_override is not None:
            runner_state = RelayRunnerState(
                name=relay_config.name,
                listen=relay_config.listen,
                next_hop=relay_config.executor.next_hop_address,
                direction=relay_config.executor.direction,
                stop_event=Event(),
                exit_to_inner_packet=relay_config.exit_to_inner_packet,
            )
            record = ServiceRegistry().register(
                ServiceRecord(
                    name=service_name_override,
                    kind="relay",
                    pid=os.getpid(),
                    log_file=service_log or Path(".gatherlink/logs") / f"{service_name_override}.log",
                    detached_from_console=False,
                    command=sys.argv,
                    metadata={
                        "config": str(path),
                        "relay_name": relay_config.name,
                        "listen": relay_config.listen,
                        "next_hop": relay_config.executor.next_hop_address,
                        "direction": relay_config.executor.direction,
                        "exit_to_inner_packet": relay_config.exit_to_inner_packet,
                    },
                )
            )
            ipc = ServiceIpcServer(record, status=runner_state.snapshot, stop=runner_state.stop)
            ipc.start()
        result = run_relay_service(
            relay_config,
            max_iterations=max_iterations,
            diagnostics_bus=diagnostics_bus,
            stop_event=runner_state.stop_event if runner_state is not None else None,
            runner_state=runner_state,
        )
    except (RustDataplaneUnavailableError, ValueError) as exc:
        _publish_start_failed(
            diagnostics_bus,
            message=f"cannot start relay service: {exc}",
            details={"path": str(path), "error_type": type(exc).__name__},
        )
        typer.echo(f"cannot start relay service: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        if ipc is not None:
            ipc.close()
        if sink is not None:
            sink.close()
    typer.echo(
        "relay service stopped: "
        f"iterations={result.iterations} "
        f"forwarded_packets={result.forwarded_packets} forwarded_bytes={result.forwarded_bytes} "
        f"dropped_packets={result.dropped_packets} "
        f"emitted_packets={result.emitted_packets} emitted_bytes={result.emitted_bytes}"
    )


@app.command("relay-start")
def relay_start(
    path: Path,
    name: str | None = typer.Option(None, "--name", help="Override the managed relay service name."),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        help="Append structured diagnostics events to this JSONL file.",
    ),
) -> None:
    """Start a process-managed Rust-backed secure relay-hop service in the background."""
    try:
        relay_config = RelayRuntimeConfig.load(path)
    except ValueError as exc:
        typer.echo(f"cannot start relay service: {exc}", err=True)
        raise typer.Exit(1) from exc
    service_record_name = name or service_name("relay", relay_config.name)
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
        "relay-service",
        str(path),
        "--diagnostics-jsonl",
        str(diagnostics_path),
        "--service-name",
        service_record_name,
        "--service-log",
        str(log_path),
    ]
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
            kind="relay",
            pid=process.pid,
            log_file=log_path,
            command=command,
            metadata={
                "config": str(path),
                "relay_name": relay_config.name,
                "listen": relay_config.listen,
                "next_hop": relay_config.executor.next_hop_address,
                "direction": relay_config.executor.direction,
                "exit_to_inner_packet": relay_config.exit_to_inner_packet,
                "diagnostics_jsonl": str(diagnostics_path),
            },
        )
    )
    typer.echo(f"started {service_record_name} pid={process.pid} log={log_path}")


@app.command("start")
def start(
    path: Path,
    name: str | None = typer.Option(None, "--name", help="Override the managed service name."),
    batch_size: int = typer.Option(
        DEFAULT_DATAPLANE_BATCH_SIZE,
        help="Maximum UDP datagrams to drain per service step.",
    ),
    diagnostics_jsonl: Path | None = typer.Option(
        None, help="Append structured diagnostics events to this JSONL file."
    ),
    scheduler_reapply_interval: float | None = typer.Option(
        None,
        "--scheduler-reapply-interval",
        help="Seconds between Python-owned status-to-scheduler hot reapply passes.",
    ),
    rekey_local_identity: Path | None = typer.Option(
        None,
        "--rekey-local-identity",
        help="Local private identity JSON used to originate/accept autonomous authenticated rekey.",
    ),
    rekey_peer_identity: Path | None = typer.Option(
        None,
        "--rekey-peer-identity",
        help="Expected peer public or private identity JSON for autonomous authenticated rekey.",
    ),
    rekey_topology: Path | None = typer.Option(
        None,
        "--rekey-topology",
        help="Signed topology bundle used to validate autonomous authenticated rekey.",
    ),
    rekey_trust_root: Path | None = typer.Option(
        None,
        "--rekey-trust-root",
        help="Trusted topology issuer identity JSON for autonomous authenticated rekey.",
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
    if any([rekey_local_identity, rekey_peer_identity, rekey_topology, rekey_trust_root]):
        if not all([rekey_local_identity, rekey_peer_identity, rekey_topology, rekey_trust_root]):
            typer.echo(
                "autonomous rekey requires --rekey-local-identity, --rekey-peer-identity, "
                "--rekey-topology, and --rekey-trust-root together",
                err=True,
            )
            raise typer.Exit(1)
        command.extend(
            [
                "--rekey-local-identity",
                str(rekey_local_identity),
                "--rekey-peer-identity",
                str(rekey_peer_identity),
                "--rekey-topology",
                str(rekey_topology),
                "--rekey-trust-root",
                str(rekey_trust_root),
            ]
        )
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
                "security_source_mode": runtime_config.security.source_mode,
                "diagnostics_jsonl": str(diagnostics_path),
            },
        )
    )
    typer.echo(f"started {service_record_name} pid={process.pid} log={log_path}")


@app.command("helpers-start")
def helpers_start(
    path: Path,
    name_prefix: str | None = typer.Option(None, "--name-prefix", help="Override the helper service name prefix."),
) -> None:
    """Start process-managed helpers declared by a config file."""
    try:
        runtime_config = expand_config(validate_config_file(path))
        registry = ServiceRegistry()
        plans = build_helper_launch_plans(runtime_config, registry_dir=registry.path, name_prefix=name_prefix)
    except (ConfigValidationError, ValueError) as exc:
        if isinstance(exc, ConfigValidationError):
            _render_error(exc)
        typer.echo(f"cannot start helpers: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not plans:
        typer.echo("no startable helpers declared")
        return
    for plan in plans:
        with suppress(ValueError):
            existing = registry.resolve(plan.name)
            if existing.manager != "process":
                typer.echo(
                    f"cannot replace {existing.name}: service is managed by {existing.manager}",
                    err=True,
                )
                raise typer.Exit(1)
            registry.close(existing.name)
        plan.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with plan.log_file.open("a", encoding="utf-8") as log_handle:
                process = subprocess.Popen(
                    plan.command,
                    cwd=Path.cwd(),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            _publish_helper_lifecycle_event(
                plan,
                code="helper.lifecycle.start_failed",
                message=f"cannot start helper {plan.name}: {exc}",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
            typer.echo(f"cannot start helper {plan.name}: {exc}", err=True)
            raise typer.Exit(1) from exc
        registry.register(
            ServiceRecord(
                name=plan.name,
                kind=plan.kind,
                pid=process.pid,
                log_file=plan.log_file,
                command=plan.command,
                metadata={
                    "config": str(path),
                    **plan.metadata,
                    **({"diagnostics_jsonl": str(plan.diagnostics_jsonl)} if plan.diagnostics_jsonl else {}),
                },
            )
        )
        _publish_helper_lifecycle_event(
            plan,
            code="helper.lifecycle.started",
            message=f"helper {plan.name} started",
            details={"pid": process.pid, "log_file": str(plan.log_file)},
        )
        typer.echo(f"started {plan.name} pid={process.pid} log={plan.log_file}")


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


def _publish_helper_lifecycle_event(
    plan: HelperLaunchPlan,
    *,
    code: str,
    message: str,
    severity: str = "info",
    details: dict[str, object] | None = None,
) -> None:
    """
    Persist process-supervisor helper lifecycle facts next to helper diagnostics.

    The helper process owns helper behavior. The runtime CLI only owns the
    process-management fact that a helper was started or could not be started.
    """
    if plan.diagnostics_jsonl is None:
        return
    sink = JsonlDiagnosticSink(plan.diagnostics_jsonl)
    try:
        bus = DiagnosticsBus(sinks=[sink])
        bus.publish(
            DiagnosticEvent.helper_event(
                code=code,
                helper=plan.kind.removeprefix("helper:"),
                severity=severity,
                message=message,
                details={
                    "service_name": plan.name,
                    "log_file": str(plan.log_file),
                    **plan.metadata,
                    **(details or {}),
                },
            )
        )
        bus.drain()
    finally:
        sink.close()
