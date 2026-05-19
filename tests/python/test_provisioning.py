from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidSignature
from gatherlink.secrets.bundles import SignedDocument
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import (
    ProvisionedNode,
    ProvisionedService,
    TopologyBundleBody,
    diff_topology_bundles,
    load_verified_topology_bundle,
    sign_topology_bundle,
)
from gatherlink.security.keys import NodeIdentity


def test_signed_topology_bundle_verifies_with_trusted_issuer() -> None:
    issuer = NodeIdentity.generate()
    node = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    body = TopologyBundleBody(
        generation=3,
        issuer_node_id=IdentityPublicRecord.from_identity(issuer).node_id,
        created_at=now,
        valid_from=now,
        valid_until=now + timedelta(hours=1),
        nodes=[ProvisionedNode(name="node-a", identity=IdentityPublicRecord.from_identity(node))],
        services=[ProvisionedService(name="wireguard", owner_node="node-a", service_id=256)],
    )

    document = sign_topology_bundle(issuer, body)
    loaded = load_verified_topology_bundle(
        document,
        trusted_issuer=IdentityPublicRecord.from_identity(issuer),
        now=now,
        minimum_generation=3,
    )

    assert loaded.generation == 3
    assert loaded.nodes[0].name == "node-a"


def test_topology_bundle_rejects_tampering() -> None:
    issuer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    body = TopologyBundleBody(
        generation=1,
        issuer_node_id=IdentityPublicRecord.from_identity(issuer).node_id,
        created_at=now,
        valid_from=now,
    )
    document = sign_topology_bundle(issuer, body)
    tampered = SignedDocument(
        domain=document.domain,
        body={**document.body, "generation": 2},
        signer_node_id=document.signer_node_id,
        signer_public_key=document.signer_public_key,
        signature=document.signature,
    )

    with pytest.raises(InvalidSignature):
        load_verified_topology_bundle(tampered, trusted_issuer=IdentityPublicRecord.from_identity(issuer), now=now)


def test_topology_bundle_rejects_stale_generation_and_expiry() -> None:
    issuer = NodeIdentity.generate()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    body = TopologyBundleBody(
        generation=1,
        issuer_node_id=IdentityPublicRecord.from_identity(issuer).node_id,
        created_at=now,
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),
    )
    document = sign_topology_bundle(issuer, body)

    with pytest.raises(ValueError, match="stale"):
        load_verified_topology_bundle(
            document,
            trusted_issuer=IdentityPublicRecord.from_identity(issuer),
            now=now,
            minimum_generation=2,
        )
    with pytest.raises(ValueError, match="validity"):
        load_verified_topology_bundle(
            document,
            trusted_issuer=IdentityPublicRecord.from_identity(issuer),
            now=now,
            minimum_generation=1,
        )


def test_topology_bundle_rejects_duplicate_services_and_unknown_owner() -> None:
    issuer = NodeIdentity.generate()
    issuer_id = IdentityPublicRecord.from_identity(issuer).node_id

    with pytest.raises(ValueError, match="unknown nodes"):
        TopologyBundleBody(
            generation=1,
            issuer_node_id=issuer_id,
            services=[ProvisionedService(name="dns", owner_node="missing", service_id=256)],
        )

    node = ProvisionedNode(name="node-a", identity=IdentityPublicRecord.from_identity(NodeIdentity.generate()))
    with pytest.raises(ValueError, match="service ids"):
        TopologyBundleBody(
            generation=1,
            issuer_node_id=issuer_id,
            nodes=[node],
            services=[
                ProvisionedService(name="dns", owner_node="node-a", service_id=256),
                ProvisionedService(name="wg", owner_node="node-a", service_id=256),
            ],
        )


def test_topology_bundle_diff_explains_added_removed_and_revoked_facts() -> None:
    issuer = NodeIdentity.generate()
    issuer_id = IdentityPublicRecord.from_identity(issuer).node_id
    node_a = ProvisionedNode(name="node-a", identity=IdentityPublicRecord.from_identity(NodeIdentity.generate()))
    node_b = ProvisionedNode(name="node-b", identity=IdentityPublicRecord.from_identity(NodeIdentity.generate()))
    current = TopologyBundleBody(
        generation=3,
        issuer_node_id=issuer_id,
        nodes=[node_a],
        services=[ProvisionedService(name="wireguard", owner_node="node-a", service_id=256)],
    )
    candidate = TopologyBundleBody(
        generation=4,
        issuer_node_id=issuer_id,
        nodes=[node_b],
        services=[ProvisionedService(name="dns", owner_node="node-b", service_id=257)],
        revoked_node_ids=[node_a.identity.node_id],
    )

    diff = diff_topology_bundles(current, candidate)

    assert diff.ok_to_install
    assert diff.added_nodes == ["node-b"]
    assert diff.removed_nodes == ["node-a"]
    assert diff.added_services == ["dns"]
    assert diff.removed_services == ["wireguard"]
    assert diff.revoked_nodes == [node_a.identity.node_id]


def test_topology_bundle_diff_rejects_non_forward_generation() -> None:
    issuer = NodeIdentity.generate()
    issuer_id = IdentityPublicRecord.from_identity(issuer).node_id
    current = TopologyBundleBody(generation=3, issuer_node_id=issuer_id)
    candidate = TopologyBundleBody(generation=3, issuer_node_id=issuer_id)

    diff = diff_topology_bundles(current, candidate)

    assert not diff.ok_to_install
    assert diff.warnings == ["candidate_generation_not_newer"]
