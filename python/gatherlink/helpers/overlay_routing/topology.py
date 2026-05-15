"""Future overlay topology graph for multihop and site/exit routing helpers.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger


logger = get_logger(__name__)

# File-specific TODO:
# - Represent future overlay graph with nodes, services, prefixes, exits, and relay capabilities.
# - Keep overlay topology separate from firewall/LAN routing policy.
# - Support per-service node roles: entry, relay, exit, site-gateway.
