from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidSignature
from gatherlink.persistence.store import atomic_write_json
from gatherlink.secrets.bundles import SignedDocument, canonical_cbor, sign_document
from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord
from gatherlink.secrets.provisioning import ProvisionedNode, TopologyBundleBody
from gatherlink.security.envelope import (
    ENCRYPTED_DATA_HEADER_LEN,
    PACKET_TYPE_ENCRYPTED_DATA_V1,
    TransportKeys,
    decrypt_packet_without_replay,
    encrypt_frame_with_counter,
)
from gatherlink.security.handshake import (
    accept_handshake_initiation,
    complete_handshake_initiator,
    create_handshake_initiation,
)
from gatherlink.security.keys import NodeIdentity, node_id_from_ed25519_public, verify_document, x25519_shared_secret
from gatherlink.security.noise import (
    accept_noise_ik_initiation,
    complete_noise_ik_initiator,
    create_noise_ik_initiation,
)
from gatherlink.security.replay import ReplayWindow
from gatherlink.security.sessions import (
    MAX_RECEIVER_INDEX,
    MIN_RECEIVER_INDEX,
    derive_directional_session_keys,
    derive_static_transport_security,
    generate_receiver_index,
    plan_authenticated_static_session,
    plan_session_rotation,
)


def test_node_identity_signs_documents_and_round_trips_record() -> None:
    identity = NodeIdentity.generate()
    body = {"schema_version": 1, "subject": identity.node_id.hex()}
    signed = sign_document(identity, "GATHERLINK_TOPOLOGY_V1", body)

    signed.verify()
    assert identity.node_id == node_id_from_ed25519_public(identity.ed25519_public)

    with pytest.raises(InvalidSignature):
        verify_document(
            identity.ed25519_public,
            b"GATHERLINK_CONFIG_APPLY_V1",
            canonical_cbor(body),
            signed.signature,
        )

    record = IdentityRecord.from_identity(identity)
    loaded = record.to_identity()
    assert loaded.node_id == identity.node_id
    assert loaded.ed25519_public == identity.ed25519_public
    assert record.export_dict()["schema_version"] == 1
    assert record.export_redacted_dict()["ed25519_private"].startswith("[redacted:")


def test_signed_document_persists_and_verifies(tmp_path) -> None:
    path = tmp_path / "topology.signed.json"
    identity = NodeIdentity.generate()
    signed = sign_document(identity, "GATHERLINK_TOPOLOGY_V1", {"schema_version": 1, "generation": 7})

    signed.save(path)
    loaded = SignedDocument.load(path)

    assert loaded.domain == "GATHERLINK_TOPOLOGY_V1"
    assert loaded.body["generation"] == 7


def test_signed_document_load_fails_closed_when_tampered(tmp_path) -> None:
    path = tmp_path / "topology.signed.json"
    identity = NodeIdentity.generate()
    signed = sign_document(identity, "GATHERLINK_TOPOLOGY_V1", {"schema_version": 1, "generation": 7})
    signed.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["body"]["generation"] = 8
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidSignature):
        SignedDocument.load(path)


def test_identity_record_persists_with_private_permissions(tmp_path) -> None:
    path = tmp_path / "node.identity.json"
    record = IdentityRecord.from_identity(NodeIdentity.generate())

    record.save(path)
    loaded = IdentityRecord.load(path)

    assert loaded.node_id == record.node_id
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_x25519_session_derivation_produces_directional_keys() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()

    shared_a = x25519_shared_secret(initiator.x25519_private, responder.x25519_public)
    shared_b = x25519_shared_secret(responder.x25519_private, initiator.x25519_public)
    assert shared_a == shared_b

    keys = derive_directional_session_keys(shared_a, b"transcript")
    assert len(keys.initiator_to_responder) == 32
    assert len(keys.responder_to_initiator) == 32
    assert keys.initiator_to_responder != keys.responder_to_initiator


def test_static_transport_security_derivation_produces_inverse_peer_configs() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()

    initiator_material = derive_static_transport_security(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        role="initiator",
        receiver_index=77,
        context=b"lab-generation-1",
    )
    responder_material = derive_static_transport_security(
        responder,
        IdentityPublicRecord.from_identity(initiator),
        role="responder",
        receiver_index=77,
        context=b"lab-generation-1",
    )

    assert initiator_material.send_key == responder_material.receive_key
    assert initiator_material.receive_key == responder_material.send_key
    assert initiator_material.export_config()["mode"] == "static"
    assert initiator_material.export_config()["receiver_index"] == 77


def test_authenticated_session_plan_binds_topology_and_rekey_limits() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=9,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
        ],
    )

    initiator_plan = plan_authenticated_static_session(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        role="initiator",
        receiver_index=99,
        now=now,
    )
    responder_plan = plan_authenticated_static_session(
        responder,
        IdentityPublicRecord.from_identity(initiator),
        topology,
        role="responder",
        receiver_index=99,
        now=now,
    )

    assert initiator_plan.security is not None
    assert responder_plan.security is not None
    assert initiator_plan.security.send_key == responder_plan.security.receive_key
    assert initiator_plan.security.receive_key == responder_plan.security.send_key
    assert initiator_plan.export_public_summary()["has_compiled_security"] is True
    assert initiator_plan.needs_rekey(now=now + timedelta(seconds=121))


def test_session_rotation_starts_before_expiry_and_overlaps_receive_windows() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=9,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
        ],
    )
    plan = plan_authenticated_static_session(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        role="initiator",
        receiver_index=99,
        now=now,
        lifetime_seconds=120,
    )

    decision = plan_session_rotation(
        plan,
        now=now + timedelta(seconds=95),
        observed_topology_generation=9,
        observed_peer_node_id=IdentityPublicRecord.from_identity(responder).node_id,
        rekey_margin_seconds=30,
        overlap_seconds=10,
        next_receiver_index=100,
    )

    assert decision.should_rekey
    assert not decision.fail_closed
    assert decision.next_receiver_index == 100
    assert decision.overlap_until == now + timedelta(seconds=105)
    assert decision.export_public_summary()["fail_closed"] is False


def test_session_rotation_fails_closed_for_expiry_or_peer_disagreement() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=9,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
        ],
    )
    plan = plan_authenticated_static_session(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        role="initiator",
        receiver_index=99,
        now=now,
        lifetime_seconds=120,
    )

    generation_mismatch = plan_session_rotation(plan, now=now, observed_topology_generation=10)
    expired = plan_session_rotation(plan, now=now + timedelta(seconds=121))

    assert generation_mismatch.fail_closed
    assert generation_mismatch.action == "reject"
    assert expired.fail_closed
    assert expired.action == "expired"


def test_authenticated_receiver_index_defaults_are_opaque_and_replay_state_resets() -> None:
    generated = [generate_receiver_index() for _item in range(16)]
    assert all(MIN_RECEIVER_INDEX <= value <= MAX_RECEIVER_INDEX for value in generated)
    assert any(value != 1 for value in generated)

    first_key = bytes([1]) * 32
    second_key = bytes([2]) * 32
    first_session = TransportKeys(receiver_index=100, send_key=first_key, receive_key=first_key)
    replacement_session = TransportKeys(receiver_index=101, send_key=second_key, receive_key=second_key)

    first_packet = encrypt_frame_with_counter(100, first_key, 0, b"first-session")
    replacement_packet = encrypt_frame_with_counter(101, second_key, 0, b"replacement-session")

    assert first_session.decrypt_packet(first_packet).plaintext == b"first-session"
    with pytest.raises(ValueError):
        first_session.decrypt_packet(first_packet)
    assert replacement_session.decrypt_packet(replacement_packet).plaintext == b"replacement-session"


def test_authenticated_session_plan_rejects_missing_or_revoked_topology_member() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    topology = TopologyBundleBody(
        generation=1,
        issuer_node_id=IdentityPublicRecord.from_identity(local).node_id,
        nodes=[ProvisionedNode(name="local", identity=IdentityPublicRecord.from_identity(local))],
    )

    with pytest.raises(ValueError, match="not present"):
        plan_authenticated_static_session(
            local,
            IdentityPublicRecord.from_identity(peer),
            topology,
            role="initiator",
            receiver_index=7,
        )

    revoked_topology = topology.model_copy(
        update={
            "nodes": [
                ProvisionedNode(name="local", identity=IdentityPublicRecord.from_identity(local)),
                ProvisionedNode(name="peer", identity=IdentityPublicRecord.from_identity(peer)),
            ],
            "revoked_node_ids": [IdentityPublicRecord.from_identity(peer).node_id],
        }
    )
    with pytest.raises(ValueError, match="revoked"):
        plan_authenticated_static_session(
            local,
            IdentityPublicRecord.from_identity(peer),
            revoked_topology,
            role="initiator",
            receiver_index=7,
        )


def test_authenticated_handshake_compiles_inverse_transport_keys() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=11,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
        ],
    )

    pending = create_handshake_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        receiver_index=222,
        now=now,
    )
    accepted = accept_handshake_initiation(
        responder,
        pending.document,
        topology,
        receiver_index=333,
        now=now + timedelta(seconds=1),
    )
    initiator_session = complete_handshake_initiator(
        initiator,
        pending,
        accepted.response,
        topology,
        now=now + timedelta(seconds=2),
    )

    assert initiator_session.security is not None
    assert accepted.session.security is not None
    assert initiator_session.receiver_index == 333
    assert initiator_session.security.local_receiver_index == 222
    assert initiator_session.security.remote_receiver_index == 333
    assert accepted.session.security.local_receiver_index == 333
    assert accepted.session.security.remote_receiver_index == 222
    assert initiator_session.security.send_key == accepted.session.security.receive_key
    assert initiator_session.security.receive_key == accepted.session.security.send_key
    assert initiator_session.security.export_config()["mode"] == "static"
    assert initiator_session.security.export_config(mode="authenticated")["mode"] == "authenticated"


def test_authenticated_handshake_fails_closed_for_wrong_responder_or_tampering() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    other = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=12,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
            ProvisionedNode(name="other", identity=IdentityPublicRecord.from_identity(other)),
        ],
    )
    pending = create_handshake_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        now=now,
    )

    with pytest.raises(ValueError, match="silently dropped"):
        accept_handshake_initiation(other, pending.document, topology, receiver_index=1, now=now)

    accepted = accept_handshake_initiation(responder, pending.document, topology, receiver_index=1, now=now)
    tampered = SignedDocument(
        domain=accepted.response.domain,
        body={**accepted.response.body, "receiver_index": 2},
        signer_node_id=accepted.response.signer_node_id,
        signer_public_key=accepted.response.signer_public_key,
        signature=accepted.response.signature,
    )
    with pytest.raises(Exception):
        complete_handshake_initiator(initiator, pending, tampered, topology, now=now)


def test_noise_ik_handshake_compiles_inverse_transport_keys_and_hides_static_identity() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=21,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
        ],
    )

    pending = create_noise_ik_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        receiver_index=444,
        now=now,
    )
    accepted = accept_noise_ik_initiation(
        responder,
        pending.message,
        topology,
        receiver_index=555,
        now=now + timedelta(seconds=1),
    )
    initiator_session = complete_noise_ik_initiator(
        initiator,
        pending,
        accepted.response,
        topology,
        now=now + timedelta(seconds=2),
    )

    assert "initiator_static_x25519" not in pending.message
    assert pending.message["encrypted_initiator_static_x25519"]
    assert accepted.session.security is not None
    assert initiator_session.security is not None
    assert initiator_session.security.local_receiver_index == 444
    assert initiator_session.security.remote_receiver_index == 555
    assert accepted.session.security.local_receiver_index == 555
    assert accepted.session.security.remote_receiver_index == 444
    assert initiator_session.security.send_key == accepted.session.security.receive_key
    assert initiator_session.security.receive_key == accepted.session.security.send_key
    assert initiator_session.security.export_config(mode="authenticated")["mode"] == "authenticated"


def test_noise_ik_handshake_fails_closed_for_tampered_or_expired_messages() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=22,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
        ],
    )
    pending = create_noise_ik_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        receiver_index=1,
        now=now,
    )
    tampered = {**pending.message, "encrypted_initiator_static_x25519": pending.message["initiator_ephemeral_x25519"]}

    with pytest.raises(ValueError, match="silently dropped"):
        accept_noise_ik_initiation(responder, tampered, topology, receiver_index=2, now=now)

    with pytest.raises(ValueError, match="silently dropped"):
        accept_noise_ik_initiation(
            responder,
            pending.message,
            topology,
            receiver_index=2,
            now=now + timedelta(seconds=31),
        )


def test_pending_handshake_cli_loader_rejects_broad_secret_permissions(tmp_path) -> None:
    from gatherlink.cli.secrets import _load_pending_handshake

    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = TopologyBundleBody(
        generation=13,
        issuer_node_id=IdentityPublicRecord.from_identity(initiator).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="initiator", identity=IdentityPublicRecord.from_identity(initiator)),
            ProvisionedNode(name="responder", identity=IdentityPublicRecord.from_identity(responder)),
        ],
    )
    pending = create_handshake_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        now=now,
    )
    pending_path = tmp_path / "pending.secret.json"
    atomic_write_json(pending_path, pending.export_secret_dict(), mode=0o644)

    with pytest.raises(PermissionError, match="owner-only"):
        _load_pending_handshake(pending_path)


def test_transport_envelope_encrypts_frame_and_rejects_tampering() -> None:
    key = bytes([7]) * 32
    packet = encrypt_frame_with_counter(42, key, 9, b"encoded-frame")

    assert packet[0] == PACKET_TYPE_ENCRYPTED_DATA_V1
    assert len(packet) == ENCRYPTED_DATA_HEADER_LEN + len(b"encoded-frame") + 16
    decrypted = decrypt_packet_without_replay(key, packet)
    assert decrypted.receiver_index == 42
    assert decrypted.counter == 9
    assert decrypted.plaintext == b"encoded-frame"

    tampered = bytearray(packet)
    tampered[-1] ^= 1
    with pytest.raises(ValueError):
        decrypt_packet_without_replay(key, bytes(tampered))


def test_transport_keys_reject_replayed_packet() -> None:
    client_to_server = bytes([1]) * 32
    server_to_client = bytes([2]) * 32
    client = TransportKeys(receiver_index=100, send_key=client_to_server, receive_key=server_to_client)
    server = TransportKeys(receiver_index=100, send_key=server_to_client, receive_key=client_to_server)

    packet = client.encrypt_frame(b"frame")
    assert server.decrypt_packet(packet).plaintext == b"frame"
    with pytest.raises(ValueError):
        server.decrypt_packet(packet)


def test_transport_keys_support_distinct_local_and_remote_receiver_indexes() -> None:
    client_to_server = bytes([1]) * 32
    server_to_client = bytes([2]) * 32
    client = TransportKeys(
        receiver_index=20,
        local_receiver_index=10,
        remote_receiver_index=20,
        send_key=client_to_server,
        receive_key=server_to_client,
    )
    server = TransportKeys(
        receiver_index=10,
        local_receiver_index=20,
        remote_receiver_index=10,
        send_key=server_to_client,
        receive_key=client_to_server,
    )

    packet = client.encrypt_frame(b"frame")
    assert decrypt_packet_without_replay(client_to_server, packet).receiver_index == 20
    assert server.decrypt_packet(packet).plaintext == b"frame"


def test_replay_window_accepts_out_of_order_once_inside_window() -> None:
    window = ReplayWindow(window_bits=8)
    assert window.accept(10)
    assert window.accept(12)
    assert window.accept(11)
    assert not window.accept(11)
    assert not window.accept(3)
