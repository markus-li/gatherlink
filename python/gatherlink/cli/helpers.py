"""Helper service CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import typer

from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.diagnostics.sinks import JsonlDiagnosticSink
from gatherlink.helpers.dns import DnsHelperResolver, DnsResolverPolicy, DnsUdpServer, DnsUpstream
from gatherlink.helpers.dns.policies import DnssecMode, DnsUpstreamKind
from gatherlink.helpers.relay_fabric import discover_relays_from_file
from gatherlink.helpers.socks5 import GatherlinkServiceExitConnector, run_lab_direct_socks5_server, run_socks5_server
from gatherlink.helpers.status_http import StatusHttpConfig, run_status_http_server
from gatherlink.helpers.tcp_forward import TcpForwardConfig, run_lab_direct_tcp_forwarder, run_tcp_forwarder
from gatherlink.helpers.udp_stream import GatherlinkUdpStreamTransport, run_gatherlink_udp_stream_exit
from gatherlink.helpers.wireguard import render_peer_endpoint_snippet, wireguard_tool_status, wireguard_transport_plans

app = typer.Typer(help="Run optional Gatherlink helper services.")


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
        typer.echo(f"wireguard local listen: {diagnostics['wireguard_local_listen']}")
        typer.echo(f"wireguard peer endpoint: {diagnostics['wireguard_peer_endpoint']}")
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
    allow_non_loopback: bool = typer.Option(
        False,
        "--allow-non-loopback",
        help="DANGER: allow binding the experimental helper outside loopback.",
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
    """Run the EXPERIMENTAL local HTTP helper showing Gatherlink services."""
    listen_host, listen_port = _parse_host_port(listen)
    try:
        config = StatusHttpConfig(
            listen_host=listen_host,
            listen_port=listen_port,
            allow_non_loopback=allow_non_loopback,
            write_window_seconds=write_window_seconds,
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
        f"Gatherlink EXPERIMENTAL status HTTP helper listening on http://{listen_host}:{listen_port}; "
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
