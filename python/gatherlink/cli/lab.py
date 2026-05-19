"""Lab scenario CLI commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import typer

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


@app.command("up")
def up(path: Path) -> None:
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
        service_result = start_lab_service(path, scenario)
        sink_result = start_lab_sink_service(path, scenario)
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
        f"pid_file={sink_result.pid_file} log={sink_result.log_file}"
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


@app.command("down")
def down(path: Path) -> None:
    """Stop the background lab service."""
    scenario = load_lab_scenario_file(path)
    status = stop_lab_service(scenario)
    typer.echo(f"lab service: stopped pid_file={status.pid_file}")


@app.command("cleanup")
def cleanup(path: Path) -> None:
    """Stop the lab service and remove lab-owned virtual interfaces."""
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
