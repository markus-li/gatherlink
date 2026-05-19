import json
import stat
from pathlib import Path

from gatherlink.persistence.sealed import open_secret_json, seal_secret_json
from gatherlink.persistence.store import (
    GatherlinkStatePaths,
    PersistentStateStore,
    atomic_write_json,
    load_json_or_default,
    load_secret_json,
    redact_secrets,
)
from gatherlink.platform.debian import DebianCompatibilityBackend


def test_state_paths_use_debian_layout() -> None:
    paths = GatherlinkStatePaths.debian(DebianCompatibilityBackend())

    assert paths.config_dir == Path("/etc/gatherlink")
    assert paths.state_dir == Path("/var/lib/gatherlink")
    assert paths.runtime_dir == Path("/run/gatherlink")
    assert paths.log_dir == Path("/var/log/gatherlink")
    assert paths.identity_path("node-a") == Path("/var/lib/gatherlink/identities/node-a.identity.json")
    assert paths.bootstrap_cache_path() == Path("/var/lib/gatherlink/bootstrap/endpoints.json")
    assert paths.signed_bundle_path("topology") == Path("/var/lib/gatherlink/bundles/topology.signed.json")
    assert paths.trust_root_path("root-a") == Path("/var/lib/gatherlink/trust-roots/root-a.public.json")
    assert paths.hint_path("path-mtu") == Path("/var/lib/gatherlink/hints/path-mtu.json")
    assert paths.sealed_secret_path("node-a") == Path("/var/lib/gatherlink/secrets/node-a.sealed.json")


def test_atomic_write_json_sets_permissions_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    atomic_write_json(target, {"value": 1}, mode=0o600)
    atomic_write_json(target, {"value": 2}, mode=0o600)

    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_load_json_or_default_ignores_missing_and_corrupt_state(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    assert load_json_or_default(missing, {"default": True}) == {"default": True}
    assert load_json_or_default(corrupt, {"default": True}) == {"default": True}


def test_load_secret_json_rejects_group_or_world_readable_files(tmp_path: Path) -> None:
    secret = tmp_path / "identity.json"
    atomic_write_json(secret, {"secret": True}, mode=0o644)

    try:
        load_secret_json(secret)
    except PermissionError as exc:
        assert "owner-only" in str(exc)
    else:
        raise AssertionError("expected load_secret_json to reject broad permissions")


def test_load_secret_json_accepts_owner_only_files(tmp_path: Path) -> None:
    secret = tmp_path / "identity.json"
    atomic_write_json(secret, {"secret": True}, mode=0o600)

    assert load_secret_json(secret) == {"secret": True}


def test_redact_secrets_keeps_shape_without_leaking_values() -> None:
    payload = {
        "node": "node-a",
        "security": {
            "send_key": "abc123",
            "nested": [{"x25519_private": "secret-value"}],
        },
    }

    redacted = redact_secrets(payload)

    assert redacted["node"] == "node-a"
    assert redacted["security"]["send_key"] == "[redacted:6 chars]"
    assert redacted["security"]["nested"][0]["x25519_private"] == "[redacted:12 chars]"


def test_redact_secrets_preserves_existing_redaction_markers() -> None:
    payload = {"send_key": "[redacted:32 bytes]"}

    assert redact_secrets(payload)["send_key"] == "[redacted:32 bytes]"


def test_sealed_secret_round_trips_without_public_plaintext() -> None:
    payload = {"node": "node-a", "x25519_private": "super-secret"}

    envelope = seal_secret_json(payload, passphrase="correct horse battery staple", label="node-identity")
    summary = envelope.public_summary()
    opened = open_secret_json(envelope, passphrase="correct horse battery staple", expected_label="node-identity")

    assert opened == payload
    assert summary["ciphertext"].startswith("[sealed:")
    assert "super-secret" not in json.dumps(envelope.export_dict())
    assert "super-secret" not in json.dumps(summary)


def test_sealed_secret_fails_closed_for_wrong_passphrase_or_label() -> None:
    envelope = seal_secret_json({"secret": "value"}, passphrase="right", label="node-identity")

    try:
        open_secret_json(envelope, passphrase="wrong", expected_label="node-identity")
    except ValueError as exc:
        assert "could not be opened" in str(exc)
    else:
        raise AssertionError("expected wrong passphrase to fail")

    try:
        open_secret_json(envelope, passphrase="right", expected_label="other-label")
    except ValueError as exc:
        assert "label mismatch" in str(exc)
    else:
        raise AssertionError("expected label mismatch to fail")


def test_persistent_state_store_writes_authority_and_hints_with_expected_permissions(tmp_path: Path) -> None:
    paths = GatherlinkStatePaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        log_dir=tmp_path / "log",
    )
    store = PersistentStateStore(paths)

    identity_path = store.write_private_identity("node-a", {"ed25519_private": "secret"})
    trust_root_path = store.write_trust_root("root-a", {"node_id": "root"})
    bundle_path = store.write_signed_bundle("topology", {"signed": True})
    endpoint_cache_path = store.write_endpoint_cache({"peers": [{"endpoint": "127.0.0.1:1"}]})
    hint_path = store.write_hint("path-mtu", {"path-a": 1200})
    sealed_path = store.write_sealed_secret("node-a", {"ciphertext": "sealed"})

    assert stat.S_IMODE(identity_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(trust_root_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(endpoint_cache_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(hint_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(sealed_path.stat().st_mode) == 0o600
    assert store.read_private_identity("node-a") == {"ed25519_private": "secret"}
    assert store.read_endpoint_cache()["peers"][0]["endpoint"] == "127.0.0.1:1"
    assert store.read_hint("path-mtu") == {"path-a": 1200}


def test_persistent_state_store_does_not_overwrite_authority_without_force(tmp_path: Path) -> None:
    paths = GatherlinkStatePaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        log_dir=tmp_path / "log",
    )
    store = PersistentStateStore(paths)
    store.write_trust_root("root-a", {"node_id": "root"})

    try:
        store.write_trust_root("root-a", {"node_id": "changed"})
    except FileExistsError as exc:
        assert "root-a.public.json" in str(exc)
    else:
        raise AssertionError("expected authority overwrite to fail without force")

    store.write_trust_root("root-a", {"node_id": "changed"}, force=True)
    assert json.loads(paths.trust_root_path("root-a").read_text(encoding="utf-8"))["node_id"] == "changed"


def test_persistent_state_store_public_summary_lists_names_without_values(tmp_path: Path) -> None:
    paths = GatherlinkStatePaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        log_dir=tmp_path / "log",
    )
    store = PersistentStateStore(paths)
    store.write_private_identity("node-a", {"ed25519_private": "secret"})
    store.write_trust_root("root-a", {"node_id": "root"})
    store.write_signed_bundle("topology", {"signed": True})
    store.write_hint("path-mtu", {"secret": "not-listed"})
    store.write_sealed_secret("node-a", {"ciphertext": "sealed"})

    summary = store.public_summary()

    assert summary["identities"] == ["node-a.identity.json"]
    assert summary["trust_roots"] == ["root-a.public.json"]
    assert summary["bundles"] == ["topology.signed.json"]
    assert summary["hints"] == ["path-mtu.json"]
    assert summary["sealed_secrets"] == ["node-a.sealed.json"]
    summary_json = json.dumps(summary)
    assert "not-listed" not in summary_json
    assert "ed25519_private" not in summary_json
