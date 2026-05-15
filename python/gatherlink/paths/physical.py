"""Physical interface/source/gateway state and validation.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger


logger = get_logger(__name__)

# File-specific TODO:
# - Validate interface existence, link state, source IP, subnet, gateway, and route sanity.
# - Reject ambiguous same-subnet layouts unless source IP and gateway behavior are deterministic.
# - Expose clear invalid reasons for diagnostics.
