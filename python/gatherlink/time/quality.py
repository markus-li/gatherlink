"""
Time-source quality aggregation and confidence scoring.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

# File-specific TODO:
# - Combine system NTP, direct NTP, tunnel NTP, peer exchange, and GPS quality signals.
# - Maintain internal time confidence and offset estimate.
# - Do not make dataplane correctness depend on wall-clock sync.
