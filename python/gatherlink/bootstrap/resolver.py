"""
Bootstrap lookup using cache, static IPs, direct DNS, DoH, and future metadata.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from gatherlink.bootstrap.cache import BootstrapCache, BootstrapEndpoint
from gatherlink.shared.logging import get_logger
from gatherlink.shared.models import GatherlinkBaseModel

logger = get_logger(__name__)


class BootstrapResolution(GatherlinkBaseModel):
    """Ordered bootstrap candidates for one peer."""

    peer: str
    endpoints: list[BootstrapEndpoint] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


def resolve_bootstrap(
    peer: str,
    *,
    static_endpoints: list[str] | None = None,
    cache_path: Path | None = None,
) -> BootstrapResolution:
    """
    Resolve bootstrap candidates without making trust decisions.

    Static config wins over cache because endpoint policy is explicit
    configuration. DNS, DoH, and relay metadata can plug in here later, but they
    should still return candidate facts only; authentication belongs in the
    connector before a candidate becomes trusted.
    """
    endpoints: list[BootstrapEndpoint] = []
    sources: list[str] = []
    seen: set[str] = set()

    for value in static_endpoints or []:
        _append_endpoint(endpoints, seen, BootstrapEndpoint.parse(value, source="static"))
    if endpoints:
        sources.append("static")

    if cache_path is not None:
        cache = BootstrapCache.load(cache_path)
        cached = cache.get(peer)
        for endpoint in cached:
            endpoint.source = endpoint.source or "cache"
            _append_endpoint(endpoints, seen, endpoint)
        if cached:
            sources.append("cache")

    return BootstrapResolution(peer=peer, endpoints=endpoints, sources=sources)


def _append_endpoint(endpoints: list[BootstrapEndpoint], seen: set[str], endpoint: BootstrapEndpoint) -> None:
    """Append a candidate once while preserving resolver priority order."""
    key = endpoint.authority()
    if key in seen:
        return
    seen.add(key)
    endpoints.append(endpoint)
