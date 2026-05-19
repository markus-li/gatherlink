from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidSignature
from gatherlink.secrets.bundles import canonical_cbor, sign_document
from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord
from gatherlink.security.envelope import (
    ENCRYPTED_DATA_HEADER_LEN,
    PACKET_TYPE_ENCRYPTED_DATA_V1,
    TransportKeys,
    decrypt_packet_without_replay,
    encrypt_frame_with_counter,
)
from gatherlink.security.keys import NodeIdentity, node_id_from_ed25519_public, verify_document, x25519_shared_secret
from gatherlink.security.replay import ReplayWindow
from gatherlink.security.sessions import derive_directional_session_keys, derive_static_transport_security


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


def test_replay_window_accepts_out_of_order_once_inside_window() -> None:
    window = ReplayWindow(window_bits=8)
    assert window.accept(10)
    assert window.accept(12)
    assert window.accept(11)
    assert not window.accept(11)
    assert not window.accept(3)
