from __future__ import annotations

import json

from gatherlink.bootstrap.cache import BootstrapCache, BootstrapEndpoint
from gatherlink.bootstrap.connector import probe_candidate
from gatherlink.bootstrap.resolver import resolve_bootstrap
from gatherlink.cli.main import app
from typer.testing import CliRunner


def test_bootstrap_endpoint_parses_ipv4_dns_and_ipv6() -> None:
    assert BootstrapEndpoint.parse("127.0.0.1:51820").authority() == "127.0.0.1:51820"
    assert BootstrapEndpoint.parse("example.test:51820").authority() == "example.test:51820"
    assert BootstrapEndpoint.parse("[2001:db8::1]:51820").authority() == "[2001:db8::1]:51820"


def test_bootstrap_cache_round_trips_endpoints(tmp_path) -> None:
    cache_path = tmp_path / "bootstrap-cache.json"
    cache = BootstrapCache()
    cache.put("peer-a", [BootstrapEndpoint.parse("127.0.0.1:51820", source="cache")])
    cache.save(cache_path)

    loaded = BootstrapCache.load(cache_path)

    assert loaded.get("peer-a")[0].authority() == "127.0.0.1:51820"
    assert loaded.get("peer-a")[0].source == "cache"


def test_resolver_prefers_static_and_deduplicates_cache(tmp_path) -> None:
    cache_path = tmp_path / "bootstrap-cache.json"
    cache = BootstrapCache()
    cache.put(
        "peer-a",
        [
            BootstrapEndpoint.parse("127.0.0.1:51820", source="cache"),
            BootstrapEndpoint.parse("127.0.0.1:51821", source="cache"),
        ],
    )
    cache.save(cache_path)

    resolution = resolve_bootstrap("peer-a", static_endpoints=["127.0.0.1:51820"], cache_path=cache_path)

    assert [endpoint.authority() for endpoint in resolution.endpoints] == [
        "127.0.0.1:51820",
        "127.0.0.1:51821",
    ]
    assert resolution.sources == ["static", "cache"]


def test_probe_refuses_authenticated_mode_until_crypto_exists() -> None:
    result = probe_candidate(BootstrapEndpoint.parse("127.0.0.1:51820"))

    assert result.reachable is False
    assert result.authenticated is False
    assert "authenticated bootstrap probes" in result.warning


def test_bootstrap_cli_resolves_static_endpoint() -> None:
    result = CliRunner().invoke(app, ["bootstrap", "resolve", "peer-a", "--static", "127.0.0.1:51820"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["peer"] == "peer-a"
    assert payload["endpoints"][0]["host"] == "127.0.0.1"


def test_bootstrap_cli_probe_requires_insecure_flag() -> None:
    result = CliRunner().invoke(app, ["bootstrap", "probe", "127.0.0.1:51820"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["reachable"] is False
