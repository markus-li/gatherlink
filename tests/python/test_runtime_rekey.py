from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.runtime.rekey import (
    LiveRekeyCoordinator,
    accept_runtime_rekey_initiation,
    authenticated_session_from_runtime_config,
    complete_runtime_rekey_response,
    create_runtime_rekey_initiation,
    evaluate_rekey_from_status,
    live_rekey_context_from_runtime,
    runtime_security_from_session,
)
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


def test_rekey_evaluation_reads_real_runtime_service_stats_shape() -> None:
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
    session = replace(session, rekey_after_packets=20)
    bus = DiagnosticsBus()

    result = evaluate_rekey_from_status(
        session,
        _runtime(),
        {
            "services": ["udp-main"],
            "service_stats": {"udp-main": {"packets": 80, "bytes": 20480}},
            "topology_generation": 7,
            "peer_node_id": IdentityPublicRecord.from_identity(peer).node_id,
        },
        now=now + timedelta(seconds=5),
        diagnostics=bus,
    )

    assert result.decision.should_rekey
    assert result.decision.reason == "session volume limit reached"
    assert bus._events[0].code == "rekey.started"


def test_runtime_rekey_control_round_trip_hot_applies_both_sides() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(initiator, responder, now)
    initiator_current = plan_authenticated_static_session(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    responder_current = plan_authenticated_static_session(
        responder,
        IdentityPublicRecord.from_identity(initiator),
        topology,
        role="responder",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    initiator_bus = DiagnosticsBus()
    responder_bus = DiagnosticsBus()

    pending = create_runtime_rekey_initiation(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        initiator_current,
        receiver_index=222,
        now=now + timedelta(seconds=20),
    )
    response = accept_runtime_rekey_initiation(
        responder,
        pending.payload,
        topology,
        responder_current,
        _runtime(),
        {},
        receiver_index=333,
        now=now + timedelta(seconds=21),
        diagnostics=responder_bus,
    )
    initiator_result = complete_runtime_rekey_response(
        initiator,
        pending.pending,
        response.response_payload,
        topology,
        initiator_current,
        _runtime(),
        {},
        now=now + timedelta(seconds=22),
        diagnostics=initiator_bus,
    )

    assert response.runtime_result.applied is True
    assert initiator_result.applied is True
    assert initiator_result.runtime_config.security.local_receiver_index == 222
    assert response.runtime_result.runtime_config.security.local_receiver_index == 333
    assert (
        initiator_result.runtime_config.security.send_key == response.runtime_result.runtime_config.security.receive_key
    )
    assert initiator_bus._events[0].code == "rekey.succeeded"
    assert responder_bus._events[0].code == "rekey.succeeded"


def test_live_rekey_coordinator_suppresses_duplicate_start_and_hot_applies() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(initiator, responder, now)
    initiator_current = plan_authenticated_static_session(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    responder_current = plan_authenticated_static_session(
        responder,
        IdentityPublicRecord.from_identity(initiator),
        topology,
        role="responder",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    initiator_coordinator = LiveRekeyCoordinator()
    responder_coordinator = LiveRekeyCoordinator()

    outbound = initiator_coordinator.maybe_start(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        initiator_current,
        _runtime(),
        {"topology_generation": 7, "peer_node_id": IdentityPublicRecord.from_identity(responder).node_id},
        now=now + timedelta(seconds=100),
    )
    duplicate = initiator_coordinator.maybe_start(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        initiator_current,
        _runtime(),
        {"topology_generation": 7, "peer_node_id": IdentityPublicRecord.from_identity(responder).node_id},
        now=now + timedelta(seconds=101),
    )

    assert outbound is not None
    assert outbound.message_type == "rekey_initiation"
    assert duplicate is None

    responder_result = responder_coordinator.handle_peer_payload(
        responder,
        outbound.payload,
        topology,
        responder_current,
        _runtime(),
        {},
        now=now + timedelta(seconds=102),
    )
    assert responder_result.accepted is True
    assert responder_result.runtime_result is not None
    assert responder_result.runtime_result.applied is True
    assert responder_result.outbound is not None
    assert responder_result.outbound.message_type == "rekey_response"

    initiator_result = initiator_coordinator.handle_peer_payload(
        initiator,
        responder_result.outbound.payload,
        topology,
        initiator_current,
        _runtime(),
        {},
        now=now + timedelta(seconds=103),
    )

    assert initiator_result.accepted is True
    assert initiator_result.runtime_result is not None
    assert initiator_result.runtime_result.applied is True
    assert (
        initiator_result.runtime_result.runtime_config.security.send_key
        == responder_result.runtime_result.runtime_config.security.receive_key
    )


def test_live_rekey_coordinator_rejects_invalid_initiation_with_bounded_payload() -> None:
    initiator = NodeIdentity.generate()
    responder = NodeIdentity.generate()
    wrong = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(initiator, responder, now)
    initiator_current = plan_authenticated_static_session(
        initiator,
        IdentityPublicRecord.from_identity(responder),
        topology,
        role="initiator",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    responder_current = plan_authenticated_static_session(
        responder,
        IdentityPublicRecord.from_identity(initiator),
        topology,
        role="responder",
        receiver_index=100,
        now=now,
        lifetime_seconds=120,
    )
    bad_pending = create_runtime_rekey_initiation(
        wrong,
        IdentityPublicRecord.from_identity(responder),
        _topology(wrong, responder, now),
        plan_authenticated_static_session(
            wrong,
            IdentityPublicRecord.from_identity(responder),
            _topology(wrong, responder, now),
            role="initiator",
            receiver_index=100,
            now=now,
            lifetime_seconds=120,
        ),
        now=now + timedelta(seconds=100),
    )
    bus = DiagnosticsBus()

    result = LiveRekeyCoordinator().handle_peer_payload(
        responder,
        bad_pending.payload,
        topology,
        responder_current,
        _runtime(),
        {},
        now=now + timedelta(seconds=101),
        diagnostics=bus,
    )

    assert result.accepted is False
    assert result.outbound is not None
    assert result.outbound.message_type == "rekey_reject"
    assert b"send_key" not in result.outbound.payload
    assert b"receive_key" not in result.outbound.payload
    assert bus._events[0].code == "rekey.rejected"
    assert initiator_current.receiver_index == 100


def test_authenticated_session_from_runtime_config_uses_keyless_metadata() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(local, peer, now)
    session = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="initiator",
        receiver_index=123,
        now=now,
        lifetime_seconds=120,
    )
    runtime = expand_config(
        GatherlinkConfig(
            schema_version=1,
            node="local",
            role="client",
            peer="peer",
            paths=[PathConfig(name="path-a", interface="lo")],
            services=[ServiceConfig(name="udp-main", listen="127.0.0.1:0", target="127.0.0.1:51820")],
            security=session.export_config(),
        )
    )

    reconstructed = authenticated_session_from_runtime_config(runtime)

    assert reconstructed is not None
    assert reconstructed.local_node_id == session.local_node_id
    assert reconstructed.peer_node_id == session.peer_node_id
    assert reconstructed.topology_generation == session.topology_generation
    assert reconstructed.receiver_index == session.security.receiver_index
    assert reconstructed.security is not None
    assert reconstructed.security.send_key == session.security.send_key
    assert reconstructed.security.receive_key == session.security.receive_key


def test_runtime_security_from_replacement_preserves_rekey_metadata() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(local, peer, now)
    session = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="initiator",
        receiver_index=123,
        now=now,
        lifetime_seconds=120,
    )
    runtime = _runtime().model_copy(update={"security": runtime_security_from_session(session)})

    reconstructed = authenticated_session_from_runtime_config(runtime)

    assert reconstructed is not None
    assert reconstructed.local_node_id == session.local_node_id
    assert reconstructed.peer_node_id == session.peer_node_id
    assert reconstructed.topology_generation == session.topology_generation
    assert reconstructed.role == session.role
    assert reconstructed.expires_at == session.expires_at


def test_live_rekey_context_from_runtime_requires_matching_identity_and_topology() -> None:
    local = NodeIdentity.generate()
    peer = NodeIdentity.generate()
    wrong_peer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    topology = _topology(local, peer, now)
    session = plan_authenticated_static_session(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        role="initiator",
        receiver_index=123,
        now=now,
        lifetime_seconds=120,
    )
    runtime = _runtime().model_copy(update={"security": runtime_security_from_session(session)})

    context = live_rekey_context_from_runtime(
        local,
        IdentityPublicRecord.from_identity(peer),
        topology,
        runtime,
    )

    assert context.current_session.peer_node_id == IdentityPublicRecord.from_identity(peer).node_id
    try:
        live_rekey_context_from_runtime(
            local,
            IdentityPublicRecord.from_identity(wrong_peer),
            topology,
            runtime,
        )
    except ValueError as exc:
        assert "peer identity does not match" in str(exc)
    else:
        raise AssertionError("wrong peer identity should be rejected")


def test_authenticated_session_from_runtime_config_ignores_legacy_blocks_without_metadata() -> None:
    runtime = _runtime()

    assert authenticated_session_from_runtime_config(runtime) is None


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
