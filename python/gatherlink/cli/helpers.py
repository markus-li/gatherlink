"""Helper service CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus, drain_diagnostics_in_background
from gatherlink.diagnostics.sinks import JsonlDiagnosticSink
from gatherlink.helpers.dns import DnsHelperResolver, DnsResolverPolicy, DnsUdpServer, DnsUpstream
from gatherlink.helpers.dns.policies import DnssecMode, DnsUpstreamKind
from gatherlink.helpers.relay_fabric import discover_relays_from_file
from gatherlink.helpers.socks5 import GatherlinkServiceExitConnector, run_lab_direct_socks5_server, run_socks5_server
from gatherlink.helpers.status_http import StatusHttpConfig, hash_status_http_api_key, run_status_http_server
from gatherlink.helpers.tcp_forward import TcpForwardConfig, run_lab_direct_tcp_forwarder, run_tcp_forwarder
from gatherlink.helpers.traffic_split import TrafficSplitPlan, execute_traffic_split_commands, render_commands
from gatherlink.helpers.udp_stream import GatherlinkUdpStreamTransport, run_gatherlink_udp_stream_exit
from gatherlink.helpers.wireguard import (
    GeneratedWireGuardSetup,
    WireGuardSetupPath,
    WireGuardSetupRequest,
    default_local_paths,
    discover_network_interfaces,
    generate_wireguard_setup,
    parse_setup_path,
    render_peer_endpoint_snippet,
    wireguard_tool_status,
    wireguard_transport_plans,
)

app = typer.Typer(help="Run optional Gatherlink helper services.")
console = Console()


@app.command("dns-serve")
def dns_serve(
    listen: str = typer.Option("127.0.0.1:5353", "--listen", help="Local DNS UDP listen endpoint."),
    upstream: list[str] = typer.Option(
        None,
        "--upstream",
        help="Direct DNS upstream as [name=]host:port[,timeout=seconds]. Can be passed multiple times.",
    ),
    tunnel_upstream: list[str] = typer.Option(
        None,
        "--tunnel-upstream",
        help="Gatherlink-carried DNS upstream as [name=]local-service-host:port[,timeout=seconds].",
    ),
    doh_upstream: list[str] = typer.Option(
        None,
        "--doh-upstream",
        help="DNS-over-HTTPS upstream as [name=]https://host/dns-query or [name=]host[:port][,timeout=seconds].",
    ),
    dnssec_mode: str = typer.Option(
        "allow_unsigned",
        "--dnssec-mode",
        help="DNSSEC policy: off, allow_unsigned, or require_ad.",
    ),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        "--diagnostics-jsonl",
        help="Append structured helper diagnostics events to this JSONL file.",
    ),
) -> None:
    """Run the DNS helper as a local UDP resolver endpoint."""
    listen_host, listen_port = _parse_host_port(listen)
    if dnssec_mode not in {"off", "allow_unsigned", "require_ad"}:
        raise typer.BadParameter("dnssec-mode must be off, allow_unsigned, or require_ad")
    direct_upstreams = upstream or []
    tunnel_upstreams = tunnel_upstream or []
    doh_upstreams = doh_upstream or []
    if not direct_upstreams and not tunnel_upstreams and not doh_upstreams:
        direct_upstreams = ["system=1.1.1.1:53"]
    upstreams = _parse_dns_upstreams(
        direct=direct_upstreams,
        tunnel=tunnel_upstreams,
        doh=doh_upstreams,
    )
    policy = DnsResolverPolicy(upstreams=upstreams, dnssec_mode=cast(DnssecMode, dnssec_mode))
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    typer.echo(
        f"DNS helper listening on {listen_host}:{listen_port}; "
        f"upstreams={','.join(item.authority() for item in upstreams)}"
    )
    try:
        resolver = DnsHelperResolver(policy=policy, diagnostics_bus=diagnostics_bus)
        DnsUdpServer((listen_host, listen_port), resolver).serve_forever()
    finally:
        if diagnostics_bus is not None:
            diagnostics_bus.drain()
        if sink is not None:
            sink.close()


@app.command("socks5-serve")
def socks5_serve(
    listen: str = typer.Option("127.0.0.1:1080", "--listen", help="Local SOCKS5 TCP listen endpoint."),
    allow_host: list[str] = typer.Option(
        None,
        "--allow-host",
        help="Allowed CONNECT target host. Can be passed multiple times.",
    ),
    allow_port: list[int] = typer.Option(
        None,
        "--allow-port",
        help="Allowed CONNECT target port. Can be passed multiple times.",
    ),
    username: str | None = typer.Option(None, "--username", help="Optional SOCKS5 username."),
    password: str | None = typer.Option(None, "--password", help="Optional SOCKS5 password."),
    lab_direct: bool = typer.Option(
        False,
        "--lab-direct",
        help="Use direct TCP instead of Gatherlink service transport; lab smoke only.",
    ),
    gatherlink_service: str | None = typer.Option(
        None,
        "--gatherlink-service",
        help="Local Gatherlink UDP service endpoint host:port that carries helper stream frames to the exit.",
    ),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        "--diagnostics-jsonl",
        help="Append structured helper diagnostics events to this JSONL file.",
    ),
) -> None:
    """Run the SOCKS5 helper as a conservative local TCP CONNECT proxy."""
    listen_host, listen_port = _parse_host_port(listen)
    if not allow_host or not allow_port:
        raise typer.BadParameter("SOCKS5 helper requires at least one --allow-host and one --allow-port")
    if (username is None) != (password is None):
        raise typer.BadParameter("--username and --password must be provided together")
    if not lab_direct and gatherlink_service is None:
        raise typer.BadParameter("SOCKS5 helper requires --gatherlink-service unless --lab-direct is used")
    typer.echo(
        f"SOCKS5 helper listening on {listen_host}:{listen_port}; "
        f"allowed_hosts={','.join(allow_host)} allowed_ports={','.join(str(port) for port in allow_port)}"
    )
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    if lab_direct:
        try:
            with drain_diagnostics_in_background(diagnostics_bus):
                run_lab_direct_socks5_server(
                    listen_host=listen_host,
                    listen_port=listen_port,
                    allowed_hosts=allow_host,
                    allowed_ports=allow_port,
                    auth=(username, password) if username is not None and password is not None else None,
                    diagnostics_bus=diagnostics_bus,
                )
        finally:
            if diagnostics_bus is not None:
                diagnostics_bus.drain()
            if sink is not None:
                sink.close()
        return
    service_host, service_port = _parse_host_port(gatherlink_service)
    exit_connector = GatherlinkServiceExitConnector(GatherlinkUdpStreamTransport(service_host, service_port))
    try:
        with drain_diagnostics_in_background(diagnostics_bus):
            run_socks5_server(
                listen_host=listen_host,
                listen_port=listen_port,
                allowed_hosts=allow_host,
                allowed_ports=allow_port,
                auth=(username, password) if username is not None and password is not None else None,
                exit_connector=exit_connector,
                diagnostics_bus=diagnostics_bus,
            )
    finally:
        if diagnostics_bus is not None:
            diagnostics_bus.drain()
        if sink is not None:
            sink.close()


@app.command("wireguard-plan")
def wireguard_plan(
    config_path: Path = typer.Argument(..., help="Gatherlink config containing a WireGuard helper."),
    peer_public_key: str | None = typer.Option(None, "--peer-public-key", help="Optional key for snippet rendering."),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        "--diagnostics-jsonl",
        help="Append structured WireGuard helper diagnostics events to this JSONL file.",
    ),
) -> None:
    """Show WireGuard-over-Gatherlink service mapping and peer endpoint guidance."""
    runtime_config = expand_config(validate_config_file(config_path))
    plans = wireguard_transport_plans(runtime_config)
    tools = wireguard_tool_status()
    if not plans:
        typer.echo("no WireGuard helper found", err=True)
        raise typer.Exit(1)
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    for plan in plans:
        diagnostics = plan.diagnostics()
        typer.echo(f"service: {diagnostics['service']}")
        typer.echo(f"profile: {diagnostics['profile']} traffic_class: {diagnostics['traffic_class']}")
        typer.echo(f"wireguard local listen: {diagnostics['wireguard_local_listen']}")
        typer.echo(f"wireguard peer endpoint: {diagnostics['wireguard_peer_endpoint']}")
        typer.echo(f"scheduler guidance: {diagnostics['scheduler_guidance']}")
        typer.echo(f"wg tool: {tools['wg'] or 'not found'}")
        typer.echo(f"wg-quick tool: {tools['wg_quick'] or 'not found'}")
        typer.echo(render_peer_endpoint_snippet(plan, peer_public_key=peer_public_key))
        if diagnostics_bus is not None:
            diagnostics_bus.publish(
                DiagnosticEvent.helper_event(
                    code="helper.wireguard.plan",
                    helper="wireguard",
                    message="WireGuard-over-Gatherlink service mapping rendered",
                    service=plan.service,
                    details={"plan": diagnostics, "tools": tools},
                )
            )
    if diagnostics_bus is not None:
        diagnostics_bus.drain()
    if sink is not None:
        sink.close()


@app.command("wireguard-setup")
def wireguard_setup(
    output: Path = typer.Option(
        Path("wireguard-gatherlink-setup"),
        "--output",
        "-o",
        help="Directory for generated Gatherlink and WireGuard files.",
    ),
    model: str = typer.Option(
        "split",
        "--model",
        help="WireGuard profile model: split or single. Split is the default mixed TCP/UDP profile.",
    ),
    path: list[str] = typer.Option(
        None,
        "--path",
        help=(
            "Carrier path as name=iface,client_bind=HOST:PORT,server_bind=HOST:PORT[,mtu=1200][,tx=BPS][,rx=BPS]. "
            "Can be passed multiple times."
        ),
    ),
    path_count: int = typer.Option(2, "--path-count", min=1, help="Number of localhost paths to generate."),
    local_only: bool = typer.Option(False, "--local-only", help="Generate a localhost-only first-run setup."),
    security: str = typer.Option("static", "--security", help="Gatherlink security mode for generated configs: static or none."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Do not prompt; use options/defaults."),
    force: bool = typer.Option(False, "--force", help="Overwrite generated files in --output."),
) -> None:
    """
    Interactive WireGuard-over-Gatherlink setup wizard.

    The wizard renders operator-owned WireGuard config skeletons and
    Gatherlink-owned UDP transport configs. It does not create WireGuard
    interfaces, change routes, or install firewall policy unless the operator
    separately reviews and runs the generated traffic-split plan.
    """
    if non_interactive:
        request = _wireguard_setup_from_options(
            output=output,
            model=model,
            path_values=path or [],
            path_count=path_count,
            local_only=local_only,
            security=security,
        )
    else:
        request, output = _wireguard_setup_interactive(
            default_output=output,
            default_model=model,
            default_path_count=path_count,
            default_security=security,
    )
    setup = generate_wireguard_setup(request)
    written = setup.write(output, force=force)
    _render_wireguard_setup_result(setup, written)


@app.command("traffic-split")
def traffic_split(
    stable_interface: str = typer.Option(..., "--stable-interface", help="WireGuard interface for TCP/default flows."),
    fast_interface: str = typer.Option(
        ..., "--fast-interface", help="WireGuard interface for UDP/high-throughput flows."
    ),
    stable_table: int = typer.Option(51881, "--stable-table", help="Policy routing table for stable/default flows."),
    fast_table: int = typer.Option(51882, "--fast-table", help="Policy routing table for UDP/high-throughput flows."),
    stable_mark: int = typer.Option(0x5181, "--stable-mark", help="Firewall mark for stable/default flows."),
    fast_mark: int = typer.Option(0x5182, "--fast-mark", help="Firewall mark for UDP/high-throughput flows."),
    apply: bool = typer.Option(False, "--apply", help="Apply the generated Debian nft/ip policy-routing rules."),
    revert: bool = typer.Option(False, "--revert", help="Remove the generated Debian nft/ip policy-routing rules."),
) -> None:
    """
    Plan or apply the advanced dual-WireGuard traffic split.

    This is intentionally explicit and noisy: it changes local firewall/routing
    policy and should be reviewed before production use.
    """
    if apply and revert:
        raise typer.BadParameter("--apply and --revert are mutually exclusive")
    plan = TrafficSplitPlan(
        stable_interface=stable_interface,
        fast_interface=fast_interface,
        stable_table=stable_table,
        fast_table=fast_table,
        stable_mark=stable_mark,
        fast_mark=fast_mark,
    )
    typer.echo("ADVANCED: review these local firewall/routing rules before use.", err=True)
    typer.echo("Prefer owning the final split policy in your normal firewall tooling.", err=True)
    commands = plan.revert_commands() if revert else plan.apply_commands()
    if apply or revert:
        if apply:
            # TODO(traffic-split-platforms): Debian/nft has no portable object
            # label for policy routing rules, so Gatherlink owns one named nft
            # table plus deterministic marks/tables. Apply starts by removing
            # those exact objects to keep repeated lab runs clean.
            execute_traffic_split_commands(plan.revert_commands(), check=False)
        execute_traffic_split_commands(commands, check=False)
        typer.echo("traffic split commands executed")
        return
    typer.echo(render_commands(commands))


@app.command("tcp-forward")
def tcp_forward(
    listen: str = typer.Option(..., "--listen", help="Local TCP listen endpoint as host:port."),
    target: str = typer.Option(..., "--target", help="Remote TCP target endpoint as host:port."),
    connect_timeout: float = typer.Option(10.0, "--connect-timeout", help="TCP connect timeout in seconds."),
    idle_timeout: float = typer.Option(300.0, "--idle-timeout", help="Idle connection timeout in seconds."),
    lab_direct: bool = typer.Option(
        False,
        "--lab-direct",
        help="Use direct TCP instead of Gatherlink service transport; lab smoke only.",
    ),
    gatherlink_service: str | None = typer.Option(
        None,
        "--gatherlink-service",
        help="Local Gatherlink UDP service endpoint host:port that carries helper stream frames to the exit.",
    ),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        "--diagnostics-jsonl",
        help="Append structured helper diagnostics events to this JSONL file.",
    ),
) -> None:
    """Run a narrow one-to-one TCP forwarding helper."""
    listen_host, listen_port = _parse_host_port(listen)
    target_host, target_port = _parse_host_port(target)
    typer.echo(f"TCP forward helper listening on {listen}; target={target}")
    config = TcpForwardConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        target_host=target_host,
        target_port=target_port,
        connect_timeout_seconds=connect_timeout,
        idle_timeout_seconds=idle_timeout,
    )
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    if lab_direct:
        try:
            run_lab_direct_tcp_forwarder(config, diagnostics_bus=diagnostics_bus)
        finally:
            if diagnostics_bus is not None:
                diagnostics_bus.drain()
            if sink is not None:
                sink.close()
        return
    if gatherlink_service is None:
        raise typer.BadParameter("TCP forward helper requires --gatherlink-service unless --lab-direct is used")
    service_host, service_port = _parse_host_port(gatherlink_service)
    transport = GatherlinkUdpStreamTransport(service_host, service_port)
    try:
        run_tcp_forwarder(config, transport=transport, diagnostics_bus=diagnostics_bus)
    finally:
        if diagnostics_bus is not None:
            diagnostics_bus.drain()
        if sink is not None:
            sink.close()


@app.command("stream-exit")
def stream_exit(
    listen: str = typer.Option(..., "--listen", help="UDP listen endpoint reached by a Gatherlink service target."),
    allow_host: list[str] = typer.Option(
        None,
        "--allow-host",
        help="Allowed exit target host. Can be passed multiple times.",
    ),
    allow_port: list[int] = typer.Option(
        None,
        "--allow-port",
        help="Allowed exit target port. Can be passed multiple times.",
    ),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        "--diagnostics-jsonl",
        help="Append structured helper diagnostics events to this JSONL file.",
    ),
) -> None:
    """Run the companion UDP stream exit helper for SOCKS5 and TCP forwarding."""
    listen_host, listen_port = _parse_host_port(listen)
    if not allow_host or not allow_port:
        raise typer.BadParameter("stream exit requires at least one --allow-host and one --allow-port")
    typer.echo(
        f"Gatherlink stream exit listening on {listen_host}:{listen_port}; "
        f"allowed_hosts={','.join(allow_host)} allowed_ports={','.join(str(port) for port in allow_port)}"
    )
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    try:
        run_gatherlink_udp_stream_exit(
            listen_host=listen_host,
            listen_port=listen_port,
            allowed_hosts=frozenset(allow_host),
            allowed_ports=frozenset(allow_port),
            diagnostics_bus=diagnostics_bus,
        )
    finally:
        if diagnostics_bus is not None:
            diagnostics_bus.drain()
        if sink is not None:
            sink.close()


@app.command("status-http")
def status_http(
    listen: str = typer.Option("127.0.0.1:8765", "--listen", help="HTTP listen endpoint as host:port."),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="GATHERLINK_STATUS_HTTP_API_KEY",
        help="API key for local REST requests. Can also be set with GATHERLINK_STATUS_HTTP_API_KEY.",
    ),
    api_key_file: Path | None = typer.Option(
        None,
        "--api-key-file",
        help="Read the local REST API key from this file.",
    ),
    api_key_hash: list[str] = typer.Option(
        None,
        "--api-key-hash",
        help="Stored API key hash in sha256:<hex> form. Can be passed multiple times.",
    ),
    allow_non_loopback: bool = typer.Option(
        False,
        "--allow-non-loopback",
        help="DANGER: allow binding the local REST helper outside loopback.",
    ),
    write_window_seconds: int = typer.Option(
        3600,
        "--write-window-seconds",
        help="Seconds before experimental write APIs become read-only.",
    ),
    diagnostics_jsonl: Path | None = typer.Option(
        None,
        "--diagnostics-jsonl",
        help="Append structured helper diagnostics events to this JSONL file.",
    ),
) -> None:
    """Run the local REST/status helper showing Gatherlink services."""
    listen_host, listen_port = _parse_host_port(listen)
    key_hashes = _status_http_key_hashes(api_key=api_key, api_key_file=api_key_file, api_key_hash=api_key_hash or [])
    try:
        config = StatusHttpConfig(
            listen_host=listen_host,
            listen_port=listen_port,
            allow_non_loopback=allow_non_loopback,
            write_window_seconds=write_window_seconds,
            api_key_hashes=key_hashes,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if allow_non_loopback:
        typer.echo(
            "DANGER: experimental status HTTP helper is bound outside loopback. "
            "Do not expose this to untrusted networks.",
            err=True,
        )
    typer.echo(
        f"Gatherlink local REST/status helper listening on http://{listen_host}:{listen_port}; "
        f"write window expires at {config.write_expires_at.isoformat()}"
    )
    sink = JsonlDiagnosticSink(diagnostics_jsonl) if diagnostics_jsonl is not None else None
    diagnostics_bus = DiagnosticsBus(sinks=[sink]) if sink is not None else None
    try:
        run_status_http_server(config, diagnostics_bus=diagnostics_bus)
    finally:
        if diagnostics_bus is not None:
            diagnostics_bus.drain()
        if sink is not None:
            sink.close()


def _status_http_key_hashes(
    *, api_key: str | None, api_key_file: Path | None, api_key_hash: list[str]
) -> tuple[str, ...]:
    """Collect status HTTP key material without exposing plaintext in runtime state."""
    hashes = list(api_key_hash)
    if api_key is not None:
        hashes.append(hash_status_http_api_key(api_key))
    if api_key_file is not None:
        try:
            hashes.append(hash_status_http_api_key(api_key_file.read_text(encoding="utf-8").strip()))
        except OSError as exc:
            raise typer.BadParameter(f"could not read API key file: {exc}") from exc
    if not hashes:
        raise typer.BadParameter(
            "status HTTP requires an API key; pass --api-key, --api-key-file, --api-key-hash, "
            "or set GATHERLINK_STATUS_HTTP_API_KEY"
        )
    return tuple(hashes)


@app.command("relay-discover")
def relay_discover(
    metadata: Path = typer.Argument(..., help="Relay metadata JSON file."),
    required_capability: str | None = typer.Option(
        None,
        "--required-capability",
        help="Capability that candidates must advertise to be compatible.",
    ),
) -> None:
    """Load relay metadata and print candidate health diagnostics."""
    report = discover_relays_from_file(metadata, required_protocol_version=required_capability)
    typer.echo(report.model_dump_json(indent=2))


def _wireguard_setup_from_options(
    *,
    output: Path,
    model: str,
    path_values: list[str],
    path_count: int,
    local_only: bool,
    security: str,
) -> WireGuardSetupRequest:
    """Build a setup request from CLI options without prompting."""
    del output
    setup_model = _wireguard_setup_model(model)
    setup_security = _wireguard_setup_security(security)
    if path_values:
        paths = [_parse_setup_path_for_cli(value) for value in path_values]
    elif local_only:
        paths = default_local_paths(path_count)
    else:
        raise typer.BadParameter("--path is required unless --local-only is used in --non-interactive mode")
    return WireGuardSetupRequest(model=setup_model, paths=paths, security=setup_security, local_only=local_only)


def _wireguard_setup_interactive(
    *,
    default_output: Path,
    default_model: str,
    default_path_count: int,
    default_security: str,
) -> tuple[WireGuardSetupRequest, Path]:
    """Collect setup choices through a small installer-style shell wizard."""
    console.print(
        Panel.fit(
            (
                "[bold]Gatherlink WireGuard setup wizard[/bold]\n"
                "This writes configs only. It does not modify WireGuard interfaces, routes, or firewall policy."
            ),
            title="WireGuard over Gatherlink",
            border_style="cyan",
        )
    )
    console.print(
        "[dim]Defaults favor split WireGuard: one stable/TCP profile and one fast/UDP profile.[/dim]\n"
    )
    model = _wireguard_setup_model(
        typer.prompt("WireGuard model [split/single]", default=_wireguard_setup_model(default_model))
    )
    local_only = typer.confirm("Is this a localhost-only lab setup?", default=True)
    security = _wireguard_setup_security(typer.prompt("Gatherlink security [static/none]", default=default_security))
    path_count = int(typer.prompt("How many Gatherlink paths?", default=str(default_path_count)))
    if local_only:
        paths = default_local_paths(path_count)
    else:
        paths = _prompt_wireguard_paths(path_count)
    output = Path(typer.prompt("Output directory", default=str(default_output)))
    return WireGuardSetupRequest(model=model, paths=paths, security=security, local_only=local_only), output


def _prompt_wireguard_paths(path_count: int) -> list[WireGuardSetupPath]:
    """Ask which Debian interfaces are WAN paths and collect path endpoints."""
    interfaces = discover_network_interfaces()
    if interfaces:
        _render_interface_choices(interfaces)
        console.print("[bold]Mark each interface[/bold] as [cyan]wan[/cyan], [green]lan[/green], management, or ignore.")
        selected: list[str] = []
        for item in interfaces:
            role = _wireguard_interface_role(typer.prompt(f"Role for {item.name}", default="ignore"))
            if role == "wan":
                selected.append(item.name)
        if not selected:
            console.print("[yellow]No WAN interfaces selected; falling back to manual names.[/yellow]", stderr=True)
            selected = [f"eth{index}" for index in range(1, path_count + 1)]
    else:
        console.print("[yellow]No interfaces discovered; using manual interface names.[/yellow]", stderr=True)
        selected = [f"eth{index}" for index in range(1, path_count + 1)]
    selected = selected[:path_count]
    while len(selected) < path_count:
        selected.append(typer.prompt(f"Interface for path {len(selected) + 1}", default=f"eth{len(selected) + 1}"))
    paths = []
    for index, interface in enumerate(selected, start=1):
        name = f"path-{chr(ord('a') + index - 1)}"
        client_bind = typer.prompt(f"{name} client bind host:port", default=f"0.0.0.0:{56000 + index}")
        server_bind = typer.prompt(f"{name} server bind host:port", default=f"0.0.0.0:{57000 + index}")
        client_remote = typer.prompt(f"{name} client remote host:port", default=server_bind)
        server_remote = typer.prompt(f"{name} server remote host:port", default=client_bind)
        paths.append(
            WireGuardSetupPath(
                name=name,
                interface=interface,
                client_bind=client_bind,
                server_bind=server_bind,
                client_remote=client_remote,
                server_remote=server_remote,
            )
        )
    return paths


def _render_interface_choices(interfaces: list[object]) -> None:
    """Render discovered interfaces as a compact Rich table before prompting."""
    table = Table(title="Detected interfaces", box=None, show_edge=False)
    table.add_column("interface", style="cyan", no_wrap=True)
    table.add_column("wizard role")
    for item in interfaces:
        table.add_row(getattr(item, "name", str(item)), "wan / lan / management / ignore")
    console.print(table)


def _render_wireguard_setup_result(setup: GeneratedWireGuardSetup, written: list[Path]) -> None:
    """Render generated setup facts with Rich while keeping output testable."""
    console.print(
        Panel.fit(
            f"[bold green]WireGuard-over-Gatherlink setup generated[/bold green]\n"
            f"model: [bold]{setup.request.model}[/bold]\n"
            f"paths: [bold]{len(setup.request.normalized_paths())}[/bold]",
            title="Complete",
            border_style="green",
        )
    )
    file_table = Table(title="Generated files")
    file_table.add_column("file", style="cyan")
    file_table.add_column("written path", overflow="fold")
    written_by_name = {path.name: path for path in written}
    for name in sorted(setup.files):
        path = written_by_name.get(Path(name).name)
        file_table.add_row(name, str(path) if path is not None else "-")
    console.print(file_table)

    warning_table = Table(title="Warnings", show_header=False)
    warning_table.add_column("warning", style="yellow")
    for warning in setup.warnings:
        warning_table.add_row(warning)
    console.print(warning_table)

    next_table = Table(title="Next actions")
    next_table.add_column("#", justify="right", style="green", no_wrap=True)
    next_table.add_column("command or action")
    for index, step in enumerate(setup.next_steps, start=1):
        next_table.add_row(str(index), step)
    console.print(next_table)


def _wireguard_setup_model(value: str) -> str:
    """Validate setup model text for Typer callbacks and prompts."""
    normalized = value.strip().lower()
    if normalized not in {"split", "single"}:
        raise typer.BadParameter("model must be split or single")
    return normalized


def _wireguard_setup_security(value: str) -> str:
    """Validate setup security text for Typer callbacks and prompts."""
    normalized = value.strip().lower()
    if normalized not in {"static", "none"}:
        raise typer.BadParameter("security must be static or none")
    return normalized


def _wireguard_interface_role(value: str) -> str:
    """Validate interface role text for the interactive setup wizard."""
    normalized = value.strip().lower()
    if normalized not in {"wan", "lan", "management", "ignore"}:
        raise typer.BadParameter("interface role must be wan, lan, management, or ignore")
    return normalized


def _parse_setup_path_for_cli(value: str) -> WireGuardSetupPath:
    """Parse setup path values and convert model errors to Typer errors."""
    try:
        return parse_setup_path(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _parse_host_port(value: str) -> tuple[str, int]:
    """Parse host:port or [ipv6]:port without pulling in bootstrap policy types."""
    text = value.strip()
    if text.startswith("["):
        host, separator, port_text = text[1:].partition("]:")
    else:
        host, separator, port_text = text.rpartition(":")
    if not separator or not host or not port_text:
        raise typer.BadParameter("expected host:port or [ipv6]:port")
    return host, int(port_text)


def _parse_dns_upstreams(*, direct: list[str], tunnel: list[str], doh: list[str]) -> list[DnsUpstream]:
    """Parse DNS helper upstream CLI values into explicit policy objects."""
    parsed: list[DnsUpstream] = []
    for kind, values in (("direct", direct), ("tunnel", tunnel), ("doh", doh)):
        for value in values:
            parsed.append(_parse_dns_upstream(value, kind=kind, index=len(parsed) + 1))
    return parsed


def _parse_dns_upstream(value: str, *, kind: str, index: int) -> DnsUpstream:
    """Parse one DNS upstream value while keeping kind explicit for diagnostics."""
    name = f"{kind}-{index}"
    endpoint = value
    timeout_seconds = 1.0
    if "=" in value and value.split("=", 1)[0] and ":" not in value.split("=", 1)[0]:
        name, endpoint = value.split("=", 1)
    if "," in endpoint:
        endpoint, *options = endpoint.split(",")
        for option in options:
            option_name, separator, option_value = option.partition("=")
            if separator and option_name == "timeout":
                timeout_seconds = float(option_value)
    if kind == "doh":
        host, port = _parse_doh_cli_endpoint(endpoint)
    else:
        host, port = _parse_host_port(endpoint)
    return DnsUpstream(
        name=name,
        address=host,
        port=port,
        kind=cast(DnsUpstreamKind, kind),
        timeout_seconds=timeout_seconds,
    )


def _parse_doh_cli_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse DoH CLI endpoints while preserving HTTPS URL paths for the resolver."""
    if endpoint.startswith(("https://", "http://")):
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise typer.BadParameter("DoH upstream URL must use https://host[/path]")
        return endpoint, parsed.port or 443
    if ":" in endpoint:
        return _parse_host_port(endpoint)
    return endpoint, 443
