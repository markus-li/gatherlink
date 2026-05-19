"""
Carrier/profile discovery and retry testing per physical link.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

# File-specific TODO:
# - Test raw UDP, stealth UDP, QUIC, WSS, and future carrier profiles per physical link.
# - Rank candidate logical paths and activate best N per interface.
# - Support manual force-retry, periodic retest, and retest-on-failure.
