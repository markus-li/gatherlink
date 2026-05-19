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
    assert runtime.security.mode == "none"
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
    assert helper.upstreams[0].name == "cloudflare"
    assert helper.upstreams[0].kind == "direct"
    assert helper.upstreams[0].address == "1.1.1.1"


def test_socks5_helper_expands_service_transport_and_policy() -> None:
    config = validate_config_file(EXAMPLES / "socks5-helper.json")
    runtime = expand_config(config)

    helper = runtime.helpers[0]
    assert helper.kind == "socks5"
    assert helper.service == "socks5-stream"
    assert helper.service_listen == "127.0.0.1:55190"
    assert helper.service_target == "127.0.0.1:56190"
    assert helper.listen == "127.0.0.1:1080"
    assert helper.allow_hosts == ["example.com"]
    assert helper.allow_ports == [443]


def test_tcp_forward_helper_expands_service_transport_and_rule() -> None:
    config = validate_config_file(EXAMPLES / "tcp-forward-helper.json")
    runtime = expand_config(config)

    helper = runtime.helpers[0]
    assert helper.kind == "tcp_forward"
    assert helper.service == "tcp-forward-stream"
    assert helper.service_listen == "127.0.0.1:55191"
    assert helper.service_target == "127.0.0.1:56191"
    assert helper.listen == "127.0.0.1:18080"
    assert helper.target == "10.0.0.10:80"


def test_ipv6_service_expands_without_ipv4_assumptions() -> None:
    config = validate_config_file(EXAMPLES / "minimal-ipv6-client.json")
    runtime = expand_config(config)

    assert runtime.paths[0].source_ip == "2001:db8::10"
    assert runtime.paths[0].gateway == "2001:db8::1"
    assert runtime.services[0].listen == "[::1]:55180"
    assert runtime.services[0].target == "[::1]:51820"


def test_static_runtime_export_redacts_key_material() -> None:
    config = validate_config_file(EXAMPLES / "windows-two-node-a.json")
    runtime = expand_config(config)

    exported = runtime.export_dict()

    assert exported["security"]["mode"] == "static"
    assert exported["security"]["send_key"] == "[redacted:32 bytes]"
    assert exported["security"]["receive_key"] == "[redacted:32 bytes]"
    assert runtime.security.send_key is not None
    assert len(runtime.security.send_key) == 32


def test_path_relay_runtime_export_redacts_hop_key() -> None:
    from base64 import b64encode

    from gatherlink.config.models import PathRelayHopConfig

    config = validate_config_file(EXAMPLES / "minimal-client.json")
    relay_config = config.model_copy(
        update={
            "paths": [
                config.paths[0].model_copy(
                    update={
                        "relay": PathRelayHopConfig(
                            relay_receiver_index=901,
                            send_key=b64encode(bytes([0x66]) * 32).decode("ascii"),
                        )
                    }
                )
            ]
        }
    )

    runtime = expand_config(relay_config)
    exported = runtime.export_dict()

    assert runtime.paths[0].relay is not None
    assert runtime.paths[0].relay.relay_receiver_index == 901
    assert runtime.paths[0].relay.send_key == bytes([0x66]) * 32
    assert exported["paths"][0]["relay"]["send_key"] == "[redacted:32 bytes]"


def test_authenticated_security_compiles_to_rust_static_executor_and_redacts() -> None:
    config = validate_config_file(EXAMPLES / "windows-two-node-a.json")
    authenticated_config = config.model_copy(
        update={"security": config.security.model_copy(update={"mode": "authenticated"})}
    )

    runtime = expand_config(authenticated_config)
    exported = runtime.export_dict()

    assert runtime.security.mode == "static"
    assert runtime.security.source_mode == "authenticated"
    assert exported["security"]["source_mode"] == "authenticated"
    assert exported["security"]["send_key"] == "[redacted:32 bytes]"
    assert exported["security"]["receive_key"] == "[redacted:32 bytes]"
