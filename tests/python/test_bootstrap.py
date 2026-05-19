from __future__ import annotations

import json
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
from gatherlink.secrets.identity import IdentityPublicRecord
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


def test_probe_refuses_authenticated_mode_until_crypto_exists() -> None:
    result = probe_candidate(BootstrapEndpoint.parse("127.0.0.1:51820"))

    assert result.reachable is False
    assert result.authenticated is False
    assert "authenticated bootstrap probes" in result.warning


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
