"""
bootstrap.cache module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from gatherlink.persistence.store import atomic_write_json, load_json_or_default
from gatherlink.shared.logging import get_logger
from gatherlink.shared.models import GatherlinkBaseModel

logger = get_logger(__name__)


class BootstrapEndpoint(GatherlinkBaseModel):
    """A candidate UDP endpoint for reaching one Gatherlink peer."""

    host: str
    port: int = Field(ge=1, le=65535)
    source: str = "static"
    last_verified_at: datetime | None = None

    @classmethod
    def parse(cls, value: str, *, source: str = "static") -> BootstrapEndpoint:
        """Parse ``host:port`` or ``[ipv6]:port`` into a typed endpoint."""
        text = value.strip()
        if not text:
            raise ValueError("bootstrap endpoint cannot be empty")
        if text.startswith("["):
            host, separator, port_text = text[1:].partition("]:")
            if separator != "]:":
                raise ValueError(f"invalid bracketed IPv6 endpoint: {value}")
        else:
            host, separator, port_text = text.rpartition(":")
            if separator != ":":
                raise ValueError(f"endpoint must include a UDP port: {value}")
        if not host:
            raise ValueError(f"endpoint host cannot be empty: {value}")
        return cls(host=host, port=int(port_text), source=source)

    def authority(self) -> str:
        """Return a stable host:port string safe for IPv4, DNS names, and IPv6."""
        if ":" in self.host and not self.host.startswith("["):
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


class BootstrapPeerCache(GatherlinkBaseModel):
    """Persisted endpoint facts for one peer name."""

    peer: str
    endpoints: list[BootstrapEndpoint] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BootstrapCache(GatherlinkBaseModel):
    """
    Small JSON cache for last-known bootstrap endpoints.

    Production callers should cache endpoints only after authenticated bootstrap
    proof verification. Plaintext lab entries remain useful for local testing,
    but they must stay visibly marked as unauthenticated test data.
    """

    peers: dict[str, BootstrapPeerCache] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> BootstrapCache:
        """Load a bootstrap cache file, returning an empty cache when absent or corrupt."""
        return cls.model_validate(load_json_or_default(path, {}))

    def save(self, path: Path) -> None:
        """Write the cache atomically for local CLI and service usage."""
        atomic_write_json(path, self.export_dict(), mode=0o600)

    def get(self, peer: str) -> list[BootstrapEndpoint]:
        """Return cached endpoints for a peer."""
        entry = self.peers.get(peer)
        return list(entry.endpoints) if entry else []

    def put(self, peer: str, endpoints: list[BootstrapEndpoint]) -> None:
        """Replace cached endpoints for a peer."""
        self.peers[peer] = BootstrapPeerCache(
            peer=peer,
            endpoints=endpoints,
            updated_at=datetime.now(UTC),
        )
