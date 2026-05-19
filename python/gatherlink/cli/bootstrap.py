"""Bootstrap discovery CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from gatherlink.bootstrap.cache import BootstrapCache, BootstrapEndpoint
from gatherlink.bootstrap.connector import (
    BootstrapChallenge,
    create_bootstrap_challenge,
    probe_candidate,
    sign_bootstrap_challenge,
)
from gatherlink.bootstrap.resolver import resolve_bootstrap
from gatherlink.secrets.bundles import SignedDocument
from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord

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
    peer_identity: Path | None = typer.Option(
        None,
        "--peer-identity",
        help="Expected peer public identity JSON for authenticated proof verification.",
    ),
    proof: Path | None = typer.Option(None, "--proof", help="Signed bootstrap proof JSON."),
    cache_peer: str | None = typer.Option(None, "--cache-peer", help="Peer name to cache after authenticated proof."),
    cache: Path | None = typer.Option(None, "--cache", help="Bootstrap cache to update after authenticated proof."),
    allow_insecure: bool = typer.Option(
        False,
        "--allow-insecure",
        help="Accept an unauthenticated local-lab candidate without caching it as verified.",
    ),
) -> None:
    """Validate one bootstrap candidate."""
    parsed_endpoint = BootstrapEndpoint.parse(endpoint)
    result = probe_candidate(
        parsed_endpoint,
        allow_insecure=allow_insecure,
        expected_peer=_load_public_identity(peer_identity) if peer_identity else None,
        proof=_load_signed_document(proof) if proof else None,
    )
    if result.authenticated and cache and cache_peer:
        bootstrap_cache = BootstrapCache.load(cache)
        bootstrap_cache.put(cache_peer, [result.endpoint])
        bootstrap_cache.save(cache)
    typer.echo(json.dumps(result.export_dict(), indent=2, sort_keys=True))
    if not result.reachable:
        raise typer.Exit(1)


@app.command("challenge")
def challenge(endpoint: str) -> None:
    """Create a signed-bootstrap challenge for a candidate endpoint."""
    created = create_bootstrap_challenge(BootstrapEndpoint.parse(endpoint))
    typer.echo(json.dumps(created.export_dict(), indent=2, sort_keys=True))


@app.command("proof")
def proof(identity: Path, challenge_path: Path) -> None:
    """Sign a bootstrap challenge with a local private identity."""
    identity_record = IdentityRecord.from_dict(json.loads(identity.read_text(encoding="utf-8")))
    challenge_doc = BootstrapChallenge.model_validate_json(challenge_path.read_text(encoding="utf-8"))
    signed = sign_bootstrap_challenge(identity_record.to_identity(), challenge_doc)
    typer.echo(json.dumps(signed.export_dict(), indent=2, sort_keys=True))


def _load_public_identity(path: Path) -> IdentityPublicRecord:
    """Load a public or private identity file as public bootstrap identity."""
    return IdentityPublicRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _load_signed_document(path: Path) -> SignedDocument:
    """Load a signed document from JSON."""
    return SignedDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))
