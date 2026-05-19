"""
helpers.wireguard.config module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from gatherlink.config.runtime import RuntimeWireGuardHelperConfig
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class WireGuardTransportPlan:
    """
    WireGuard-over-Gatherlink endpoint mapping.

    WireGuard owns its interface, keys, peers, routes, and firewall behavior.
    Gatherlink only provides the local UDP transport socket that WireGuard sends
    to and the remote UDP target where the peer's WireGuard listener lives.
    """

    service: str
    wireguard_target: str
    gatherlink_listen: str | None
    helper_enabled: bool = True

    @classmethod
    def from_runtime_helper(cls, helper: RuntimeWireGuardHelperConfig) -> WireGuardTransportPlan:
        """Build a transport plan from expanded runtime helper config."""
        return cls(
            service=helper.service,
            wireguard_target=helper.service_target,
            gatherlink_listen=helper.service_listen,
            helper_enabled=helper.enabled,
        )

    def diagnostics(self) -> dict[str, object]:
        """Return operator-facing mapping diagnostics."""
        return {
            "service": self.service,
            "enabled": self.helper_enabled,
            "wireguard_local_listen": self.wireguard_target,
            "wireguard_peer_endpoint": self.gatherlink_listen,
            "note": (
                "Set the WireGuard peer Endpoint to wireguard_peer_endpoint. "
                "Gatherlink forwards that UDP service to wireguard_local_listen on the far side."
            ),
        }


def render_peer_endpoint_snippet(plan: WireGuardTransportPlan, *, peer_public_key: str | None = None) -> str:
    """
    Render a small WireGuard peer snippet for the Gatherlink service endpoint.

    The snippet is intentionally incomplete: interface addresses, AllowedIPs,
    routes, firewall policy, and persistent keepalive remain WireGuard/operator
    decisions.
    """
    if not plan.gatherlink_listen:
        raise ValueError("wireguard helper needs a service listen endpoint to render a peer Endpoint")
    public_key = peer_public_key or "<peer-public-key>"
    return "\n".join(
        [
            "[Peer]",
            f"PublicKey = {public_key}",
            f"Endpoint = {plan.gatherlink_listen}",
            "AllowedIPs = <wireguard-owned-routes>",
            "",
        ]
    )
