"""Identity and static transport-key provisioning commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.sessions import derive_static_transport_security

app = typer.Typer(help="Manage Gatherlink identity and lab/manual transport secrets.")


@app.command("identity-create")
def identity_create(path: Path, force: bool = typer.Option(False, "--force", help="Overwrite an existing identity.")) -> None:
    """Create a new local node identity file."""
    if path.exists() and not force:
        raise typer.BadParameter(f"{path} already exists; pass --force to overwrite")
    identity = NodeIdentity.generate()
    record = IdentityRecord.from_identity(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.export_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    typer.echo(json.dumps(IdentityPublicRecord.from_identity(identity).export_dict(), indent=2, sort_keys=True))


@app.command("identity-public")
def identity_public(path: Path) -> None:
    """Print the public identity record for a private identity file."""
    record = _load_identity_record(path)
    public = IdentityPublicRecord.from_identity_record(record)
    typer.echo(json.dumps(public.export_dict(), indent=2, sort_keys=True))


@app.command("static-session")
def static_session(
    local_identity: Path = typer.Option(..., "--local", help="Local private identity JSON."),
    peer_identity: Path = typer.Option(..., "--peer", help="Peer public or private identity JSON."),
    role: str = typer.Option(..., "--role", help="Local role in this session: initiator or responder."),
    receiver_index: int = typer.Option(1, "--receiver-index", help="Receiver index compiled into Rust."),
    context: str = typer.Option("", "--context", help="Optional ASCII session context such as config generation."),
) -> None:
    """Derive a config-compatible static AEAD security block from identities."""
    if role not in {"initiator", "responder"}:
        raise typer.BadParameter("role must be initiator or responder")
    material = derive_static_transport_security(
        _load_identity_record(local_identity).to_identity(),
        _load_public_identity_record(peer_identity),
        role=role,
        receiver_index=receiver_index,
        context=context.encode("ascii"),
    )
    typer.echo(json.dumps(material.export_config(), indent=2, sort_keys=True))


def _load_identity_record(path: Path) -> IdentityRecord:
    """Load a private identity record from disk."""
    return IdentityRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _load_public_identity_record(path: Path) -> IdentityPublicRecord:
    """Load either a public or private identity record as public peer material."""
    return IdentityPublicRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

