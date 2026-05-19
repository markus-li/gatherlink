"""Signed canonical control-plane document helpers."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from typing import Any

import cbor2

from gatherlink.security.keys import NodeIdentity, verify_document


@dataclass(frozen=True)
class SignedDocument:
    """A signed canonical CBOR document with explicit domain separation."""

    domain: str
    body: dict[str, Any]
    signer_node_id: bytes
    signer_public_key: bytes
    signature: bytes

    def verify(self) -> None:
        """Verify the signature against the canonical body."""
        verify_document(
            self.signer_public_key,
            self.domain.encode("ascii"),
            canonical_cbor(self.body),
            self.signature,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignedDocument:
        """Load a signed document from JSON-decoded data."""
        return cls(
            domain=str(data["domain"]),
            body=dict(data["body"]),
            signer_node_id=b64decode(str(data["signer_node_id"])),
            signer_public_key=b64decode(str(data["signer_public_key"])),
            signature=b64decode(str(data["signature"])),
        )

    def export_dict(self) -> dict[str, Any]:
        """Return a stable JSON-friendly signed document mapping."""
        return {
            "domain": self.domain,
            "body": self.body,
            "signer_node_id": b64encode(self.signer_node_id).decode("ascii"),
            "signer_public_key": b64encode(self.signer_public_key).decode("ascii"),
            "signature": b64encode(self.signature).decode("ascii"),
        }


def canonical_cbor(body: dict[str, Any]) -> bytes:
    """Return deterministic CBOR bytes for a signed document body."""
    return cbor2.dumps(body, canonical=True)


def sign_document(identity: NodeIdentity, domain: str, body: dict[str, Any]) -> SignedDocument:
    """Sign one canonical CBOR document with explicit domain separation."""
    signature = identity.sign(domain.encode("ascii"), canonical_cbor(body))
    return SignedDocument(
        domain=domain,
        body=body,
        signer_node_id=identity.node_id,
        signer_public_key=identity.ed25519_public,
        signature=signature,
    )
