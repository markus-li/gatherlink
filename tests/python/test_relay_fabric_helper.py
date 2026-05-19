from __future__ import annotations

import json

from gatherlink.cli.main import app
from gatherlink.helpers.relay_fabric import RelayCandidate, RelayEndpoint, discover_relays, discover_relays_from_file
from typer.testing import CliRunner


def test_relay_discovery_marks_authenticated_reachable_candidate() -> None:
    relay = RelayCandidate(
        node_id="relay-a",
        region="eu",
        endpoints=[RelayEndpoint(protocol="udp", address="203.0.113.10", port=51820)],
        capabilities=["v1", "authenticated"],
        allowed_transit=True,
    )

    report = discover_relays([relay], required_protocol_version="v1", endpoint_probe=lambda _endpoint: True)

    assert report.health[0].state == "authenticated"
    assert report.usable_candidates()[0].node_id == "relay-a"


def test_relay_discovery_reports_disabled_and_incompatible_candidates() -> None:
    disabled = RelayCandidate(node_id="relay-disabled", disabled=True)
    incompatible = RelayCandidate(
        node_id="relay-old",
        endpoints=[RelayEndpoint(protocol="wss", address="relay.example", port=443)],
        capabilities=["legacy"],
    )

    report = discover_relays([disabled, incompatible], required_protocol_version="v1")

    assert [item.state for item in report.health] == ["disabled", "incompatible"]
    assert report.usable_candidates() == []


def test_relay_discovery_file_round_trip(tmp_path) -> None:
    metadata = tmp_path / "relays.json"
    metadata.write_text(
        json.dumps(
            {
                "relays": [
                    {
                        "node_id": "relay-a",
                        "region": "apac",
                        "endpoints": [{"protocol": "quic", "address": "relay.example", "port": 443}],
                        "capabilities": ["v1"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = discover_relays_from_file(metadata)

    assert report.candidates[0].node_id == "relay-a"
    assert report.health[0].state == "reachable"


def test_relay_discover_cli_prints_health_report(tmp_path) -> None:
    metadata = tmp_path / "relays.json"
    metadata.write_text(
        json.dumps(
            [
                {
                    "node_id": "relay-a",
                    "endpoints": [{"protocol": "udp", "address": "203.0.113.10", "port": 51820}],
                    "capabilities": ["v1"],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["helpers", "relay-discover", str(metadata), "--required-capability", "v1"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["health"][0]["state"] == "reachable"
