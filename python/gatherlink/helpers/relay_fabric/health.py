"""Relay health tracking and capability state."""

from __future__ import annotations

from collections.abc import Callable

from gatherlink.helpers.relay_fabric.models import RelayCandidate, RelayEndpoint, RelayHealth

EndpointProbe = Callable[[RelayEndpoint], bool]


def evaluate_relay_health(
    candidate: RelayCandidate,
    *,
    required_protocol_version: str | None = None,
    endpoint_probe: EndpointProbe | None = None,
) -> RelayHealth:
    """Evaluate one relay candidate without creating routing/session policy."""
    if candidate.disabled:
        return RelayHealth(node_id=candidate.node_id, state="disabled", reason="relay is disabled")
    if required_protocol_version and required_protocol_version not in candidate.capabilities:
        return RelayHealth(
            node_id=candidate.node_id,
            state="incompatible",
            reason=f"missing required capability: {required_protocol_version}",
        )
    if not candidate.endpoints:
        return RelayHealth(node_id=candidate.node_id, state="stale_topology", reason="relay has no endpoints")

    reachable = [
        endpoint.authority()
        for endpoint in candidate.endpoints
        if endpoint_probe is None or endpoint_probe(endpoint)
    ]
    if not reachable:
        return RelayHealth(
            node_id=candidate.node_id,
            state="carrier_failure",
            reason="no relay endpoints were reachable",
        )
    if "overloaded" in candidate.capabilities:
        return RelayHealth(
            node_id=candidate.node_id,
            state="overloaded",
            reason="relay reports overloaded capability state",
            reachable_endpoints=reachable,
            overloaded=True,
        )
    if "authenticated" in candidate.capabilities:
        return RelayHealth(
            node_id=candidate.node_id,
            state="authenticated",
            reason="relay metadata is authenticated",
            reachable_endpoints=reachable,
        )
    return RelayHealth(
        node_id=candidate.node_id,
        state="reachable",
        reason="relay has at least one reachable endpoint",
        reachable_endpoints=reachable,
    )
