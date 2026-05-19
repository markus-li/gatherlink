"""Relay discovery and candidate relay selection."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from gatherlink.helpers.relay_fabric.health import EndpointProbe, evaluate_relay_health
from gatherlink.helpers.relay_fabric.models import RelayCandidate, RelayDiscoveryReport


def load_relay_candidates(path: Path) -> list[RelayCandidate]:
    """Load relay candidates from a local JSON metadata file."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload if isinstance(payload, list) else payload.get("relays", [])
    return [RelayCandidate.model_validate(item) for item in records]


def discover_relays(
    configured: Iterable[RelayCandidate],
    *,
    required_protocol_version: str | None = None,
    endpoint_probe: EndpointProbe | None = None,
) -> RelayDiscoveryReport:
    """Build a relay discovery report from configured/signed candidates."""
    candidates = list(configured)
    health = [
        evaluate_relay_health(
            candidate,
            required_protocol_version=required_protocol_version,
            endpoint_probe=endpoint_probe,
        )
        for candidate in candidates
    ]
    return RelayDiscoveryReport(candidates=candidates, health=health)


def discover_relays_from_file(
    path: Path,
    *,
    required_protocol_version: str | None = None,
    endpoint_probe: EndpointProbe | None = None,
) -> RelayDiscoveryReport:
    """Load and evaluate relays from one local JSON metadata file."""
    return discover_relays(
        load_relay_candidates(path),
        required_protocol_version=required_protocol_version,
        endpoint_probe=endpoint_probe,
    )
