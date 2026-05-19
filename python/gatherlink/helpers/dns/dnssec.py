"""
helpers.dns.dnssec module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import dns.flags
import dns.message

from gatherlink.helpers.dns.policies import DnssecMode
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


DnssecStatus = Literal["disabled", "validated_by_upstream", "unsigned_or_unvalidated", "failed"]


@dataclass(frozen=True)
class DnssecDiagnostic:
    """DNSSEC validation state visible to diagnostics and cache records."""

    status: DnssecStatus
    message: str

    @property
    def accepted(self) -> bool:
        """Return whether policy allows this response to be used."""
        return self.status != "failed"


def evaluate_dnssec(response: dns.message.Message, mode: DnssecMode) -> DnssecDiagnostic:
    """
    Evaluate DNSSEC status for a response.

    Full chain validation can be added here without touching the resolver
    listener. The first production-safe behavior is explicit: disabled mode does
    not claim validation, allow-unsigned reports unvalidated answers, and
    require-ad accepts only responses marked authenticated by an upstream
    validating resolver.
    """
    if mode == "off":
        return DnssecDiagnostic(status="disabled", message="DNSSEC validation disabled by helper policy")

    authenticated = bool(response.flags & dns.flags.AD)
    if authenticated:
        return DnssecDiagnostic(status="validated_by_upstream", message="upstream resolver set AD")

    if mode == "require_ad":
        return DnssecDiagnostic(status="failed", message="DNSSEC AD bit required but not present")

    return DnssecDiagnostic(status="unsigned_or_unvalidated", message="response accepted without AD bit")
