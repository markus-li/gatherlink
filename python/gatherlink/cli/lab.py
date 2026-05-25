"""Lab scenario CLI commands."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import typer

from gatherlink.lab.bundles import (
    execute_lab_bundle_cleanup,
    generate_lab_bundle,
    is_lab_bundle_manifest,
    plan_lab_bundle_cleanup,
    preflight_lab_bundle,
)
from gatherlink.lab.helper_smoke import run_all_helper_smokes
from gatherlink.lab.reports import write_three_path_scheduler_report
from gatherlink.lab.runtime import (
    apply_lab_network_mode,
    apply_lab_profile,
    apply_lab_shape,
    apply_lab_shape_profile,
    apply_lab_sink_view_rates,
    cleanup_lab_runtime,
    clear_lab_shape,
    inspect_lab_interfaces,
    prepare_lab_runtime,
    read_service_status,
    read_sink_service_status,
    request_lab_service_disable,
    run_rust_transport_smoke,
    run_shared_sink_transport_smoke,
    run_standard_carrier_comparison,
    run_standard_carrier_proxy_smoke,
    run_standard_carrier_smoke,
    run_udp_forwarder,
    run_udp_sink,
    run_udp_sink_service,
    run_udp_smoke_test,
    send_udp_packets,
    send_udp_packets_from_sink,
    start_lab_service,
    start_lab_sink_service,
    stop_lab_service,
)
from gatherlink.lab.scenarios import (
    LabShapeConfig,
    load_lab_scenario_file,
    load_lab_shape_profile_file,
    plan_lab_scenario,
)

app = typer.Typer(help="Plan and later run local Gatherlink lab scenarios.")


@app.command("plan")
def plan(path: Path) -> None:
    """Print the lab plan for a scenario config."""
    scenario = load_lab_scenario_file(path)
    lab_plan = plan_lab_scenario(scenario)
    typer.echo(json.dumps(lab_plan.export_dict(), indent=2, sort_keys=True))


@app.command("helpers-smoke")
def helpers_smoke() -> None:
    """Run local userland smoke scenarios for all active helpers."""
    results = run_all_helper_smokes()
    for result in results:
        status = "ok" if result.ok else "failed"
        typer.echo(f"{result.helper}: {status} {result.detail}")
    if not all(result.ok for result in results):
        raise typer.Exit(1)


@app.command("bundle")
def bundle(
    topology: str = typer.Argument(..., help="Bundle topology to generate, for example hyperv-three-node."),
    out: Path = typer.Option(..., "--out", help="Output directory for manifest, configs, and commands."),
) -> None:
    """Generate an operator-safe lab bundle without mutating host state."""
    try:
        result = generate_lab_bundle(topology, out)  # type: ignore[arg-type]
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"lab bundle: wrote manifest={result.manifest_path}")
    typer.echo(f"lab bundle: wrote commands={result.command_path}")
    for config_path in result.config_paths:
        typer.echo(f"lab bundle: wrote config={config_path}")


@app.command("preflight")
def preflight(manifest: Path) -> None:
    """Run read-only checks against a generated lab bundle manifest."""
    report = preflight_lab_bundle(manifest)
    for finding in report.findings:
        node = f" node={finding.node}" if finding.node else ""
        typer.echo(f"lab preflight: {finding.status} code={finding.code}{node} {finding.message}")
    if not report.ok:
        raise typer.Exit(1)


@app.command("up")
def up(
    path: Path,
    sink_local_ipc: bool = typer.Option(
        True,
        "--sink-local-ipc/--sink-no-local-ipc",
        help="Expose the sink directly; disable to hide local sink IPC and expose the normal sink name through remote status.",
    ),
) -> None:
    """Prepare paths and start the unprivileged background lab service."""
    scenario = load_lab_scenario_file(path)
    lab_plan = plan_lab_scenario(scenario)
    typer.echo(f"lab: {scenario.name} ({scenario.scenario})")
    for warning in lab_plan.warnings:
        typer.echo(warning)
    typer.echo("lab: preparing simulated network paths with sudo ip")
    try:
        results = prepare_lab_runtime(scenario)
    except subprocess.CalledProcessError as exc:
        typer.echo(f"failed to prepare lab network path: {' '.join(exc.cmd)}", err=True)
        if exc.stderr:
            typer.echo(exc.stderr.strip(), err=True)
        raise typer.Exit(1) from exc
    for result in results:
        shape_suffix = f" shape={','.join(result.shape_actions)}" if result.shape_actions else ""
        typer.echo(
            "lab path: "
            f"{result.name} {result.status} "
            f"client={result.client_namespace}/{result.client_interface} "
            f"server={result.server_namespace}/{result.server_interface}"
            f"{shape_suffix}"
        )
    try:
        service_result = start_lab_service(path, scenario, request_remote_status=not sink_local_ipc)
        sink_result = start_lab_sink_service(path, scenario, local_ipc=sink_local_ipc)
    except RuntimeError as exc:
        typer.echo(f"failed to start lab service: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"lab service: {service_result.status} name={service_result.name} "
        f"pid={service_result.pid} user={service_result.user} "
        f"pid_file={service_result.pid_file} log={service_result.log_file}"
    )
    typer.echo(
        f"lab sink service: {sink_result.status} name={sink_result.name} "
        f"pid={sink_result.pid} user={sink_result.user} "
        f"pid_file={sink_result.pid_file} log={sink_result.log_file} "
        f"local_ipc={'visible' if sink_local_ipc else 'hidden'}"
    )


@app.command("service", hidden=True)
def service(path: Path) -> None:
    """Run the foreground unprivileged lab service worker."""
    scenario = load_lab_scenario_file(path)
    try:
        run_udp_forwarder(scenario)
    except RuntimeError as exc:
        typer.echo(f"invalid lab service invocation: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("sink-service", hidden=True)
def sink_service(path: Path) -> None:
    """Run the foreground unprivileged lab sink worker."""
    scenario = load_lab_scenario_file(path)
    try:
        run_udp_sink_service(scenario)
    except RuntimeError as exc:
        typer.echo(f"invalid lab sink service invocation: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("status")
def status(path: Path) -> None:
    """Show lab service and configured path status."""
    scenario = load_lab_scenario_file(path)
    service_status = read_service_status(scenario)
    sink_status = read_sink_service_status(scenario)
    state = "running" if service_status.running else "stopped"
    sink_state = "running" if sink_status.running else "stopped"
    typer.echo(f"lab: {scenario.name} ({scenario.scenario})")
    typer.echo(f"lab service: {state} pid={service_status.pid} log={service_status.log_file}")
    typer.echo(f"lab sink service: {sink_state} pid={sink_status.pid} log={sink_status.log_file}")
    for lab_path in scenario.paths:
        typer.echo(
            f"lab path: {lab_path.name} subnet={lab_path.subnet} "
            f"client={lab_path.client_address} server={lab_path.server_address}"
        )


@app.command("interfaces")
def interfaces(path: Path) -> None:
    """Show lab interfaces inside their Linux network namespaces."""
    scenario = load_lab_scenario_file(path)
    try:
        outputs = inspect_lab_interfaces(scenario)
    except subprocess.CalledProcessError as exc:
        typer.echo(f"failed to inspect lab interface: {' '.join(exc.cmd)}", err=True)
        if exc.stderr:
            typer.echo(exc.stderr.strip(), err=True)
        raise typer.Exit(1) from exc
    for output in outputs:
        typer.echo(output)


@app.command("sink")
def sink(
    path: Path,
    count: int | None = typer.Option(None, help="Stop after receiving this many packets."),
    timeout: float | None = typer.Option(None, help="Stop after this many seconds without requiring Ctrl-C."),
) -> None:
    """Run a simple UDP sink on the lab target address."""
    scenario = load_lab_scenario_file(path)
    typer.echo(f"lab sink: listening target={scenario.traffic.target}")
    result = run_udp_sink(scenario, count=count, timeout_seconds=timeout)
    typer.echo(f"lab sink: stopped packets={result.packets} bytes={result.bytes}")


@app.command("send")
def send(
    path: Path,
    payload: str = typer.Option("gatherlink-lab", help="Payload prefix to send."),
    count: int = typer.Option(5, help="Number of UDP packets to send."),
    interval: float = typer.Option(0.05, help="Seconds between packets."),
    duration: float | None = typer.Option(None, help="Seconds to send traffic for."),
    bandwidth: str | None = typer.Option(None, help="Target bandwidth, for example 7mbit."),
    payload_size: int | None = typer.Option(None, help="UDP payload size for bandwidth tests."),
    direction: str = typer.Option("to-sink", help="Traffic direction: to-sink, from-sink, or both."),
) -> None:
    """Send UDP packets in either lab direction."""
    scenario = load_lab_scenario_file(path)
    if direction == "to-sink":
        result = send_udp_packets(
            scenario,
            payload=payload,
            count=count,
            interval_seconds=interval,
            duration_seconds=duration,
            bandwidth=bandwidth,
            payload_size=payload_size,
            use_namespace=True,
        )
    elif direction == "from-sink":
        result = send_udp_packets_from_sink(
            scenario,
            payload=payload,
            count=count,
            interval_seconds=interval,
            duration_seconds=duration,
            bandwidth=bandwidth,
            payload_size=payload_size,
        )
    elif direction == "both":
        to_sink = send_udp_packets(
            scenario,
            payload=payload,
            count=count,
            interval_seconds=interval,
            duration_seconds=duration,
            bandwidth=bandwidth,
            payload_size=payload_size,
            use_namespace=True,
        )
        from_sink = send_udp_packets_from_sink(
            scenario,
            payload=payload,
            count=count,
            interval_seconds=interval,
            duration_seconds=duration,
            bandwidth=bandwidth,
            payload_size=payload_size,
        )
        result = type(to_sink)(
            target=f"{to_sink.target}<->{from_sink.target}",
            packets=to_sink.packets + from_sink.packets,
            bytes=to_sink.bytes + from_sink.bytes,
        )
    else:
        typer.echo("direction must be 'to-sink', 'from-sink', or 'both'", err=True)
        raise typer.Exit(1)
    typer.echo(f"lab send: direction={direction} target={result.target} packets={result.packets} bytes={result.bytes}")


@app.command("disable-service")
def disable_service(
    path: Path,
    service: str = typer.Option("udp-main", help="Service name or compact service id to disable."),
    side: str = typer.Option("sink", help="Lab node that advertises the disable: sink or source."),
    reason: str = typer.Option("peer declined this service", help="Reason advertised to the peer and logs."),
) -> None:
    """Advertise a generic service-disable control assertion from one lab side."""
    scenario = load_lab_scenario_file(path)
    try:
        result = request_lab_service_disable(scenario, side=side, service=service, reason=reason)
    except RuntimeError as exc:
        typer.echo(f"lab disable-service: failed {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        "lab disable-service: advertised "
        f"side={side} service={service} service_id={result['service_id']} reason={result['reason']}"
    )


@app.command("smoke")
def smoke(
    path: Path,
    payload: str = typer.Option("gatherlink-smoke", help="Payload prefix to send."),
    count: int = typer.Option(3, help="Number of UDP packets to verify."),
    timeout: float = typer.Option(3.0, help="Seconds to wait for forwarded packets."),
) -> None:
    """Verify a running lab service forwards UDP to its target."""
    scenario = load_lab_scenario_file(path)
    result = run_udp_smoke_test(scenario, payload=payload, count=count, timeout_seconds=timeout)
    if result.packets != count:
        typer.echo(f"lab smoke: failed expected={count} received={result.packets} target={result.listen}", err=True)
        raise typer.Exit(1)
    typer.echo(f"lab smoke: ok packets={result.packets} bytes={result.bytes} target={result.listen}")


@app.command("rust-smoke")
def rust_smoke(
    path: Path,
    count: int = typer.Option(3, help="Number of UDP payloads to send through the Rust path transport."),
    payload: str = typer.Option("gatherlink-rust-path", help="Payload prefix to send."),
) -> None:
    """Verify production Rust path transport encapsulates UDP over configured lab paths."""
    scenario = load_lab_scenario_file(path)
    try:
        result = run_rust_transport_smoke(scenario, count=count, payload=payload)
    except RuntimeError as exc:
        typer.echo(f"lab rust smoke: failed {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        "lab rust smoke: ok "
        f"packets={result.packets} bytes={result.bytes} paths={result.paths} "
        f"forwarded={result.forwarded_packets} delivered={result.delivered_packets} "
        f"client_listen={result.client_listen} remote_target={result.remote_target}"
    )


@app.command("shared-sink-smoke")
def shared_sink_smoke(
    path: Path,
    count: int = typer.Option(3, help="Number of UDP payloads to send from each source peer."),
    payload: str = typer.Option("gatherlink-shared-sink", help="Payload prefix to send."),
) -> None:
    """Verify two authenticated source peers can share one sink carrier port."""
    scenario = load_lab_scenario_file(path)
    try:
        result = run_shared_sink_transport_smoke(scenario, count=count, payload=payload)
    except RuntimeError as exc:
        typer.echo(f"lab shared sink smoke: failed {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        "lab shared sink smoke: ok "
        f"sources={result.source_count} packets={result.packets} bytes={result.bytes} "
        f"paths={result.paths} sink_transport={result.sink_transport} remote_target={result.remote_target}"
    )


@app.command("carrier-smoke")
def carrier_smoke(
    carrier: str = typer.Argument(..., help="Carrier mode: quic-datagram or http3-datagram."),
    count: int = typer.Option(3, help="Number of packets to send in each direction."),
    payload: str = typer.Option("gatherlink-carrier-smoke", help="Opaque payload prefix to preserve."),
) -> None:
    """Verify a standard-protocol carrier preserves opaque Gatherlink packet bytes."""
    try:
        result = run_standard_carrier_smoke(carrier, count=count, payload=payload)
    except RuntimeError as exc:
        typer.echo(f"lab carrier smoke: failed {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        "lab carrier smoke: ok "
        f"carrier={result.carrier} packets={result.packets} bytes={result.bytes} "
        f"client_udp={result.client_udp} server_udp={result.server_udp} "
        f"carrier_endpoint={result.carrier_endpoint}"
    )


@app.command("carrier-proxy-smoke")
def carrier_proxy_smoke(
    carrier: str = typer.Argument(..., help="Carrier mode: quic-datagram or http3-datagram."),
    proxy: str = typer.Option("traefik", help="UDP-capable proxy to use. Currently only traefik."),
    count: int = typer.Option(3, help="Number of packets to send in each direction."),
    payload: str = typer.Option("gatherlink-carrier-proxy-smoke", help="Opaque payload prefix to preserve."),
    traefik_bin: str | None = typer.Option(None, help="Path to the traefik binary when it is not on PATH."),
) -> None:
    """Verify a standard-protocol carrier preserves bytes through a real UDP proxy."""
    try:
        result = run_standard_carrier_proxy_smoke(
            carrier,
            proxy=proxy,
            count=count,
            payload=payload,
            traefik_bin=traefik_bin,
        )
    except RuntimeError as exc:
        typer.echo(f"lab carrier proxy smoke: failed {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        "lab carrier proxy smoke: ok "
        f"carrier={result.carrier} proxy={result.proxy} packets={result.packets} bytes={result.bytes} "
        f"client_udp={result.client_udp} server_udp={result.server_udp} "
        f"proxy_endpoint={result.proxy_endpoint} upstream_endpoint={result.upstream_endpoint}"
    )


@app.command("carrier-compare")
def carrier_compare(
    count: int = typer.Option(3, help="Number of packets to send in each direction per row."),
    payload: str = typer.Option("gatherlink-carrier-compare", help="Opaque payload prefix to preserve."),
    include_proxy: bool = typer.Option(False, help="Include Traefik UDP proxy rows when Traefik is available."),
    traefik_bin: str | None = typer.Option(None, help="Path to the traefik binary when it is not on PATH."),
    json_output: bool = typer.Option(False, "--json", help="Print the comparison report as JSON."),
) -> None:
    """Compare UDP, QUIC DATAGRAM, and HTTP/3 DATAGRAM carrier byte-preservation paths."""
    report = run_standard_carrier_comparison(
        count=count,
        payload=payload,
        include_proxy=include_proxy,
        traefik_bin=traefik_bin,
    )
    if json_output:
        typer.echo(json.dumps(report.export_dict(), indent=2, sort_keys=True))
    else:
        status = "ok" if report.ok else "failed"
        typer.echo(f"lab carrier compare: {status} rows={len(report.rows)} count={report.count}")
        for row in report.rows:
            row_status = "ok" if row.ok else "failed"
            typer.echo(
                f"carrier={row.carrier} path={row.path} status={row_status} "
                f"packets={row.packets} bytes={row.bytes} {row.detail}"
            )
    if not report.ok:
        raise typer.Exit(1)


@app.command("down")
def down(path: Path) -> None:
    """Stop the background lab service."""
    scenario = load_lab_scenario_file(path)
    status = stop_lab_service(scenario)
    typer.echo(f"lab service: stopped pid_file={status.pid_file}")


@app.command("cleanup")
def cleanup(
    path: Path, execute: bool = typer.Option(False, "--execute", help="Execute manifest cleanup commands.")
) -> None:
    """Stop a lab scenario or render scoped cleanup for a bundle manifest."""
    if is_lab_bundle_manifest(path):
        cleanup_plan = plan_lab_bundle_cleanup(path, execute=execute)
        for warning in cleanup_plan.warnings:
            typer.echo(f"lab cleanup: warning {warning}")
        for command in cleanup_plan.commands:
            typer.echo(f"lab cleanup: command {command}")
        if execute:
            try:
                results = execute_lab_bundle_cleanup(path)
            except ValueError as exc:
                typer.echo(f"lab cleanup: blocked {exc}", err=True)
                raise typer.Exit(1) from exc
            failed = False
            for result in results:
                status = "ok" if result.returncode == 0 else "failed"
                typer.echo(f"lab cleanup: executed status={status} command {result.command}")
                if result.stderr:
                    typer.echo(result.stderr.strip(), err=True)
                failed = failed or result.returncode != 0
            if failed:
                raise typer.Exit(1)
        return

    scenario = load_lab_scenario_file(path)
    service_status = stop_lab_service(scenario)
    typer.echo(f"lab service: stopped pid_file={service_status.pid_file}")
    try:
        results = cleanup_lab_runtime(scenario)
    except subprocess.CalledProcessError as exc:
        typer.echo(f"failed to cleanup lab network path: {' '.join(exc.cmd)}", err=True)
        if exc.stderr:
            typer.echo(exc.stderr.strip(), err=True)
        raise typer.Exit(1) from exc
    for result in results:
        typer.echo(f"lab cleanup: {result.action} namespace={result.namespace} status={result.status}")


@app.command("profiles")
def profiles(path: Path) -> None:
    """List named live shaping profiles from a lab config."""
    scenario = load_lab_scenario_file(path)
    if not scenario.profiles:
        typer.echo("lab profiles: none")
        return
    for name, profile in scenario.profiles.items():
        typer.echo(f"lab profile: {name} paths={','.join(profile.keys())}")


@app.command("network-modes")
def network_modes(path: Path) -> None:
    """List named network behavior modes from a lab config."""
    scenario = load_lab_scenario_file(path)
    if not scenario.network_modes:
        typer.echo("lab network modes: none")
        return
    for name, mode in scenario.network_modes.items():
        description = f" description={mode.description}" if mode.description else ""
        typer.echo(f"lab network mode: {name} targets={len(mode.targets)}{description}")


@app.command("scheduler-report")
def scheduler_report(
    results_dir: Path = typer.Option(
        Path(".lab/local-three-path/results-fresh"),
        help="Directory containing saved *-sink.json lab status snapshots.",
    ),
    output: Path = typer.Option(
        Path("docs/reports/three-path-scheduler-lab.md"),
        help="Markdown report file to write.",
    ),
) -> None:
    """Generate a scheduler behavior report from saved three-path lab runs."""
    report = write_three_path_scheduler_report(results_dir, output)
    typer.echo(f"lab scheduler report: wrote {output} bytes={len(report.encode('utf-8'))}")


@app.command("apply-network-mode")
def apply_network_mode(path: Path, mode: str) -> None:
    """Apply a named network behavior mode to an existing lab."""
    scenario = load_lab_scenario_file(path)
    try:
        results = apply_lab_network_mode(scenario, mode)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _render_shape_results(results)


@app.command("cycle-network-modes")
def cycle_network_modes(
    path: Path,
    modes: str = typer.Option(..., help="Comma-separated network modes to apply in order."),
    interval: float = typer.Option(10.0, help="Seconds to hold each mode before applying the next."),
    cycles: int = typer.Option(1, help="Number of complete mode cycles to apply."),
) -> None:
    """Cycle named network modes to make bandwidth/latency wander during a test."""
    scenario = load_lab_scenario_file(path)
    mode_names = [mode.strip() for mode in modes.split(",") if mode.strip()]
    if not mode_names:
        typer.echo("at least one network mode is required", err=True)
        raise typer.Exit(1)
    for cycle in range(cycles):
        for mode_name in mode_names:
            typer.echo(f"lab network mode: cycle={cycle + 1}/{cycles} applying={mode_name}")
            try:
                _render_shape_results(apply_lab_network_mode(scenario, mode_name))
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
            if interval > 0:
                time.sleep(interval)


@app.command("apply-profile")
def apply_profile(path: Path, profile: str) -> None:
    """Apply a named live shaping profile to an existing lab."""
    scenario = load_lab_scenario_file(path)
    try:
        results = apply_lab_profile(scenario, profile)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _render_shape_results(results)


@app.command("apply-shape-config")
def apply_shape_config(path: Path, shape_config: Path) -> None:
    """Apply a standalone live shaping config to an existing lab."""
    scenario = load_lab_scenario_file(path)
    profile = load_lab_shape_profile_file(shape_config)
    _render_shape_results(apply_lab_shape_profile(scenario, profile))


@app.command("shape-sink-view")
def shape_sink_view(
    path: Path,
    path_name: str,
    up: str = typer.Option(..., help="Sink-side upload rate, applied to remote/server egress."),
    down: str = typer.Option(..., help="Sink-side download rate, applied to local/client egress."),
) -> None:
    """Apply asymmetric path rates using sink-side up/down semantics."""
    scenario = load_lab_scenario_file(path)
    _render_shape_results(apply_lab_sink_view_rates(scenario, path_name, sink_up_rate=up, sink_down_rate=down))


@app.command("shape")
def shape(
    path: Path,
    path_name: str,
    rate: str | None = typer.Option(None, help="Netem rate limit, for example 10mbit."),
    delay: str | None = typer.Option(None, help="Netem delay, for example 50ms."),
    jitter: str | None = typer.Option(None, help="Delay jitter, for example 10ms."),
    loss: str | None = typer.Option(None, help="Packet loss, for example 2%."),
    reorder: str | None = typer.Option(None, help="Packet reorder, for example 25%."),
    limit: int | None = typer.Option(None, help="Netem queue limit in packets; useful for overload drop tests."),
    mtu: int | None = typer.Option(None, help="Link MTU to apply to both veth ends."),
    state: str | None = typer.Option(None, help="Set path state to up or down."),
    side: str = typer.Option("both", help="Apply to local, remote, or both veth ends."),
    blackhole: bool = typer.Option(False, help="Drop all traffic with netem loss 100%."),
) -> None:
    """Apply ad-hoc live shaping to one existing path."""
    scenario = load_lab_scenario_file(path)
    if state is not None and state not in {"up", "down"}:
        typer.echo("state must be 'up' or 'down'", err=True)
        raise typer.Exit(1)
    if side not in {"local", "remote", "both"}:
        typer.echo("side must be 'local', 'remote', or 'both'", err=True)
        raise typer.Exit(1)
    result = apply_lab_shape(
        scenario,
        path_name,
        LabShapeConfig(
            rate=rate,
            delay=delay,
            jitter=jitter,
            loss=loss,
            reorder=reorder,
            limit=limit,
            mtu=mtu,
            state=state,
            blackhole=blackhole,
        ),
        side=side,
    )
    _render_shape_results([result])


@app.command("clear-shape")
def clear_shape(path: Path, path_name: str, side: str = typer.Option("both")) -> None:
    """Clear live shaping and bring one path back up."""
    if side not in {"local", "remote", "both"}:
        typer.echo("side must be 'local', 'remote', or 'both'", err=True)
        raise typer.Exit(1)
    scenario = load_lab_scenario_file(path)
    _render_shape_results([clear_lab_shape(scenario, path_name, side=side)])


def _render_shape_results(results) -> None:
    for result in results:
        typer.echo(
            f"lab shape: {result.name} side={result.side} actions={','.join(result.actions)} "
            f"client={result.client_namespace}/{result.client_interface} "
            f"server={result.server_namespace}/{result.server_interface}"
        )
