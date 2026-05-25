from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.runtime.rekey import evaluate_rekey_from_status
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import ProvisionedNode, TopologyBundleBody
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.sessions import plan_authenticated_static_session


def _topology(local: NodeIdentity, peer: NodeIdentity, now: datetime) -> TopologyBundleBody:
    return TopologyBundleBody(
        generation=7,
        issuer_node_id=IdentityPublicRecord.from_identity(local).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="local", identity=IdentityPublicRecord.from_identity(local)),
            ProvisionedNode(name="peer", identity=IdentityPublicRecord.from_identity(peer)),
        ],
    )


def _runtime():
    return expand_config(
        GatherlinkConfig(
            schema_version=1,
            node="local",
            role="client",
            peer="peer",
            paths=[PathConfig(name="path-a", interface="lo")],
            services=[ServiceConfig(name="udp-main", listen="127.0.0.1:0", target="127.0.0.1:51820")],
        )
    )


def test_rekey_evaluation_starts_before_expiry_and_emits_diagnostic() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        _topology(local, peer, now),
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    bus = DiagnosticsBus()

    result = evaluate_rekey_from_status(
        session,
        _runtime(),
        {"topology_generation": 7, "peer_node_id": IdentityPublicRecord.from_identity(peer).node_id},
        now=now + timedelta(seconds=100),
        diagnostics=bus,
    )

    assert result.decision.should_rekey
    assert result.applied is False
    assert result.fail_closed is False
    assert bus.queued_events == 1
    event = bus._events[0]
    assert event.code == "rekey.started"
    assert event.details["current_receiver_index"] == 100


def test_rekey_evaluation_hot_applies_valid_replacement_security() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(local, peer, now)
    current = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    replacement = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="initiator",
        receiver_index=101,
        now=now + timedelta(seconds=100),
        lifetime_seconds=120,
    )
    bus = DiagnosticsBus()

    result = evaluate_rekey_from_status(
        current,
        _runtime(),
        {"services": {"udp-main": {"packets": 10, "bytes": 500}}},
        replacement_session=replacement,
        now=now + timedelta(seconds=100),
        diagnostics=bus,
    )

    assert result.applied is True
    assert result.runtime_config.security.source_mode == "authenticated"
    assert result.runtime_config.security.send_key == replacement.security.send_key
    assert result.runtime_config.security.receive_key == replacement.security.receive_key
    assert bus._events[0].code == "rekey.succeeded"


def test_rekey_evaluation_fails_closed_for_stale_or_invalid_replacement() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    other = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(local, peer, now)
    current = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    bad_topology = _topology(local, other, now)
    stale_replacement = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(other),
        bad_topology,
        role="initiator",
        receiver_index=101,
        now=now,
        lifetime_seconds=120,
    )
    bus = DiagnosticsBus()

    result = evaluate_rekey_from_status(
        current,
        _runtime(),
        {},
        replacement_session=stale_replacement,
        now=now + timedelta(seconds=100),
        diagnostics=bus,
    )

    assert result.applied is False
    assert result.fail_closed is True
    assert result.decision.action == "reject"
    assert bus._events[0].code == "rekey.rejected"


def test_rekey_evaluation_fails_closed_when_current_session_expired() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        _topology(local, peer, now),
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=30,
    )
    bus = DiagnosticsBus()

    result = evaluate_rekey_from_status(session, _runtime(), {}, now=now + timedelta(seconds=31), diagnostics=bus)

    assert result.fail_closed is True
    assert result.decision.action == "expired"
    assert bus._events[0].code == "rekey.expired"
