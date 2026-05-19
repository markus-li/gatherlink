"""
PAC file generation for temporary captive portal login sessions.

This module is part of the optional Gatherlink helper/control-plane layer.

Helper modules may provide policy, topology planning, user workflow support, connectivity workflow support, or configuration generation. They must not move packet hot-path behavior out of the Rust dataplane and must not turn Gatherlink into a firewall/router/proxy-zoo product.
"""

from __future__ import annotations

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


# File-specific TODO:
# - Implement python/gatherlink/helpers/captive_portal/pac.py.
# - Preserve explicit/generated configuration philosophy.
# - Keep helper failures isolated from the core transport.
# - Add unit tests and integration scenarios before marking stable.
