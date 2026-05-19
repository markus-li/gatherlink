"""Signed topology/provisioning bundle models owned by the Python control plane."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from gatherlink.secrets.bundles import SignedDocument, sign_document
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.security.keys import NodeIdentity
from gatherlink.shared.models import GatherlinkBaseModel

TOPOLOGY_DOMAIN = "GATHERLINK_TOPOLOGY_V1"


class ProvisionedNode(GatherlinkBaseModel):
    """One node identity and role carried by a signed topology bundle."""

    name: str
    identity: IdentityPublicRecord
    roles: list[Literal["node", "relay", "exit", "helper"]] = Field(default_factory=lambda: ["node"])


class ProvisionedService(GatherlinkBaseModel):
    """One service authorization carried by a signed topology bundle."""

    name: str
    owner_node: str
    service_id: int = Field(ge=256, le=65535)
    protocol: Literal["udp"] = "udp"


class TopologyBundleBody(GatherlinkBaseModel):
    """Canonical signed topology/provisioning body for static v1 deployments."""

    schema_version: int = 1
    generation: int = Field(ge=1)
    issuer_node_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    nodes: list[ProvisionedNode] = Field(default_factory=list)
    services: list[ProvisionedService] = Field(default_factory=list)
    revoked_node_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_topology(self) -> TopologyBundleBody:
        """Validate relationships inside the signed topology body."""
        if self.schema_version != 1:
            raise ValueError("topology schema_version must be 1")
        node_names = {node.name for node in self.nodes}
        if len(node_names) != len(self.nodes):
            raise ValueError("topology node names must be unique")
        node_ids = [node.identity.node_id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("topology node identities must be unique")
        service_ids = [service.service_id for service in self.services]
        if len(set(service_ids)) != len(service_ids):
            raise ValueError("topology service ids must be unique")
        unknown_owners = [service.owner_node for service in self.services if service.owner_node not in node_names]
        if unknown_owners:
            raise ValueError(f"topology services reference unknown nodes: {', '.join(sorted(set(unknown_owners)))}")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("topology valid_until must be after valid_from")
        return self

    def is_valid_at(self, now: datetime) -> bool:
        """Return whether this bundle is inside its validity window."""
        if now < self.valid_from:
            return False
        return self.valid_until is None or now <= self.valid_until


def sign_topology_bundle(identity: NodeIdentity, body: TopologyBundleBody) -> SignedDocument:
    """Sign a topology bundle after binding the issuer to the signing identity."""
    if body.issuer_node_id != IdentityPublicRecord.from_identity(identity).node_id:
        raise ValueError("topology issuer_node_id must match signer identity")
    return sign_document(identity, TOPOLOGY_DOMAIN, body.model_dump(mode="json"))


def load_verified_topology_bundle(
    document: SignedDocument,
    *,
    trusted_issuer: IdentityPublicRecord,
    now: datetime | None = None,
    minimum_generation: int = 1,
) -> TopologyBundleBody:
    """Verify signature, issuer, generation, and validity for a topology bundle."""
    if document.domain != TOPOLOGY_DOMAIN:
        raise ValueError(f"expected {TOPOLOGY_DOMAIN}, got {document.domain}")
    document.verify()
    if document.signer_node_id != trusted_issuer.public_bytes()[0]:
        raise ValueError("topology signer is not the trusted issuer")
    if document.signer_public_key != trusted_issuer.public_bytes()[1]:
        raise ValueError("topology signer public key does not match trusted issuer")
    body = TopologyBundleBody.model_validate(document.body)
    if body.issuer_node_id != trusted_issuer.node_id:
        raise ValueError("topology issuer_node_id does not match trusted issuer")
    if body.generation < minimum_generation:
        raise ValueError("topology generation is stale")
    if not body.is_valid_at(now or datetime.now(UTC)):
        raise ValueError("topology bundle is outside its validity window")
    return body
