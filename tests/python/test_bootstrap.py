from __future__ import annotations

import json
import stat
from datetime import UTC, datetime

from gatherlink.bootstrap.cache import BootstrapCache, BootstrapEndpoint
from gatherlink.bootstrap.connector import (
    create_bootstrap_challenge,
    probe_candidate,
    sign_bootstrap_challenge,
    verify_bootstrap_challenge_proof,
)
from gatherlink.bootstrap.resolver import resolve_bootstrap
from gatherlink.cli.main import app
from gatherlink.persistence.store import atomic_write_json
from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord
from gatherlink.security.keys import NodeIdentity
from typer.testing import CliRunner


def test_bootstrap_endpoint_parses_ipv4_dns_and_ipv6() -> None:
    assert BootstrapEndpoint.parse("127.0.0.1:51820").authority() == "127.0.0.1:51820"
    assert BootstrapEndpoint.parse("example.test:51820").authority() == "example.test:51820"
    assert BootstrapEndpoint.parse("[2001:db8::1]:51820").authority() == "[2001:db8::1]:51820"


def test_bootstrap_cache_round_trips_endpoints(tmp_path) -> None:
    cache_path = tmp_path / "bootstrap-cache.json"
    cache = BootstrapCache()
    cache.put("peer-a", [BootstrapEndpoint.parse("127.0.0.1:51820", source="cache")])
    cache.save(cache_path)

    loaded = BootstrapCache.load(cache_path)

    assert loaded.get("peer-a")[0].authority() == "127.0.0.1:51820"
    assert loaded.get("peer-a")[0].source == "cache"


def test_resolver_prefers_static_and_deduplicates_cache(tmp_path) -> None:
    cache_path = tmp_path / "bootstrap-cache.json"
    cache = BootstrapCache()
    cache.put(
        "peer-a",
        [
            BootstrapEndpoint.parse("127.0.0.1:51820", source="cache"),
            BootstrapEndpoint.parse("127.0.0.1:51821", source="cache"),
        ],
    )
    cache.save(cache_path)

    resolution = resolve_bootstrap("peer-a", static_endpoints=["127.0.0.1:51820"], cache_path=cache_path)

    assert [endpoint.authority() for endpoint in resolution.endpoints] == [
        "127.0.0.1:51820",
        "127.0.0.1:51821",
    ]
    assert resolution.sources == ["static", "cache"]


def test_probe_requires_authenticated_proof_or_insecure_flag() -> None:
    result = probe_candidate(BootstrapEndpoint.parse("127.0.0.1:51820"))

    assert result.reachable is False
    assert result.authenticated is False
    assert "authenticated bootstrap requires" in result.warning


def test_signed_bootstrap_challenge_verifies_expected_peer_and_endpoint() -> None:
    peer = NodeIdentity.generate()
    endpoint = BootstrapEndpoint.parse("127.0.0.1:51820")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    challenge = create_bootstrap_challenge(endpoint, now=now, nonce=b"1" * 32)
    proof = sign_bootstrap_challenge(peer, challenge)

    verify_bootstrap_challenge_proof(
        proof,
        IdentityPublicRecord.from_identity(peer),
        expected_endpoint=endpoint,
        now=now,
    )


def test_authenticated_probe_accepts_signed_peer_challenge() -> None:
    peer = NodeIdentity.generate()
    endpoint = BootstrapEndpoint.parse("127.0.0.1:51820")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    challenge = create_bootstrap_challenge(endpoint, now=now, nonce=b"2" * 32)
    proof = sign_bootstrap_challenge(peer, challenge)

    result = probe_candidate(
        endpoint,
        expected_peer=IdentityPublicRecord.from_identity(peer),
        proof=proof,
        now=now,
    )

    assert result.reachable is True
    assert result.authenticated is True
    assert result.endpoint.last_verified_at == now
    assert result.proof["domain"] == "GATHERLINK_BOOTSTRAP_CHALLENGE_V1"


def test_bootstrap_cli_resolves_static_endpoint() -> None:
    result = CliRunner().invoke(app, ["bootstrap", "resolve", "peer-a", "--static", "127.0.0.1:51820"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["peer"] == "peer-a"
    assert payload["endpoints"][0]["host"] == "127.0.0.1"


def test_bootstrap_cli_probe_requires_insecure_flag() -> None:
    result = CliRunner().invoke(app, ["bootstrap", "probe", "127.0.0.1:51820"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["reachable"] is False


def test_bootstrap_cli_probe_accepts_signed_proof_and_updates_cache(tmp_path) -> None:
    runner = CliRunner()
    endpoint = "127.0.0.1:51820"
    identity = NodeIdentity.generate()
    identity_path = tmp_path / "peer.json"
    challenge_path = tmp_path / "challenge.json"
    proof_path = tmp_path / "proof.json"
    cache_path = tmp_path / "bootstrap-cache.json"

    identity_path.write_text(json.dumps(IdentityRecord.from_identity(identity).export_dict()), encoding="utf-8")

    challenge = runner.invoke(app, ["bootstrap", "challenge", endpoint])
    assert challenge.exit_code == 0
    challenge_path.write_text(challenge.output, encoding="utf-8")

    proof = runner.invoke(app, ["bootstrap", "proof", str(identity_path), str(challenge_path)])
    assert proof.exit_code == 0
    proof_path.write_text(proof.output, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "probe",
            endpoint,
            "--peer-identity",
            str(identity_path),
            "--proof",
            str(proof_path),
            "--cache-peer",
            "peer-a",
            "--cache",
            str(cache_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["reachable"] is True
    assert payload["authenticated"] is True
    cached = BootstrapCache.load(cache_path).get("peer-a")
    assert cached[0].authority() == endpoint
    assert cached[0].last_verified_at is not None


def test_secrets_cli_creates_public_identity_and_static_session(tmp_path) -> None:
    runner = CliRunner()
    initiator_path = tmp_path / "initiator.json"
    responder_path = tmp_path / "responder.json"

    first = runner.invoke(app, ["secrets", "identity-create", str(initiator_path)])
    second = runner.invoke(app, ["secrets", "identity-create", str(responder_path)])
    assert first.exit_code == 0
    assert second.exit_code == 0

    public = runner.invoke(app, ["secrets", "identity-public", str(initiator_path)])
    assert public.exit_code == 0
    public_payload = json.loads(public.output)
    assert "ed25519_private" not in public_payload
    assert "x25519_private" not in public_payload

    initiator = runner.invoke(
        app,
        [
            "secrets",
            "static-session",
            "--local",
            str(initiator_path),
            "--peer",
            str(responder_path),
            "--role",
            "initiator",
            "--context",
            "test",
        ],
    )
    responder = runner.invoke(
        app,
        [
            "secrets",
            "static-session",
            "--local",
            str(responder_path),
            "--peer",
            str(initiator_path),
            "--role",
            "responder",
            "--context",
            "test",
        ],
    )
    assert initiator.exit_code == 0
    assert responder.exit_code == 0
    initiator_security = json.loads(initiator.output)
    responder_security = json.loads(responder.output)
    assert initiator_security["send_key"] == responder_security["receive_key"]
    assert initiator_security["receive_key"] == responder_security["send_key"]


def test_secrets_cli_creates_and_verifies_signed_topology(tmp_path) -> None:
    runner = CliRunner()
    issuer_path = tmp_path / "issuer.json"
    node_path = tmp_path / "node-a.json"
    bundle_path = tmp_path / "topology.signed.json"

    assert runner.invoke(app, ["secrets", "identity-create", str(issuer_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(node_path)]).exit_code == 0

    create = runner.invoke(
        app,
        [
            "secrets",
            "topology-create",
            "--issuer",
            str(issuer_path),
            "--output",
            str(bundle_path),
            "--generation",
            "3",
            "--node",
            f"node-a={node_path}",
            "--service",
            "wireguard=node-a=256",
        ],
    )
    assert create.exit_code == 0
    assert bundle_path.exists()

    verify = runner.invoke(
        app,
        [
            "secrets",
            "topology-verify",
            str(bundle_path),
            "--trust-root",
            str(issuer_path),
            "--minimum-generation",
            "3",
        ],
    )
    assert verify.exit_code == 0
    payload = json.loads(verify.output)
    assert payload["generation"] == 3
    assert payload["nodes"][0]["name"] == "node-a"
    assert payload["services"][0]["service_id"] == 256


def test_secrets_cli_creates_authenticated_handshake_security_files(tmp_path) -> None:
    runner = CliRunner()
    issuer_path = tmp_path / "issuer.json"
    initiator_path = tmp_path / "initiator.json"
    responder_path = tmp_path / "responder.json"
    bundle_path = tmp_path / "topology.signed.json"
    initiation_path = tmp_path / "handshake-init.signed.json"
    pending_path = tmp_path / "handshake-init.pending.json"
    response_path = tmp_path / "handshake-response.signed.json"
    initiator_security_path = tmp_path / "initiator-security.json"
    responder_security_path = tmp_path / "responder-security.json"

    assert runner.invoke(app, ["secrets", "identity-create", str(issuer_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(initiator_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(responder_path)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "topology-create",
                "--issuer",
                str(issuer_path),
                "--output",
                str(bundle_path),
                "--generation",
                "7",
                "--node",
                f"initiator={initiator_path}",
                "--node",
                f"responder={responder_path}",
                "--service",
                "wireguard=initiator=256",
            ],
        ).exit_code
        == 0
    )

    init = runner.invoke(
        app,
        [
            "secrets",
            "handshake-init",
            "--local",
            str(initiator_path),
            "--peer",
            str(responder_path),
            "--topology",
            str(bundle_path),
            "--trust-root",
            str(issuer_path),
            "--initiation-output",
            str(initiation_path),
            "--pending-output",
            str(pending_path),
            "--receiver-index",
            "111",
        ],
    )
    assert init.exit_code == 0
    assert initiation_path.exists()
    pending_payload = json.loads(pending_path.read_text(encoding="utf-8"))
    assert "ephemeral_private" in pending_payload

    accept = runner.invoke(
        app,
        [
            "secrets",
            "handshake-accept",
            "--local",
            str(responder_path),
            "--topology",
            str(bundle_path),
            "--trust-root",
            str(issuer_path),
            "--initiation",
            str(initiation_path),
            "--response-output",
            str(response_path),
            "--security-output",
            str(responder_security_path),
            "--receiver-index",
            "222",
        ],
    )
    assert accept.exit_code == 0

    complete = runner.invoke(
        app,
        [
            "secrets",
            "handshake-complete",
            "--local",
            str(initiator_path),
            "--topology",
            str(bundle_path),
            "--trust-root",
            str(issuer_path),
            "--pending",
            str(pending_path),
            "--response",
            str(response_path),
            "--security-output",
            str(initiator_security_path),
        ],
    )
    assert complete.exit_code == 0
    initiator_security = json.loads(initiator_security_path.read_text(encoding="utf-8"))
    responder_security = json.loads(responder_security_path.read_text(encoding="utf-8"))
    assert initiator_security["local_receiver_index"] == 111
    assert initiator_security["remote_receiver_index"] == 222
    assert responder_security["local_receiver_index"] == 222
    assert responder_security["remote_receiver_index"] == 111
    assert initiator_security["send_key"] == responder_security["receive_key"]
    assert initiator_security["receive_key"] == responder_security["send_key"]


def test_secrets_cli_creates_noise_ik_security_files(tmp_path) -> None:
    runner = CliRunner()
    issuer_path = tmp_path / "issuer.json"
    initiator_path = tmp_path / "initiator.json"
    responder_path = tmp_path / "responder.json"
    bundle_path = tmp_path / "topology.signed.json"
    initiation_path = tmp_path / "noise-init.json"
    pending_path = tmp_path / "noise-init.pending.secret.json"
    response_path = tmp_path / "noise-response.json"
    initiator_security_path = tmp_path / "initiator-security.json"
    responder_security_path = tmp_path / "responder-security.json"

    assert runner.invoke(app, ["secrets", "identity-create", str(issuer_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(initiator_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(responder_path)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "topology-create",
                "--issuer",
                str(issuer_path),
                "--output",
                str(bundle_path),
                "--generation",
                "8",
                "--node",
                f"initiator={initiator_path}",
                "--node",
                f"responder={responder_path}",
            ],
        ).exit_code
        == 0
    )

    init = runner.invoke(
        app,
        [
            "secrets",
            "noise-init",
            "--local",
            str(initiator_path),
            "--peer",
            str(responder_path),
            "--topology",
            str(bundle_path),
            "--trust-root",
            str(issuer_path),
            "--initiation-output",
            str(initiation_path),
            "--pending-output",
            str(pending_path),
            "--receiver-index",
            "333",
        ],
    )
    assert init.exit_code == 0
    initiation_payload = json.loads(initiation_path.read_text(encoding="utf-8"))
    pending_payload = json.loads(pending_path.read_text(encoding="utf-8"))
    assert initiation_payload["protocol"] == "noise-ik"
    assert "encrypted_initiator_static_x25519" in initiation_payload
    assert "ephemeral_private" in pending_payload

    accept = runner.invoke(
        app,
        [
            "secrets",
            "noise-accept",
            "--local",
            str(responder_path),
            "--topology",
            str(bundle_path),
            "--trust-root",
            str(issuer_path),
            "--initiation",
            str(initiation_path),
            "--response-output",
            str(response_path),
            "--security-output",
            str(responder_security_path),
            "--receiver-index",
            "444",
        ],
    )
    assert accept.exit_code == 0

    complete = runner.invoke(
        app,
        [
            "secrets",
            "noise-complete",
            "--local",
            str(initiator_path),
            "--topology",
            str(bundle_path),
            "--trust-root",
            str(issuer_path),
            "--pending",
            str(pending_path),
            "--response",
            str(response_path),
            "--security-output",
            str(initiator_security_path),
        ],
    )
    assert complete.exit_code == 0
    initiator_security = json.loads(initiator_security_path.read_text(encoding="utf-8"))
    responder_security = json.loads(responder_security_path.read_text(encoding="utf-8"))
    assert initiator_security["mode"] == "authenticated"
    assert initiator_security["local_receiver_index"] == 333
    assert initiator_security["remote_receiver_index"] == 444
    assert responder_security["local_receiver_index"] == 444
    assert responder_security["remote_receiver_index"] == 333
    assert initiator_security["send_key"] == responder_security["receive_key"]
    assert initiator_security["receive_key"] == responder_security["send_key"]


def test_secrets_cli_noise_ik_generates_receiver_indexes_by_default(tmp_path) -> None:
    runner = CliRunner()
    issuer_path = tmp_path / "issuer.json"
    initiator_path = tmp_path / "initiator.json"
    responder_path = tmp_path / "responder.json"
    bundle_path = tmp_path / "topology.signed.json"
    initiation_path = tmp_path / "noise-init.json"
    pending_path = tmp_path / "noise-init.pending.secret.json"
    response_path = tmp_path / "noise-response.json"
    initiator_security_path = tmp_path / "initiator-security.json"
    responder_security_path = tmp_path / "responder-security.json"

    assert runner.invoke(app, ["secrets", "identity-create", str(issuer_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(initiator_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(responder_path)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "topology-create",
                "--issuer",
                str(issuer_path),
                "--output",
                str(bundle_path),
                "--generation",
                "9",
                "--node",
                f"initiator={initiator_path}",
                "--node",
                f"responder={responder_path}",
            ],
        ).exit_code
        == 0
    )

    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "noise-init",
                "--local",
                str(initiator_path),
                "--peer",
                str(responder_path),
                "--topology",
                str(bundle_path),
                "--trust-root",
                str(issuer_path),
                "--initiation-output",
                str(initiation_path),
                "--pending-output",
                str(pending_path),
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "noise-accept",
                "--local",
                str(responder_path),
                "--topology",
                str(bundle_path),
                "--trust-root",
                str(issuer_path),
                "--initiation",
                str(initiation_path),
                "--response-output",
                str(response_path),
                "--security-output",
                str(responder_security_path),
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "noise-complete",
                "--local",
                str(initiator_path),
                "--topology",
                str(bundle_path),
                "--trust-root",
                str(issuer_path),
                "--pending",
                str(pending_path),
                "--response",
                str(response_path),
                "--security-output",
                str(initiator_security_path),
            ],
        ).exit_code
        == 0
    )

    initiator_security = json.loads(initiator_security_path.read_text(encoding="utf-8"))
    responder_security = json.loads(responder_security_path.read_text(encoding="utf-8"))
    assert initiator_security["local_receiver_index"] >= 1
    assert initiator_security["remote_receiver_index"] >= 1
    assert responder_security["local_receiver_index"] == initiator_security["remote_receiver_index"]
    assert responder_security["remote_receiver_index"] == initiator_security["local_receiver_index"]
    assert {
        initiator_security["local_receiver_index"],
        initiator_security["remote_receiver_index"],
    } != {1}


def test_secrets_cli_exports_imports_and_lists_trust_roots(tmp_path) -> None:
    runner = CliRunner()
    issuer_path = tmp_path / "issuer.json"
    public_path = tmp_path / "issuer.public.json"
    state_dir = tmp_path / "state"

    assert runner.invoke(app, ["secrets", "identity-create", str(issuer_path)]).exit_code == 0

    export = runner.invoke(app, ["secrets", "trust-root-export", str(issuer_path), str(public_path)])
    assert export.exit_code == 0
    exported = json.loads(public_path.read_text(encoding="utf-8"))
    assert "ed25519_private" not in exported
    assert "x25519_private" not in exported

    imported = runner.invoke(
        app,
        [
            "secrets",
            "trust-root-import",
            "lab-root",
            str(public_path),
            "--state-dir",
            str(state_dir),
        ],
    )
    assert imported.exit_code == 0

    listed = runner.invoke(app, ["secrets", "trust-root-list", "--state-dir", str(state_dir)])
    assert listed.exit_code == 0
    payload = json.loads(listed.output)
    assert payload["trust_roots"][0]["name"] == "lab-root"
    assert payload["trust_roots"][0]["node_id"] == exported["node_id"]


def test_secrets_cli_topology_diff_explains_candidate_before_install(tmp_path) -> None:
    runner = CliRunner()
    issuer_path = tmp_path / "issuer.json"
    node_a_path = tmp_path / "node-a.json"
    node_b_path = tmp_path / "node-b.json"
    current_path = tmp_path / "current.signed.json"
    candidate_path = tmp_path / "candidate.signed.json"

    assert runner.invoke(app, ["secrets", "identity-create", str(issuer_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(node_a_path)]).exit_code == 0
    assert runner.invoke(app, ["secrets", "identity-create", str(node_b_path)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "topology-create",
                "--issuer",
                str(issuer_path),
                "--output",
                str(current_path),
                "--generation",
                "1",
                "--node",
                f"node-a={node_a_path}",
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "secrets",
                "topology-create",
                "--issuer",
                str(issuer_path),
                "--output",
                str(candidate_path),
                "--generation",
                "2",
                "--node",
                f"node-a={node_a_path}",
                "--node",
                f"node-b={node_b_path}",
                "--service",
                "dns=node-b=257",
            ],
        ).exit_code
        == 0
    )

    result = runner.invoke(
        app,
        [
            "secrets",
            "topology-diff",
            str(current_path),
            str(candidate_path),
            "--trust-root",
            str(issuer_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["generation_delta"] == 1
    assert payload["added_nodes"] == ["node-b"]
    assert payload["added_services"] == ["dns"]


def test_secrets_cli_rejects_unsafe_trust_root_name(tmp_path) -> None:
    runner = CliRunner()
    issuer_path = tmp_path / "issuer.json"
    assert runner.invoke(app, ["secrets", "identity-create", str(issuer_path)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "secrets",
            "trust-root-import",
            "../bad",
            str(issuer_path),
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )

    assert result.exit_code != 0
    assert "name may contain only" in result.output


def test_secrets_cli_state_audit_reports_redacted_state(tmp_path) -> None:
    runner = CliRunner()
    state_dir = tmp_path / "state"
    identity = NodeIdentity.generate()
    identity_record = IdentityRecord.from_identity(identity)
    atomic_write_json(state_dir / "identities" / "node-a.identity.json", identity_record.export_dict(), mode=0o600)

    result = runner.invoke(app, ["secrets", "state-audit", "--state-dir", str(state_dir), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["summary"]["ok"] == 1
    assert "ed25519_private" not in result.output
    assert "x25519_private" not in result.output


def test_secrets_cli_state_audit_fails_for_unsafe_private_state(tmp_path) -> None:
    runner = CliRunner()
    state_dir = tmp_path / "state"
    identity = NodeIdentity.generate()
    identity_record = IdentityRecord.from_identity(identity)
    atomic_write_json(state_dir / "identities" / "node-a.identity.json", identity_record.export_dict(), mode=0o644)

    result = runner.invoke(app, ["secrets", "state-audit", "--state-dir", str(state_dir), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "state.permission.too_broad"


def test_secrets_cli_seals_inspects_and_opens_secret_without_leaking_plaintext(tmp_path) -> None:
    runner = CliRunner()
    secret_path = tmp_path / "secret.json"
    sealed_path = tmp_path / "secret.sealed.json"
    opened_path = tmp_path / "secret.opened.json"
    atomic_write_json(secret_path, {"x25519_private": "super-secret", "node": "node-a"}, mode=0o600)
    env = {"GATHERLINK_SECRET_PASSPHRASE": "correct horse battery staple"}

    sealed = runner.invoke(
        app,
        ["secrets", "secret-seal", str(secret_path), str(sealed_path), "--label", "node-identity"],
        env=env,
    )
    inspected = runner.invoke(app, ["secrets", "secret-inspect", str(sealed_path)])
    opened = runner.invoke(
        app,
        ["secrets", "secret-open", str(sealed_path), str(opened_path), "--label", "node-identity"],
        env=env,
    )

    assert sealed.exit_code == 0
    assert inspected.exit_code == 0
    assert opened.exit_code == 0
    assert "super-secret" not in sealed.output
    assert "super-secret" not in inspected.output
    assert "super-secret" not in opened.output
    assert json.loads(opened_path.read_text(encoding="utf-8"))["x25519_private"] == "super-secret"
    assert stat.S_IMODE(sealed_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(opened_path.stat().st_mode) == 0o600


def test_secrets_cli_accepts_passphrase_file_for_sealed_secret(tmp_path) -> None:
    runner = CliRunner()
    secret_path = tmp_path / "secret.json"
    sealed_path = tmp_path / "secret.sealed.json"
    opened_path = tmp_path / "secret.opened.json"
    passphrase_path = tmp_path / "passphrase.txt"
    atomic_write_json(secret_path, {"x25519_private": "file-secret"}, mode=0o600)
    passphrase_path.write_text("file based passphrase\n", encoding="utf-8")

    sealed = runner.invoke(
        app,
        [
            "secrets",
            "secret-seal",
            str(secret_path),
            str(sealed_path),
            "--label",
            "node-identity",
            "--passphrase-file",
            str(passphrase_path),
        ],
    )
    opened = runner.invoke(
        app,
        [
            "secrets",
            "secret-open",
            str(sealed_path),
            str(opened_path),
            "--label",
            "node-identity",
            "--passphrase-file",
            str(passphrase_path),
        ],
    )

    assert sealed.exit_code == 0
    assert opened.exit_code == 0
    assert "file-secret" not in sealed.output
    assert "file-secret" not in opened.output
    assert json.loads(opened_path.read_text(encoding="utf-8"))["x25519_private"] == "file-secret"
