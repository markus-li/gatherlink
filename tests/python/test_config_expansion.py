from __future__ import annotations

from pathlib import Path

from gatherlink.config import expand_config, validate_config_file

EXAMPLES = Path("configs/examples")


def test_minimal_client_expands_to_runtime_config() -> None:
    config = validate_config_file(EXAMPLES / "minimal-client.json")
    runtime = expand_config(config)

    assert runtime.metadata["runtime_model"] == "RuntimeConfig"
    assert runtime.role == "client"
    assert runtime.peer == "relay"
    assert runtime.paths[0].name == "wan1"
    assert runtime.services[0].protocol == "udp"
    assert runtime.helpers == []


def test_wireguard_helper_expands_service_reference() -> None:
    config = validate_config_file(EXAMPLES / "wireguard-client.json")
    runtime = expand_config(config)

    helper = runtime.helpers[0]
    assert helper.kind == "wireguard"
    assert helper.service == "wireguard-main"
    assert helper.service_target == "127.0.0.1:51820"
    assert helper.service_listen == "127.0.0.1:55180"


def test_dns_helper_expands_to_ordered_runtime_helper() -> None:
    config = validate_config_file(EXAMPLES / "dns-helper.json")
    runtime = expand_config(config)

    helper = runtime.helpers[0]
    assert helper.kind == "dns"
    assert helper.listen == "127.0.0.1:5353"
    assert helper.strategy == "race_first_valid"


def test_ipv6_service_expands_without_ipv4_assumptions() -> None:
    config = validate_config_file(EXAMPLES / "minimal-ipv6-client.json")
    runtime = expand_config(config)

    assert runtime.paths[0].source_ip == "2001:db8::10"
    assert runtime.paths[0].gateway == "2001:db8::1"
    assert runtime.services[0].listen == "[::1]:55180"
    assert runtime.services[0].target == "[::1]:51820"
