"""
Top-level runtime coordinator for starting, stopping, and wiring components.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.config.runtime import RuntimeConfig
from gatherlink.runtime.plan import RuntimePlan, build_runtime_plan
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


def plan_runtime_start(config: RuntimeConfig) -> RuntimePlan:
    """Return the non-privileged startup plan for a runtime config."""
    return build_runtime_plan(config)
