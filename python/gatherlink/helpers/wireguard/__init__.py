"""WireGuard helper package."""

from gatherlink.helpers.wireguard.config import WireGuardTransportPlan, render_peer_endpoint_snippet
from gatherlink.helpers.wireguard.keys import derive_public_key, generate_private_key
from gatherlink.helpers.wireguard.manager import wireguard_diagnostics, wireguard_tool_status, wireguard_transport_plans
from gatherlink.helpers.wireguard.setup import (
    GeneratedWireGuardSetup,
    WireGuardSetupPath,
    WireGuardSetupRequest,
    default_local_paths,
    discover_network_interfaces,
    generate_wireguard_setup,
    parse_setup_path,
    render_setup_summary,
)

__all__ = [
    "GeneratedWireGuardSetup",
    "WireGuardSetupPath",
    "WireGuardSetupRequest",
    "WireGuardTransportPlan",
    "default_local_paths",
    "derive_public_key",
    "discover_network_interfaces",
    "generate_private_key",
    "generate_wireguard_setup",
    "parse_setup_path",
    "render_peer_endpoint_snippet",
    "render_setup_summary",
    "wireguard_diagnostics",
    "wireguard_tool_status",
    "wireguard_transport_plans",
]
