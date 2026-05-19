"""
helpers.dns.policies module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


DnsUpstreamKind = Literal["direct", "tunnel", "doh"]
DnssecMode = Literal["off", "allow_unsigned", "require_ad"]


@dataclass(frozen=True)
class DnsUpstream:
    """One DNS upstream candidate selected by Python policy."""

    name: str
    address: str
    port: int = 53
    kind: DnsUpstreamKind = "direct"
    timeout_seconds: float = 1.0

    def authority(self) -> str:
        """Return a compact diagnostics label for this upstream."""
        return f"{self.kind}:{self.name}@{self.address}:{self.port}"


@dataclass(frozen=True)
class DnsResolverPolicy:
    """
    DNS helper policy compiled for the local resolver endpoint.

    Direct and tunnel upstreams execute in v1. Tunnel means the DNS helper sends
    a normal DNS UDP query to a configured local Gatherlink service endpoint.
    DoH remains represented for policy compatibility and fails closed until it
    is explicitly promoted.
    """

    strategy: str = "race_first_valid"
    upstreams: list[DnsUpstream] = field(default_factory=lambda: [DnsUpstream(name="system", address="1.1.1.1")])
    dnssec_mode: DnssecMode = "allow_unsigned"
    serve_stale_seconds: int = 300

    def ordered_upstreams(self) -> list[DnsUpstream]:
        """Return upstreams in the order this local resolver should try them."""
        # TODO(dns-policy): Add actual racing and path-aware scoring here. The
        # current deterministic order keeps behavior easy to test while
        # preserving the policy surface for direct/tunnel/DoH choices.
        return list(self.upstreams)
