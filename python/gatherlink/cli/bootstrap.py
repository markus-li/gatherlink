"""Bootstrap discovery CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from gatherlink.bootstrap.cache import BootstrapCache, BootstrapEndpoint
from gatherlink.bootstrap.connector import probe_candidate
from gatherlink.bootstrap.resolver import resolve_bootstrap

app = typer.Typer(help="Resolve and validate Gatherlink bootstrap endpoints.")


@app.command("resolve")
def resolve(
    peer: str,
    static: list[str] = typer.Option(
        None,
        "--static",
        help="Static candidate endpoint as host:port or [ipv6]:port. Can be passed multiple times.",
    ),
    cache: Path | None = typer.Option(None, "--cache", help="Optional bootstrap cache JSON file."),
) -> None:
    """Resolve candidate endpoints for one peer."""
    resolution = resolve_bootstrap(peer, static_endpoints=static or [], cache_path=cache)
    typer.echo(json.dumps(resolution.export_dict(), indent=2, sort_keys=True))


@app.command("cache-put")
def cache_put(
    peer: str,
    endpoint: list[str] = typer.Option(..., "--endpoint", "-e", help="Endpoint to cache as host:port."),
    cache: Path = typer.Option(..., "--cache", help="Bootstrap cache JSON file."),
) -> None:
    """Write static endpoints into the local bootstrap cache."""
    bootstrap_cache = BootstrapCache.load(cache)
    bootstrap_cache.put(peer, [BootstrapEndpoint.parse(value, source="cache") for value in endpoint])
    bootstrap_cache.save(cache)
    typer.echo(f"cached {len(endpoint)} endpoint(s) for {peer} in {cache}")


@app.command("probe")
def probe(
    endpoint: str,
    allow_insecure: bool = typer.Option(
        False,
        "--allow-insecure",
        help="Accept the current plaintext lab probe while authenticated bootstrap is not implemented.",
    ),
) -> None:
    """Validate one bootstrap candidate."""
    result = probe_candidate(BootstrapEndpoint.parse(endpoint), allow_insecure=allow_insecure)
    typer.echo(json.dumps(result.export_dict(), indent=2, sort_keys=True))
    if not result.reachable:
        raise typer.Exit(1)
