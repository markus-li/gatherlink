"""
Peer failover, failback, standby probing, and peer-health logic.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

# File-specific TODO:
# - Implement peer priority, automatic failover, conservative failback, and session-aware migration.
# - Track peer health separately from individual carrier/path health.
# - Add minimum dwell-time windows to prevent oscillation.
