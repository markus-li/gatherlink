"""Expand minimal user config into explicit runtime config.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger


logger = get_logger(__name__)

# File-specific TODO:
# - Expand minimal config into explicit runtime config.
# - Apply default carrier profiles, MTU policy, scheduler policy, and diagnostics settings.
# - Ensure helpers remain optional and never required by core transport.
