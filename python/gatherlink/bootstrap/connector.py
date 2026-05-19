"""
Resolve plus authenticated connect validation for candidate peer endpoints.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from gatherlink.bootstrap.cache import BootstrapEndpoint
from gatherlink.shared.logging import get_logger
from gatherlink.shared.models import GatherlinkBaseModel

logger = get_logger(__name__)


class BootstrapProbeResult(GatherlinkBaseModel):
    """Result of validating one bootstrap candidate."""

    endpoint: BootstrapEndpoint
    reachable: bool
    authenticated: bool
    checked_at: datetime
    warning: str | None = None


def probe_candidate(endpoint: BootstrapEndpoint, *, allow_insecure: bool = False) -> BootstrapProbeResult:
    """
    Validate whether a candidate can be used for bootstrap.

    TODO(bootstrap-auth): Replace this plaintext lab probe with an authenticated
    challenge once identity, signing, and crypto are in place. Until then this
    function intentionally refuses production-style validation unless the caller
    opts into insecure local bootstrap behavior.
    """
    if not allow_insecure:
        return BootstrapProbeResult(
            endpoint=endpoint,
            reachable=False,
            authenticated=False,
            checked_at=datetime.now(UTC),
            warning="authenticated bootstrap probes are not implemented yet",
        )

    logger.warning("using insecure bootstrap candidate %s; this is only acceptable for local labs", endpoint.authority())
    return BootstrapProbeResult(
        endpoint=endpoint,
        reachable=True,
        authenticated=False,
        checked_at=datetime.now(UTC),
        warning="insecure plaintext bootstrap accepted for local lab use",
    )
