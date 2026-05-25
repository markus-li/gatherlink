"""
helpers.wireguard.config module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
    profile: Literal["single", "dual_profile"] = "single"
    traffic_class: Literal["single", "stable", "fast"] = "single"

    @classmethod
    def from_runtime_helper(cls, helper: RuntimeWireGuardHelperConfig) -> WireGuardTransportPlan:
        """Build a transport plan from expanded runtime helper config."""
        return cls(
            service=helper.service,
            wireguard_target=helper.service_target,
            gatherlink_listen=helper.service_listen,
            helper_enabled=helper.enabled,
            profile=helper.profile,
            traffic_class=helper.traffic_class,
        )

    @property
    def scheduler_guidance(self) -> str:
        """Return the scheduler posture this WireGuard service should normally use."""
        if self.traffic_class == "stable":
            return "flowlet_adaptive or coordinated_adaptive with TCP-stability bias"
        if self.traffic_class == "fast":
            return "capacity_aware or coordinated_adaptive with UDP-throughput bias"
        return "coordinated_adaptive"

    def diagnostics(self) -> dict[str, object]:
        """Return operator-facing mapping diagnostics."""
        return {
            "service": self.service,
            "enabled": self.helper_enabled,
            "profile": self.profile,
            "traffic_class": self.traffic_class,
            "wireguard_local_listen": self.wireguard_target,
            "wireguard_peer_endpoint": self.gatherlink_listen,
            "scheduler_guidance": self.scheduler_guidance,
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
    suffix = "" if plan.traffic_class == "single" else f" # {plan.traffic_class} profile"
    return "\n".join(
        [
            "[Peer]",
            f"PublicKey = {public_key}",
            f"Endpoint = {plan.gatherlink_listen}{suffix}",
            "AllowedIPs = <wireguard-owned-routes>",
            "",
        ]
    )
