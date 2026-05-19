"""
Time-source quality aggregation and confidence scoring.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

# TODO(time-quality):
# - Combine system NTP, direct NTP, tunnel NTP, peer exchange, and GPS quality signals.
# - Maintain internal time confidence and peer-relative offset estimates from Gatherlink control metadata.
# - Keep sink-authoritative internal sync separate from privileged system clock corrections.
# - Do not make dataplane correctness depend on wall-clock sync.
