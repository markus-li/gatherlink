"""Identity and static transport-key provisioning commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from gatherlink.persistence.audit import StateAuditReport, audit_persistent_state
from gatherlink.persistence.sealed import SealedSecretEnvelope, open_secret_json, seal_secret_json
from gatherlink.persistence.store import GatherlinkStatePaths, atomic_write_json, load_secret_json, redact_secrets
from gatherlink.secrets.bundles import SignedDocument
from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord
from gatherlink.secrets.provisioning import (
    ProvisionedNode,
    ProvisionedService,
    TopologyBundleBody,
    diff_topology_bundles,
    load_verified_topology_bundle,
    sign_topology_bundle,
)
from gatherlink.security.handshake import (
    PendingHandshakeInitiation,
    accept_handshake_initiation,
    complete_handshake_initiator,
    create_handshake_initiation,
)
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.noise import (
    PendingNoiseInitiation,
    accept_noise_ik_initiation,
    complete_noise_ik_initiator,
    create_noise_ik_initiation,
)
from gatherlink.security.sessions import derive_static_transport_security

app = typer.Typer(help="Manage Gatherlink identity and lab/manual transport secrets.")
DEFAULT_PASSPHRASE_ENV = "GATHERLINK_SECRET_PASSPHRASE"


@app.command("identity-create")
def identity_create(
    path: Path, force: bool = typer.Option(False, "--force", help="Overwrite an existing identity.")
) -> None:
    """Create a new local node identity file."""
    identity = NodeIdentity.generate()
    record = IdentityRecord.from_identity(identity)
    try:
        record.save(path, force=force)
    except FileExistsError as exc:
        raise typer.BadParameter(f"{path} already exists; pass --force to overwrite") from exc
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


@app.command("topology-create")
def topology_create(
    issuer_identity: Path = typer.Option(..., "--issuer", help="Trust-root/private issuer identity JSON."),
    output: Path = typer.Option(..., "--output", help="Signed topology bundle output path."),
    generation: int = typer.Option(..., "--generation", help="Monotonic topology generation."),
    node: list[str] = typer.Option(
        [],
        "--node",
        help="Provisioned node as name=identity.json. May be repeated.",
    ),
    service: list[str] = typer.Option(
        [],
        "--service",
        help="Provisioned service as name=owner_node=service_id. May be repeated.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing signed bundle."),
) -> None:
    """Create a signed topology/provisioning bundle."""
    issuer_record = _load_identity_record(issuer_identity)
    issuer = issuer_record.to_identity()
    body = TopologyBundleBody(
        generation=generation,
        issuer_node_id=IdentityPublicRecord.from_identity_record(issuer_record).node_id,
        nodes=[_parse_provisioned_node(value) for value in node],
        services=[_parse_provisioned_service(value) for value in service],
    )
    document = sign_topology_bundle(issuer, body)
    try:
        document.save(output, force=force)
    except FileExistsError as exc:
        raise typer.BadParameter(f"{output} already exists; pass --force to overwrite") from exc
    typer.echo(
        json.dumps({"status": "signed", "path": str(output), "generation": generation}, indent=2, sort_keys=True)
    )


@app.command("topology-verify")
def topology_verify(
    bundle: Path,
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
    minimum_generation: int = typer.Option(1, "--minimum-generation", help="Reject older topology generations."),
) -> None:
    """Verify a signed topology/provisioning bundle and print public facts."""
    document = SignedDocument.load(bundle)
    body = load_verified_topology_bundle(
        document,
        trusted_issuer=_load_public_identity_record(trust_root),
        minimum_generation=minimum_generation,
    )
    typer.echo(json.dumps(body.model_dump(mode="json"), indent=2, sort_keys=True))


@app.command("topology-diff")
def topology_diff(
    current_bundle: Path = typer.Argument(..., help="Currently installed signed topology bundle JSON."),
    candidate_bundle: Path = typer.Argument(..., help="Candidate signed topology bundle JSON."),
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
) -> None:
    """Explain topology changes before installing a candidate bundle."""
    trusted = _load_public_identity_record(trust_root)
    current = load_verified_topology_bundle(SignedDocument.load(current_bundle), trusted_issuer=trusted)
    candidate = load_verified_topology_bundle(SignedDocument.load(candidate_bundle), trusted_issuer=trusted)
    diff = diff_topology_bundles(current, candidate)
    typer.echo(json.dumps(diff.export_dict(), indent=2, sort_keys=True))
    if not diff.ok_to_install:
        raise typer.Exit(1)


@app.command("handshake-init")
def handshake_init(
    local_identity: Path = typer.Option(..., "--local", help="Local private identity JSON."),
    peer_identity: Path = typer.Option(..., "--peer", help="Peer public or private identity JSON."),
    topology_bundle: Path = typer.Option(..., "--topology", help="Signed topology bundle JSON."),
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
    initiation_output: Path = typer.Option(..., "--initiation-output", help="Public signed initiation output."),
    pending_output: Path = typer.Option(..., "--pending-output", help="Secret pending initiator state output."),
    receiver_index: int | None = typer.Option(
        None,
        "--receiver-index",
        help="Local receiver index for inbound packets. Defaults to a generated opaque index.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing output files."),
) -> None:
    """Create a signed authenticated-session initiation and local pending state."""
    pending = create_handshake_initiation(
        _load_identity_record(local_identity).to_identity(),
        _load_public_identity_record(peer_identity),
        _load_verified_topology(topology_bundle, trust_root),
        receiver_index=receiver_index,
    )
    pending.document.save(initiation_output, force=force)
    _write_json_output(pending_output, pending.export_secret_dict(), force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {
                "status": "created",
                "initiation": str(initiation_output),
                "pending": str(pending_output),
                "expires_at": pending.expires_at.isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("noise-init")
def noise_init(
    local_identity: Path = typer.Option(..., "--local", help="Local private identity JSON."),
    peer_identity: Path = typer.Option(..., "--peer", help="Peer public or private identity JSON."),
    topology_bundle: Path = typer.Option(..., "--topology", help="Signed topology bundle JSON."),
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
    initiation_output: Path = typer.Option(..., "--initiation-output", help="Public Noise IK initiation output."),
    pending_output: Path = typer.Option(..., "--pending-output", help="Secret pending initiator state output."),
    receiver_index: int | None = typer.Option(
        None,
        "--receiver-index",
        help="Local receiver index for inbound packets. Defaults to a generated opaque index.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing output files."),
) -> None:
    """Create a Noise IK authenticated-session initiation and local pending state."""
    pending = create_noise_ik_initiation(
        _load_identity_record(local_identity).to_identity(),
        _load_public_identity_record(peer_identity),
        _load_verified_topology(topology_bundle, trust_root),
        receiver_index=receiver_index,
    )
    _write_json_output(initiation_output, pending.message, force=force, mode=0o644)
    _write_json_output(pending_output, pending.export_secret_dict(), force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {
                "status": "created",
                "protocol": "noise-ik",
                "initiation": str(initiation_output),
                "pending": str(pending_output),
                "expires_at": pending.expires_at.isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("noise-accept")
def noise_accept(
    local_identity: Path = typer.Option(..., "--local", help="Local responder private identity JSON."),
    topology_bundle: Path = typer.Option(..., "--topology", help="Signed topology bundle JSON."),
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
    initiation: Path = typer.Option(..., "--initiation", help="Noise IK initiation JSON from the peer."),
    response_output: Path = typer.Option(..., "--response-output", help="Public Noise IK response output."),
    security_output: Path = typer.Option(..., "--security-output", help="Secret config-compatible security block."),
    receiver_index: int | None = typer.Option(
        None,
        "--receiver-index",
        help="Local receiver index for inbound packets. Defaults to a generated opaque index.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing output files."),
) -> None:
    """Accept a Noise IK initiation and write responder security material."""
    accepted = accept_noise_ik_initiation(
        _load_identity_record(local_identity).to_identity(),
        _load_json_object(initiation),
        _load_verified_topology(topology_bundle, trust_root),
        receiver_index=receiver_index,
    )
    _write_json_output(response_output, accepted.response, force=force, mode=0o644)
    if accepted.session.security is None:
        raise typer.BadParameter("accepted Noise handshake did not compile security material")
    _write_json_output(security_output, accepted.session.export_config(), force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {
                "status": "accepted",
                "protocol": "noise-ik",
                "response": str(response_output),
                "security": str(security_output),
                "session": accepted.session.export_public_summary(),
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("noise-complete")
def noise_complete(
    local_identity: Path = typer.Option(..., "--local", help="Local initiator private identity JSON."),
    topology_bundle: Path = typer.Option(..., "--topology", help="Signed topology bundle JSON."),
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
    pending: Path = typer.Option(..., "--pending", help="Secret pending initiator state JSON."),
    response: Path = typer.Option(..., "--response", help="Noise IK response JSON from the peer."),
    security_output: Path = typer.Option(..., "--security-output", help="Secret config-compatible security block."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing security output file."),
) -> None:
    """Complete a Noise IK authenticated-session handshake and write initiator security material."""
    session = complete_noise_ik_initiator(
        _load_identity_record(local_identity).to_identity(),
        _load_pending_noise(pending),
        _load_json_object(response),
        _load_verified_topology(topology_bundle, trust_root),
    )
    if session.security is None:
        raise typer.BadParameter("completed Noise handshake did not compile security material")
    _write_json_output(security_output, session.export_config(), force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {
                "status": "completed",
                "protocol": "noise-ik",
                "security": str(security_output),
                "session": session.export_public_summary(),
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("handshake-accept")
def handshake_accept(
    local_identity: Path = typer.Option(..., "--local", help="Local responder private identity JSON."),
    topology_bundle: Path = typer.Option(..., "--topology", help="Signed topology bundle JSON."),
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
    initiation: Path = typer.Option(..., "--initiation", help="Signed initiation JSON from the peer."),
    response_output: Path = typer.Option(..., "--response-output", help="Public signed response output."),
    security_output: Path = typer.Option(..., "--security-output", help="Secret config-compatible security block."),
    receiver_index: int | None = typer.Option(
        None,
        "--receiver-index",
        help="Local receiver index for inbound packets. Defaults to a generated opaque index.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing output files."),
) -> None:
    """Accept a signed initiation and write responder security material."""
    accepted = accept_handshake_initiation(
        _load_identity_record(local_identity).to_identity(),
        SignedDocument.load(initiation),
        _load_verified_topology(topology_bundle, trust_root),
        receiver_index=receiver_index,
    )
    accepted.response.save(response_output, force=force)
    if accepted.session.security is None:
        raise typer.BadParameter("accepted handshake did not compile security material")
    _write_json_output(security_output, accepted.session.export_config(), force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {
                "status": "accepted",
                "response": str(response_output),
                "security": str(security_output),
                "session": accepted.session.export_public_summary(),
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("handshake-complete")
def handshake_complete(
    local_identity: Path = typer.Option(..., "--local", help="Local initiator private identity JSON."),
    topology_bundle: Path = typer.Option(..., "--topology", help="Signed topology bundle JSON."),
    trust_root: Path = typer.Option(..., "--trust-root", help="Trusted public or private issuer identity JSON."),
    pending: Path = typer.Option(..., "--pending", help="Secret pending initiator state JSON."),
    response: Path = typer.Option(..., "--response", help="Signed response JSON from the peer."),
    security_output: Path = typer.Option(..., "--security-output", help="Secret config-compatible security block."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing security output file."),
) -> None:
    """Complete an authenticated-session handshake and write initiator security material."""
    session = complete_handshake_initiator(
        _load_identity_record(local_identity).to_identity(),
        _load_pending_handshake(pending),
        SignedDocument.load(response),
        _load_verified_topology(topology_bundle, trust_root),
    )
    if session.security is None:
        raise typer.BadParameter("completed handshake did not compile security material")
    _write_json_output(security_output, session.export_config(), force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {
                "status": "completed",
                "security": str(security_output),
                "session": session.export_public_summary(),
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("trust-root-export")
def trust_root_export(
    identity: Path = typer.Argument(..., help="Trusted public or private identity JSON."),
    output: Path = typer.Argument(..., help="Public trust-root output path."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing public trust-root export."),
) -> None:
    """Export a public trust-root identity without private key material."""
    public = _load_public_identity_record(identity)
    try:
        public.save(output, force=force)
    except FileExistsError as exc:
        raise typer.BadParameter(f"{output} already exists; pass --force to overwrite") from exc
    typer.echo(
        json.dumps({"status": "exported", "path": str(output), "node_id": public.node_id}, indent=2, sort_keys=True)
    )


@app.command("trust-root-import")
def trust_root_import(
    name: str = typer.Argument(..., help="Local trust-root name."),
    identity: Path = typer.Argument(..., help="Trusted public or private identity JSON."),
    state_dir: Path = typer.Option(Path(".gatherlink/state"), "--state-dir", help="Gatherlink state directory."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing trust-root record."),
) -> None:
    """Import a public trust-root into the local Gatherlink state directory."""
    _validate_state_name(name)
    public = _load_public_identity_record(identity)
    path = _state_paths(state_dir).trust_root_path(name)
    try:
        public.save(path, force=force)
    except FileExistsError as exc:
        raise typer.BadParameter(f"{path} already exists; pass --force to overwrite") from exc
    typer.echo(
        json.dumps(
            {"status": "imported", "name": name, "path": str(path), "node_id": public.node_id}, indent=2, sort_keys=True
        )
    )


@app.command("trust-root-list")
def trust_root_list(
    state_dir: Path = typer.Option(Path(".gatherlink/state"), "--state-dir", help="Gatherlink state directory."),
) -> None:
    """List imported public trust roots from the local Gatherlink state directory."""
    root_dir = _state_paths(state_dir).state_dir / "trust-roots"
    roots = []
    for path in sorted(root_dir.glob("*.public.json")):
        public = IdentityPublicRecord.load(path)
        roots.append({"name": path.name.removesuffix(".public.json"), "path": str(path), "node_id": public.node_id})
    typer.echo(json.dumps({"trust_roots": roots}, indent=2, sort_keys=True))


@app.command("state-audit")
def state_audit(
    state_dir: Path = typer.Option(Path(".gatherlink/state"), "--state-dir", help="Gatherlink state directory."),
    strict_hints: bool = typer.Option(
        False,
        "--strict-hints",
        help="Treat corrupt non-authoritative hints and endpoint cache entries as errors.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the full redacted audit report as JSON."),
) -> None:
    """Audit persisted local state without printing secret material."""
    report = audit_persistent_state(_state_paths(state_dir), strict_hints=strict_hints)
    if json_output:
        typer.echo(json.dumps(report.export_dict(), indent=2, sort_keys=True))
    else:
        _print_state_audit(report)
    if not report.ok:
        raise typer.Exit(1)


@app.command("secret-seal")
def secret_seal(
    input_path: Path = typer.Argument(..., help="Owner-only secret JSON input path."),
    output_path: Path = typer.Argument(..., help="Owner-only sealed secret output path."),
    label: str = typer.Option(..., "--label", help="Purpose label bound into the sealed envelope."),
    passphrase_env: str = typer.Option(
        DEFAULT_PASSPHRASE_ENV,
        "--passphrase-env",
        help="Environment variable containing the sealing passphrase.",
    ),
    passphrase_file: Path | None = typer.Option(None, "--passphrase-file", help="File containing the passphrase."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing sealed output file."),
) -> None:
    """Seal owner-only secret JSON without printing secret values."""
    payload = load_secret_json(input_path)
    envelope = seal_secret_json(payload, passphrase=_read_passphrase(passphrase_env, passphrase_file), label=label)
    _write_json_output(output_path, envelope.export_dict(), force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {"status": "sealed", "path": str(output_path), "summary": envelope.public_summary()},
            indent=2,
            sort_keys=True,
        )
    )


@app.command("secret-open")
def secret_open(
    input_path: Path = typer.Argument(..., help="Sealed secret JSON input path."),
    output_path: Path = typer.Argument(..., help="Owner-only opened secret JSON output path."),
    label: str | None = typer.Option(None, "--label", help="Expected label; rejects the envelope if it differs."),
    passphrase_env: str = typer.Option(
        DEFAULT_PASSPHRASE_ENV,
        "--passphrase-env",
        help="Environment variable containing the opening passphrase.",
    ),
    passphrase_file: Path | None = typer.Option(None, "--passphrase-file", help="File containing the passphrase."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing opened output file."),
) -> None:
    """Open a sealed secret to an owner-only JSON file without printing it."""
    envelope = SealedSecretEnvelope.model_validate(_load_json_object(input_path))
    payload = open_secret_json(
        envelope,
        passphrase=_read_passphrase(passphrase_env, passphrase_file),
        expected_label=label,
    )
    if not isinstance(payload, dict):
        raise typer.BadParameter("sealed secret payload must be a JSON object")
    _write_json_output(output_path, payload, force=force, mode=0o600)
    typer.echo(
        json.dumps(
            {
                "status": "opened",
                "path": str(output_path),
                "summary": redact_secrets(payload),
                "label": envelope.label,
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("secret-inspect")
def secret_inspect(input_path: Path = typer.Argument(..., help="Sealed secret JSON input path.")) -> None:
    """Print sealed secret metadata without opening or exposing plaintext."""
    envelope = SealedSecretEnvelope.model_validate(_load_json_object(input_path))
    typer.echo(json.dumps(envelope.public_summary(), indent=2, sort_keys=True))


def _load_identity_record(path: Path) -> IdentityRecord:
    """Load a private identity record from disk."""
    return IdentityRecord.load(path)


def _load_public_identity_record(path: Path) -> IdentityPublicRecord:
    """Load either a public or private identity record as public peer material."""
    return IdentityPublicRecord.load(path)


def _load_verified_topology(path: Path, trust_root: Path) -> TopologyBundleBody:
    """Load a topology bundle through the same trust-root path as topology-verify."""
    return load_verified_topology_bundle(
        SignedDocument.load(path),
        trusted_issuer=_load_public_identity_record(trust_root),
    )


def _load_pending_handshake(path: Path) -> PendingHandshakeInitiation:
    """Load secret pending initiator state from disk."""
    return PendingHandshakeInitiation.from_secret_dict(load_secret_json(path))


def _load_pending_noise(path: Path) -> PendingNoiseInitiation:
    """Load owner-only pending Noise initiator state from disk."""
    return PendingNoiseInitiation.from_secret_dict(load_secret_json(path))


def _load_json_object(path: Path) -> dict:
    """Load a public JSON object from disk."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{path} must contain a JSON object")
    return payload


def _write_json_output(path: Path, payload: dict, *, force: bool, mode: int) -> None:
    """Write a JSON output file with the requested permissions and no accidental overwrite."""
    if path.exists() and not force:
        raise typer.BadParameter(f"{path} already exists; pass --force to overwrite")
    atomic_write_json(path, payload, mode=mode)


def _read_passphrase(passphrase_env: str | None, passphrase_file: Path | None) -> str:
    """Load a non-empty passphrase from one explicit noninteractive source."""
    if passphrase_file is not None:
        try:
            value = passphrase_file.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError) as exc:
            raise typer.BadParameter(f"cannot read passphrase from {passphrase_file}") from exc
    else:
        env_name = passphrase_env or DEFAULT_PASSPHRASE_ENV
        value = os.environ.get(env_name, "")
        if not value:
            raise typer.BadParameter(f"set {env_name} or pass --passphrase-file")
    if not value:
        raise typer.BadParameter("passphrase must not be empty")
    return value


def _parse_provisioned_node(value: str) -> ProvisionedNode:
    """Parse a CLI node mapping in the form ``name=identity.json``."""
    name, separator, path_text = value.partition("=")
    if separator != "=" or not name or not path_text:
        raise typer.BadParameter("node must be name=identity.json")
    return ProvisionedNode(name=name, identity=_load_public_identity_record(Path(path_text)))


def _parse_provisioned_service(value: str) -> ProvisionedService:
    """Parse a CLI service mapping in the form ``name=owner_node=service_id``."""
    parts = value.split("=")
    if len(parts) != 3 or not all(parts):
        raise typer.BadParameter("service must be name=owner_node=service_id")
    name, owner_node, service_id_text = parts
    return ProvisionedService(name=name, owner_node=owner_node, service_id=int(service_id_text))


def _state_paths(state_dir: Path) -> GatherlinkStatePaths:
    """Build testable state paths without requiring writes to Debian system directories."""
    return GatherlinkStatePaths(
        config_dir=state_dir.parent / "config",
        state_dir=state_dir,
        runtime_dir=state_dir.parent / "run",
        log_dir=state_dir.parent / "log",
    )


def _validate_state_name(name: str) -> None:
    """Keep state filenames boring and shell-friendly."""
    if not name or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in name
    ):
        raise typer.BadParameter("name may contain only letters, digits, underscore, dot, and dash")


def _print_state_audit(report: StateAuditReport) -> None:
    """Render a compact redacted state-audit report."""
    payload = report.export_dict()
    summary = payload["summary"]
    typer.echo(
        f"state audit: {'ok' if report.ok else 'failed'} "
        f"state_dir={payload['state_dir']} ok={summary['ok']} warnings={summary['warning']} errors={summary['error']}"
    )
    for finding in payload["findings"]:
        typer.echo(f"{finding['severity']:7} {finding['code']} {finding['path']} - {finding['message']}")
