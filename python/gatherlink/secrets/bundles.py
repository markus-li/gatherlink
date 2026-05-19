"""Signed canonical control-plane document helpers."""

from __future__ import annotations

import json
from base64 import b64decode, b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cbor2

from gatherlink.persistence.store import atomic_write_json
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

    def save(self, path: Path, *, force: bool = False, mode: int = 0o644) -> None:
        """Persist a signed control-plane document atomically."""
        if path.exists() and not force:
            raise FileExistsError(f"{path} already exists")
        atomic_write_json(path, self.export_dict(), mode=mode)

    @classmethod
    def load(cls, path: Path) -> SignedDocument:
        """Load and verify a signed control-plane document from disk."""
        try:
            document = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise FileNotFoundError(path) from exc
        document.verify()
        return document


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
