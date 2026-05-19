"""Serializable node identity records for provisioning and local secrets."""

from __future__ import annotations

import json
from base64 import b64decode, b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gatherlink.persistence.store import atomic_write_json, load_secret_json, redact_secrets
from gatherlink.security.keys import NodeIdentity


@dataclass(frozen=True)
class IdentityRecord:
    """JSON-friendly identity record for at-rest storage or provisioning bundles."""

    schema_version: int
    node_id: str
    ed25519_public: str
    ed25519_private: str
    x25519_public: str
    x25519_private: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IdentityRecord:
        """Load an identity record from JSON-decoded data."""
        return cls(
            schema_version=int(data["schema_version"]),
            node_id=str(data["node_id"]),
            ed25519_public=str(data["ed25519_public"]),
            ed25519_private=str(data["ed25519_private"]),
            x25519_public=str(data["x25519_public"]),
            x25519_private=str(data["x25519_private"]),
        )

    @classmethod
    def from_identity(cls, identity: NodeIdentity) -> IdentityRecord:
        """Create a serializable record from in-memory key material."""
        return cls(
            schema_version=1,
            node_id=_b64(identity.node_id),
            ed25519_public=_b64(identity.ed25519_public),
            ed25519_private=_b64(identity.ed25519_private),
            x25519_public=_b64(identity.x25519_public),
            x25519_private=_b64(identity.x25519_private),
        )

    def to_identity(self) -> NodeIdentity:
        """Load an in-memory identity from this record."""
        if self.schema_version != 1:
            raise ValueError(f"unsupported identity schema_version: {self.schema_version}")
        identity = NodeIdentity.from_private_keys(
            b64decode(self.ed25519_private),
            b64decode(self.x25519_private),
        )
        if identity.node_id != b64decode(self.node_id):
            raise ValueError("identity record node_id does not match public key")
        return identity

    def export_dict(self) -> dict[str, int | str]:
        """Return a stable JSON-friendly mapping."""
        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "ed25519_public": self.ed25519_public,
            "ed25519_private": self.ed25519_private,
            "x25519_public": self.x25519_public,
            "x25519_private": self.x25519_private,
        }

    def export_redacted_dict(self) -> dict[str, Any]:
        """Return an operator-safe identity summary without private key material."""
        return redact_secrets(self.export_dict())

    def save(self, path: Path, *, force: bool = False) -> None:
        """Persist private identity material atomically with owner-only permissions."""
        if path.exists() and not force:
            raise FileExistsError(f"{path} already exists")
        atomic_write_json(path, self.export_dict(), mode=0o600)

    @classmethod
    def load(cls, path: Path) -> IdentityRecord:
        """Load a private identity record from disk."""
        return cls.from_dict(load_secret_json(path))


@dataclass(frozen=True)
class IdentityPublicRecord:
    """JSON-friendly public identity record safe to share with peers."""

    schema_version: int
    node_id: str
    ed25519_public: str
    x25519_public: str

    @classmethod
    def from_identity(cls, identity: NodeIdentity) -> IdentityPublicRecord:
        """Create a public identity record from in-memory key material."""
        return cls(
            schema_version=1,
            node_id=_b64(identity.node_id),
            ed25519_public=_b64(identity.ed25519_public),
            x25519_public=_b64(identity.x25519_public),
        )

    @classmethod
    def from_identity_record(cls, record: IdentityRecord) -> IdentityPublicRecord:
        """Create a public identity record from a private identity record."""
        return cls(
            schema_version=record.schema_version,
            node_id=record.node_id,
            ed25519_public=record.ed25519_public,
            x25519_public=record.x25519_public,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IdentityPublicRecord:
        """Load a public identity record from JSON-decoded data."""
        if "ed25519_private" in data or "x25519_private" in data:
            return cls.from_identity_record(IdentityRecord.from_dict(data))
        return cls(
            schema_version=int(data["schema_version"]),
            node_id=str(data["node_id"]),
            ed25519_public=str(data["ed25519_public"]),
            x25519_public=str(data["x25519_public"]),
        )

    def public_bytes(self) -> tuple[bytes, bytes, bytes]:
        """Return decoded node id, Ed25519 public key, and X25519 public key."""
        if self.schema_version != 1:
            raise ValueError(f"unsupported identity schema_version: {self.schema_version}")
        node_id = b64decode(self.node_id)
        ed25519_public = b64decode(self.ed25519_public)
        x25519_public = b64decode(self.x25519_public)
        if len(node_id) != 32:
            raise ValueError("identity public record node_id must decode to 32 bytes")
        if len(ed25519_public) != 32:
            raise ValueError("identity public record ed25519_public must decode to 32 bytes")
        if len(x25519_public) != 32:
            raise ValueError("identity public record x25519_public must decode to 32 bytes")
        return node_id, ed25519_public, x25519_public

    def export_dict(self) -> dict[str, int | str]:
        """Return a stable JSON-friendly public mapping."""
        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "ed25519_public": self.ed25519_public,
            "x25519_public": self.x25519_public,
        }

    def save(self, path: Path, *, force: bool = False) -> None:
        """Persist public identity material atomically."""
        if path.exists() and not force:
            raise FileExistsError(f"{path} already exists")
        atomic_write_json(path, self.export_dict(), mode=0o644)

    @classmethod
    def load(cls, path: Path) -> IdentityPublicRecord:
        """Load a public identity record from disk."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _b64(value: bytes) -> str:
    return b64encode(value).decode("ascii")
