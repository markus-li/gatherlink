from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import ProvisionedNode, TopologyBundleBody
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.rekey_control import (
    RekeyControlMessage,
    accept_rekey_initiation_payload,
    complete_rekey_response_payload,
    create_rekey_initiation,
    create_rekey_reject,
)
from gatherlink.security.sessions import plan_authenticated_static_session


def _topology(local: NodeIdentity, peer: NodeIdentity, now: datetime, *, generation: int = 7) -> TopologyBundleBody:
    return TopologyBundleBody(
        generation=generation,
        issuer_node_id=IdentityPublicRecord.from_identity(local).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="local", identity=IdentityPublicRecord.from_identity(local)),
            ProvisionedNode(name="peer", identity=IdentityPublicRecord.from_identity(peer)),
        ],
    )


def _current_sessions(
    local: NodeIdentity,
    peer: NodeIdentity,
    topology: TopologyBundleBody,
    now: datetime,
) -> tuple:
    initiator = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    responder = plan_authenticated_static_session(
        peer,
        IdentityPublicRecord.from_identity(local),
        topology,
        role="responder",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    return initiator, responder


def test_rekey_control_round_trip_compiles_inverse_replacement_sessions() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(initiator, responder, now)
    initiator_current, responder_current = _current_sessions(initiator, responder, topology, now)

    pending = create_rekey_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        initiator_current,
        receiver_index=222,
        now=now + timedelta(seconds=90),
    )
    accepted = accept_rekey_initiation_payload(
        responder,
        pending.encode(),
        topology,
        responder_current,
        receiver_index=333,
        now=now + timedelta(seconds=91),
    )
    initiator_replacement = complete_rekey_response_payload(
        initiator,
        pending,
        accepted.encode(),
        topology,
        initiator_current,
        now=now + timedelta(seconds=92),
    )

    assert initiator_replacement.security is not None
    assert accepted.session.security is not None
    assert initiator_replacement.security.local_receiver_index == 222
    assert initiator_replacement.security.remote_receiver_index == 333
    assert accepted.session.security.local_receiver_index == 333
    assert accepted.session.security.remote_receiver_index == 222
    assert initiator_replacement.security.send_key == accepted.session.security.receive_key
    assert initiator_replacement.security.receive_key == accepted.session.security.send_key


def test_rekey_control_rejects_stale_receiver_index_before_noise_work() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(initiator, responder, now)
    initiator_current, responder_current = _current_sessions(initiator, responder, topology, now)
    pending = create_rekey_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        initiator_current,
        receiver_index=222,
        now=now + timedelta(seconds=90),
    )
    decoded = RekeyControlMessage.decode(pending.encode())
    stale = decoded.__class__(
        **{
            **decoded.__dict__,
            "current_receiver_index": decoded.current_receiver_index + 1,
        }
    )

    with pytest.raises(ValueError, match="receiver index is stale"):
        accept_rekey_initiation_payload(
            responder,
            stale.encode(),
            topology,
            responder_current,
            receiver_index=333,
            now=now + timedelta(seconds=91),
        )


def test_rekey_control_rejects_wrong_peer_and_topology() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    wrong = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(initiator, responder, now)
    initiator_current, responder_current = _current_sessions(initiator, responder, topology, now)

    with pytest.raises(ValueError, match="peer identity"):
        create_rekey_initiation(
            initiator,
            IdentityPublicRecord.from_identity(wrong),
            topology,
            initiator_current,
            now=now,
        )

    pending = create_rekey_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        initiator_current,
        receiver_index=222,
        now=now,
    )
    decoded = RekeyControlMessage.decode(pending.encode())
    stale_topology = decoded.__class__(
        **{
            **decoded.__dict__,
            "topology_generation": decoded.topology_generation + 1,
        }
    )

    with pytest.raises(ValueError, match="topology generation is stale"):
        accept_rekey_initiation_payload(responder, stale_topology.encode(), topology, responder_current, now=now)


def test_rekey_reject_message_is_bounded_and_keyless() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(initiator, responder, now)
    current, _responder_current = _current_sessions(initiator, responder, topology, now)

    reject = create_rekey_reject(
        initiator,
        IdentityPublicRecord.from_identity(responder).node_id,
        current,
        "x" * 400,
        now=now,
    )
    decoded = RekeyControlMessage.decode(reject.encode())

    assert decoded.message_type == "rekey_reject"
    assert decoded.noise is None
    assert decoded.reason == "x" * 255
    assert b"send_key" not in reject.encode()
    assert b"receive_key" not in reject.encode()
