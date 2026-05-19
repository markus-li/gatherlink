"""Helper service CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import typer

from gatherlink.config.expansion import expand_config
from gatherlink.config.validation import validate_config_file
from gatherlink.diagnostics import DiagnosticsBus
from gatherlink.diagnostics.sinks import JsonlDiagnosticSink
from gatherlink.helpers.dns import DnsHelperResolver, DnsResolverPolicy, DnsUdpServer, DnsUpstream
from gatherlink.helpers.dns.policies import DnssecMode
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
        help="Direct DNS upstream as host:port. Can be passed multiple times.",
    ),
    dnssec_mode: str = typer.Option(
        "allow_unsigned",
        "--dnssec-mode",
        help="DNSSEC policy: off, allow_unsigned, or require_ad.",
    ),
) -> None:
    """Run the DNS helper as a local UDP resolver endpoint."""
    listen_host, listen_port = _parse_host_port(listen)
    if dnssec_mode not in {"off", "allow_unsigned", "require_ad"}:
        raise typer.BadParameter("dnssec-mode must be off, allow_unsigned, or require_ad")
    upstreams = [
        DnsUpstream(name=f"upstream-{index + 1}", address=host, port=port)
        for index, (host, port) in enumerate(_parse_host_port(value) for value in (upstream or ["1.1.1.1:53"]))
    ]
    policy = DnsResolverPolicy(upstreams=upstreams, dnssec_mode=cast(DnssecMode, dnssec_mode))
    typer.echo(
        f"DNS helper listening on {listen_host}:{listen_port}; "
        f"upstreams={','.join(item.authority() for item in upstreams)}"
    )
    DnsUdpServer((listen_host, listen_port), DnsHelperResolver(policy=policy)).serve_forever()


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
) -> None:
    """Run the SOCKS5 helper as a conservative local TCP CONNECT proxy."""
    listen_host, listen_port = _parse_host_port(listen)
    if not allow_host or not allow_port:
        raise typer.BadParameter("SOCKS5 helper requires at least one --allow-host and one --allow-port")
    if (username is None) != (password is None):
        raise typer.BadParameter("--username and --password must be provided together")
    typer.echo(
        f"SOCKS5 helper listening on {listen_host}:{listen_port}; "
        f"allowed_hosts={','.join(allow_host)} allowed_ports={','.join(str(port) for port in allow_port)}"
    )
    if lab_direct:
        run_lab_direct_socks5_server(
            listen_host=listen_host,
            listen_port=listen_port,
            allowed_hosts=allow_host,
            allowed_ports=allow_port,
            auth=(username, password) if username is not None and password is not None else None,
        )
        return
    exit_connector = None
    if gatherlink_service is not None:
        service_host, service_port = _parse_host_port(gatherlink_service)
        exit_connector = GatherlinkServiceExitConnector(GatherlinkUdpStreamTransport(service_host, service_port))
    run_socks5_server(
        listen_host=listen_host,
        listen_port=listen_port,
        allowed_hosts=allow_host,
        allowed_ports=allow_port,
        auth=(username, password) if username is not None and password is not None else None,
        exit_connector=exit_connector,
    )


@app.command("wireguard-plan")
def wireguard_plan(
    config_path: Path = typer.Argument(..., help="Gatherlink config containing a WireGuard helper."),
    peer_public_key: str | None = typer.Option(None, "--peer-public-key", help="Optional key for snippet rendering."),
) -> None:
    """Show WireGuard-over-Gatherlink service mapping and peer endpoint guidance."""
    runtime_config = expand_config(validate_config_file(config_path))
    plans = wireguard_transport_plans(runtime_config)
    tools = wireguard_tool_status()
    if not plans:
        typer.echo("no WireGuard helper found", err=True)
        raise typer.Exit(1)
    for plan in plans:
        diagnostics = plan.diagnostics()
        typer.echo(f"service: {diagnostics['service']}")
        typer.echo(f"wireguard local listen: {diagnostics['wireguard_local_listen']}")
        typer.echo(f"wireguard peer endpoint: {diagnostics['wireguard_peer_endpoint']}")
        typer.echo(f"wg tool: {tools['wg'] or 'not found'}")
        typer.echo(f"wg-quick tool: {tools['wg_quick'] or 'not found'}")
        typer.echo(render_peer_endpoint_snippet(plan, peer_public_key=peer_public_key))


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
    if lab_direct:
        run_lab_direct_tcp_forwarder(config)
        return
    if gatherlink_service is not None:
        service_host, service_port = _parse_host_port(gatherlink_service)
        run_tcp_forwarder(config, transport=GatherlinkUdpStreamTransport(service_host, service_port))
        return
    run_tcp_forwarder(config)


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
) -> None:
    """Run a small local HTTP helper showing Gatherlink services on this machine."""
    listen_host, listen_port = _parse_host_port(listen)
    typer.echo(f"Gatherlink status HTTP helper listening on http://{listen_host}:{listen_port}")
    run_status_http_server(StatusHttpConfig(listen_host=listen_host, listen_port=listen_port))


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
