"""
External domain-set loading and matching.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import dns.name

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


def normalize_qname(value: dns.name.Name | str) -> str:
    """
    Normalize a DNS name for cache keys, policy matching, and diagnostics.

    dnspython performs IDNA handling for text input. The returned key is
    absolute, lower-case DNS wire text so equivalent Unicode and punycode forms
    do not fork the cache.
    """
    name = value if isinstance(value, dns.name.Name) else dns.name.from_text(value)
    return name.canonicalize().to_text()
