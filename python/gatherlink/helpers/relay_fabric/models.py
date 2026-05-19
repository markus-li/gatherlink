"""Relay fabric DTOs and capability declarations."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from gatherlink.shared.models import GatherlinkBaseModel

RelayHealthState = Literal[
    "reachable",
    "authenticated",
    "degraded",
    "carrier_failure",
    "overloaded",
    "disabled",
    "incompatible",
    "stale_topology",
]


class RelayEndpoint(GatherlinkBaseModel):
    """One public relay endpoint over a carrier/protocol."""

    protocol: Literal["udp", "quic", "wss"]
    address: str
    port: int
    carrier: str | None = None

    def authority(self) -> str:
        """Return a compact endpoint diagnostics label."""
        return f"{self.protocol}:{self.address}:{self.port}"


class RelayCandidate(GatherlinkBaseModel):
    """Configured or discovered relay candidate metadata."""

    node_id: str
    region: str | None = None
    endpoints: list[RelayEndpoint] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    trust_domain: str | None = None
    allowed_transit: bool = False
    allowed_exit: bool = False
    disabled: bool = False
    operator_notes: str | None = None


class RelayHealth(GatherlinkBaseModel):
    """Health state for one relay candidate."""

    node_id: str
    state: RelayHealthState
    reason: str
    reachable_endpoints: list[str] = Field(default_factory=list)
    stale: bool = False
    overloaded: bool = False


class RelayDiscoveryReport(GatherlinkBaseModel):
    """Relay candidates plus health diagnostics exported to control/topology logic."""

    candidates: list[RelayCandidate]
    health: list[RelayHealth]

    def usable_candidates(self) -> list[RelayCandidate]:
        """Return candidates with a usable health state."""
        usable_ids = {
            item.node_id
            for item in self.health
            if item.state in {"reachable", "authenticated", "degraded"} and not item.stale
        }
        return [candidate for candidate in self.candidates if candidate.node_id in usable_ids]
