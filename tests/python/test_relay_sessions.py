from datetime import UTC, datetime, timedelta

import pytest
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import ProvisionedNode, TopologyBundleBody
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.relay_sessions import RelayNextHop, authorize_relay_session, compile_relay_executor_configs


def _topology(
    upstream: NodeIdentity,
    relay: NodeIdentity,
    downstream: NodeIdentity,
    *,
    revoked: list[str] | None = None,
) -> TopologyBundleBody:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return TopologyBundleBody(
        generation=4,
        issuer_node_id=IdentityPublicRecord.from_identity(upstream).node_id,
        created_at=now,
        valid_from=now,
        nodes=[
            ProvisionedNode(name="upstream", identity=IdentityPublicRecord.from_identity(upstream)),
            ProvisionedNode(name="relay", identity=IdentityPublicRecord.from_identity(relay), roles=["node", "relay"]),
            ProvisionedNode(name="downstream", identity=IdentityPublicRecord.from_identity(downstream)),
        ],
        revoked_node_ids=revoked or [],
    )


def test_relay_session_authorization_compiles_explicit_hop_state() -> None:
    upstream = NodeIdentity.generate()
    relay = NodeIdentity.generate()
    downstream = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    authorization = authorize_relay_session(
        topology=_topology(upstream, relay, downstream),
        upstream_peer=IdentityPublicRecord.from_identity(upstream),
        relay=IdentityPublicRecord.from_identity(relay),
        next_hop=RelayNextHop(
            peer_node_id=IdentityPublicRecord.from_identity(downstream).node_id,
            endpoint="udp:203.0.113.10:51820",
            receiver_index=77,
        ),
        direction="upstream_to_downstream",
        relay_receiver_index=55,
        now=now,
        max_packet_size=1400,
    )

    assert authorization.relay_receiver_index == 55
    assert authorization.topology_generation == 4
    assert authorization.allows_packet(packet_size=1200, now=now)
    assert not authorization.allows_packet(packet_size=1600, now=now)
    assert authorization.is_expired(now + timedelta(seconds=121))
    executor_config = authorization.export_executor_config()
    assert executor_config == {
        "relay_receiver_index": 55,
        "next_hop_transport": "udp",
        "next_hop_address": "203.0.113.10:51820",
        "next_hop_receiver_index": 77,
        "direction": "upstream_to_downstream",
        "topology_generation": 4,
        "expires_at_unix_us": 1767225720000000,
        "max_packet_size": 1400,
    }
    assert "service_id" not in executor_config
    assert "path_id" not in executor_config
    assert "next_hop_endpoint" not in executor_config


def test_relay_session_rejects_non_relay_or_revoked_nodes() -> None:
    upstream = NodeIdentity.generate()
    relay = NodeIdentity.generate()
    downstream = NodeIdentity.generate()
    not_relay_topology = TopologyBundleBody(
        generation=1,
        issuer_node_id=IdentityPublicRecord.from_identity(upstream).node_id,
        nodes=[
            ProvisionedNode(name="upstream", identity=IdentityPublicRecord.from_identity(upstream)),
            ProvisionedNode(name="relay", identity=IdentityPublicRecord.from_identity(relay)),
            ProvisionedNode(name="downstream", identity=IdentityPublicRecord.from_identity(downstream)),
        ],
    )

    with pytest.raises(ValueError, match="relay role"):
        authorize_relay_session(
            topology=not_relay_topology,
            upstream_peer=IdentityPublicRecord.from_identity(upstream),
            relay=IdentityPublicRecord.from_identity(relay),
            next_hop=RelayNextHop(
                peer_node_id=IdentityPublicRecord.from_identity(downstream).node_id,
                endpoint="udp:203.0.113.10:51820",
                receiver_index=77,
            ),
            direction="upstream_to_downstream",
            relay_receiver_index=55,
        )

    revoked_topology = _topology(
        upstream, relay, downstream, revoked=[IdentityPublicRecord.from_identity(downstream).node_id]
    )
    with pytest.raises(ValueError, match="revoked"):
        authorize_relay_session(
            topology=revoked_topology,
            upstream_peer=IdentityPublicRecord.from_identity(upstream),
            relay=IdentityPublicRecord.from_identity(relay),
            next_hop=RelayNextHop(
                peer_node_id=IdentityPublicRecord.from_identity(downstream).node_id,
                endpoint="udp:203.0.113.10:51820",
                receiver_index=77,
            ),
            direction="upstream_to_downstream",
            relay_receiver_index=55,
        )


def test_relay_executor_compilation_omits_expired_sessions() -> None:
    upstream = NodeIdentity.generate()
    relay = NodeIdentity.generate()
    downstream = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    authorization = authorize_relay_session(
        topology=_topology(upstream, relay, downstream),
        upstream_peer=IdentityPublicRecord.from_identity(upstream),
        relay=IdentityPublicRecord.from_identity(relay),
        next_hop=RelayNextHop(
            peer_node_id=IdentityPublicRecord.from_identity(downstream).node_id,
            endpoint="udp:[2001:db8::10]:51820",
            receiver_index=77,
        ),
        direction="upstream_to_downstream",
        relay_receiver_index=55,
        now=now,
        lifetime_seconds=5,
    )

    active = compile_relay_executor_configs([authorization], now=now)
    expired = compile_relay_executor_configs([authorization], now=now + timedelta(seconds=6))

    assert len(active) == 1
    assert active[0].next_hop_transport == "udp"
    assert active[0].next_hop_address == "[2001:db8::10]:51820"
    assert expired == []
