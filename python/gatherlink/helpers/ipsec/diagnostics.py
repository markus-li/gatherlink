"""Diagnostics for IPsec NAT-T service templates and unsupported raw ESP/AH expectations.

This module is an optional Gatherlink helper or helper-support module.

Helpers may improve usability, expose intent/metadata, or solve ugly connectivity
edge cases. They must not define or contaminate the core transport architecture.

Logging:
    Use the shared Gatherlink logger so helper logs can be redirected, formatted,
    filtered, or exported consistently.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger


logger = get_logger(__name__)


# File-specific TODO:
# - Implement helpers.ipsec.diagnostics without introducing dataplane hot-path dependencies.
# - Keep helper failures isolated from core Gatherlink transport.
# - Add focused unit tests and at least one integration scenario if applicable.
