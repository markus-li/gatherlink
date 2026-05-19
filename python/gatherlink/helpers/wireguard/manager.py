"""
Optional WireGuard orchestration helper.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import shutil

from gatherlink.config.runtime import RuntimeConfig
from gatherlink.helpers.wireguard.config import WireGuardTransportPlan
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


def wireguard_transport_plans(runtime_config: RuntimeConfig) -> list[WireGuardTransportPlan]:
    """Return WireGuard helper transport plans from runtime config."""
    return [
        WireGuardTransportPlan.from_runtime_helper(helper)
        for helper in runtime_config.helpers
        if helper.kind == "wireguard"
    ]


def wireguard_diagnostics(runtime_config: RuntimeConfig) -> list[dict[str, object]]:
    """Return operator-facing WireGuard helper diagnostics."""
    tools = wireguard_tool_status()
    diagnostics = []
    for plan in wireguard_transport_plans(runtime_config):
        item = plan.diagnostics()
        item["tools"] = tools
        diagnostics.append(item)
    return diagnostics


def wireguard_tool_status() -> dict[str, object]:
    """Return availability of WireGuard platform tooling without invoking privileged operations."""
    wg = shutil.which("wg")
    wg_quick = shutil.which("wg-quick")
    return {
        "wg": wg,
        "wg_quick": wg_quick,
        "ready_for_key_ops": wg is not None,
        "ready_for_wg_quick_guidance": wg_quick is not None,
    }
