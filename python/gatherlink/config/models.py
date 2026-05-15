"""User-facing Pydantic configuration models.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger


logger = get_logger(__name__)

# File-specific TODO:
# - Define minimal intent-based user config models.
# - Add advanced override models without making the simple config noisy.
# - Add schema_version and migration strategy.
