"""Scheduler policy definitions; Python decides policy, Rust executes compiled state.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger


logger = get_logger(__name__)

# File-specific TODO:
# - Implement fixed round-robin and weighted round-robin first.
# - Add adaptive policy only after receiver metrics are reliable.
# - Keep policy definitions separate from Rust hot-path execution.
