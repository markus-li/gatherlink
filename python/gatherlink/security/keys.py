"""Node identity and signing helpers owned by the Python control plane."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

NODE_ID_DOMAIN = b"gatherlink node v1"


@dataclass(frozen=True)
class NodeIdentity:
    """A Gatherlink node identity with signing and transport static keys."""

    ed25519_private: bytes
    ed25519_public: bytes
    x25519_private: bytes
    x25519_public: bytes
    node_id: bytes

    @classmethod
    def generate(cls) -> NodeIdentity:
        """Generate a new Ed25519 identity and X25519 transport static key."""
        ed_private = Ed25519PrivateKey.generate()
        x_private = X25519PrivateKey.generate()
        ed_public = ed_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        x_public = x_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return cls(
            ed25519_private=ed_private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
            ed25519_public=ed_public,
            x25519_private=x_private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
            x25519_public=x_public,
            node_id=node_id_from_ed25519_public(ed_public),
        )

    @classmethod
    def from_private_keys(cls, ed25519_private: bytes, x25519_private: bytes) -> NodeIdentity:
        """Load an identity from raw private-key bytes."""
        ed_private = Ed25519PrivateKey.from_private_bytes(ed25519_private)
        x_private = X25519PrivateKey.from_private_bytes(x25519_private)
        ed_public = ed_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        x_public = x_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return cls(
            ed25519_private=ed25519_private,
            ed25519_public=ed_public,
            x25519_private=x25519_private,
            x25519_public=x_public,
            node_id=node_id_from_ed25519_public(ed_public),
        )

    def sign(self, domain: bytes, canonical_body: bytes) -> bytes:
        """Sign a domain-separated canonical document body."""
        return sign_document(self.ed25519_private, domain, canonical_body)


def node_id_from_ed25519_public(public_key: bytes) -> bytes:
    """Return the stable Gatherlink node id for an Ed25519 public key."""
    if len(public_key) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    return sha256(NODE_ID_DOMAIN + public_key).digest()


def sign_document(private_key: bytes, domain: bytes, canonical_body: bytes) -> bytes:
    """Sign domain-separated canonical document bytes with Ed25519."""
    return Ed25519PrivateKey.from_private_bytes(private_key).sign(domain + canonical_body)


def verify_document(public_key: bytes, domain: bytes, canonical_body: bytes, signature: bytes) -> None:
    """Verify a domain-separated Ed25519 document signature."""
    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, domain + canonical_body)


def x25519_shared_secret(private_key: bytes, peer_public_key: bytes) -> bytes:
    """Derive a raw X25519 shared secret."""
    private = X25519PrivateKey.from_private_bytes(private_key)
    public = X25519PublicKey.from_public_bytes(peer_public_key)
    return private.exchange(public)

