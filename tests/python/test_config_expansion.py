from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from gatherlink.config import expand_config, validate_config_dict, validate_config_file

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
    assert runtime.services[0].traffic_class == "tcp_ordered"


def test_wireguard_dual_profile_expands_two_runtime_helpers() -> None:
    config = validate_config_file(EXAMPLES / "wireguard-dual-profile-client.json")
    runtime = expand_config(config)

    assert [helper.kind for helper in runtime.helpers] == ["wireguard", "wireguard"]
    assert [helper.profile for helper in runtime.helpers] == ["dual_profile", "dual_profile"]
    assert [helper.traffic_class for helper in runtime.helpers] == ["stable", "fast"]
    assert [helper.service for helper in runtime.helpers] == ["wireguard-stable", "wireguard-fast"]
    assert runtime.services[0].priority == "high"
    assert runtime.services[0].traffic_class == "tcp_ordered"
    assert runtime.services[0].scheduler_poll_batch_packets == 128
    assert runtime.services[1].priority == "bulk"
    assert runtime.services[1].traffic_class == "udp_bulk"


def test_explicit_service_traffic_class_wins_over_helper_default() -> None:
    data = validate_config_file(EXAMPLES / "wireguard-client.json").model_dump(mode="json")
    data["services"][0]["traffic_class"] = "latency_sensitive"

    runtime = expand_config(validate_config_dict(data))

    assert runtime.services[0].traffic_class == "latency_sensitive"


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
        update={
            "security": config.security.model_copy(
                update={
                    "mode": "authenticated",
                    "local_node_id": "node-a-id",
                    "peer_node_id": "node-b-id",
                    "topology_generation": 9,
                    "session_role": "initiator",
                    "session_created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "session_expires_at": datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
                    "rekey_after_packets": 123,
                    "rekey_after_bytes": 456,
                }
            )
        }
    )

    runtime = expand_config(authenticated_config)
    exported = runtime.export_dict()

    assert runtime.security.mode == "static"
    assert runtime.security.source_mode == "authenticated"
    assert runtime.security.local_node_id == "node-a-id"
    assert runtime.security.peer_node_id == "node-b-id"
    assert runtime.security.topology_generation == 9
    assert runtime.security.session_role == "initiator"
    assert runtime.security.rekey_after_packets == 123
    assert runtime.security.rekey_after_bytes == 456
    assert exported["security"]["source_mode"] == "authenticated"
    assert exported["security"]["session_role"] == "initiator"
    assert exported["security"]["session_created_at"] == "2026-01-01T00:00:00Z"
    assert exported["security"]["send_key"] == "[redacted:32 bytes]"
    assert exported["security"]["receive_key"] == "[redacted:32 bytes]"


def test_flowlet_adaptive_expands_service_flowlet_defaults() -> None:
    from gatherlink.config.models import GatherlinkConfig, PathConfig, SchedulerConfig, ServiceConfig
    from gatherlink.scheduling.policies import (
        FLOWLET_ADAPTIVE_DEFAULT_IDLE_US,
        FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US,
        FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS,
    )

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="flowlet_adaptive"),
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    runtime = expand_config(config)

    assert runtime.scheduler.mode == "adaptive"
    assert runtime.services[0].scheduler_flowlet_idle_us == FLOWLET_ADAPTIVE_DEFAULT_IDLE_US
    assert runtime.services[0].scheduler_flowlet_max_hold_us == FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US
    assert runtime.services[0].scheduler_path_run_datagrams == FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS


def test_validated_config_preserves_top_level_scheduler_policy() -> None:
    config = validate_config_dict(
        {
            "schema_version": 1,
            "node": "local",
            "role": "client",
            "peer": "remote",
            "scheduler": {"mode": "ordered_multipath"},
            "paths": [
                {
                    "name": "path-a",
                    "interface": "gl-a",
                    "scheduler": {"tx_capacity_bps": 300_000_000, "reorder_hold_us": 50_000},
                }
            ],
            "services": [{"name": "udp-main", "target": "127.0.0.1:51820"}],
        }
    )

    runtime = expand_config(config)

    assert runtime.scheduler.mode == "ordered_multipath"
    assert runtime.paths[0].scheduler.max_in_flight_bytes == 3_750_000


def test_explicit_service_flowlet_values_override_flowlet_adaptive_defaults() -> None:
    from gatherlink.config.models import GatherlinkConfig, PathConfig, SchedulerConfig, ServiceConfig

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="flowlet_adaptive"),
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="udp-main",
                target="127.0.0.1:51820",
                scheduler_flowlet_idle_us=10_000,
                scheduler_flowlet_max_hold_us=20_000,
                scheduler_path_run_datagrams=64,
            )
        ],
    )

    runtime = expand_config(config)

    assert runtime.services[0].scheduler_flowlet_idle_us == 10_000
    assert runtime.services[0].scheduler_flowlet_max_hold_us == 20_000
    assert runtime.services[0].scheduler_path_run_datagrams == 64


def test_per_service_path_policy_expands_to_runtime_primitive() -> None:
    from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="wireguard-stable",
                target="127.0.0.1:51820",
                scheduler_path_policy="single_best_path",
            )
        ],
    )

    runtime = expand_config(config)

    assert runtime.services[0].scheduler_path_policy == "single_best_path"


def test_per_service_allowed_paths_expand_to_runtime_path_ids() -> None:
    from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[
            PathConfig(name="path-a", interface="gl-a"),
            PathConfig(name="path-b", interface="gl-b"),
            PathConfig(name="path-c", interface="gl-c"),
        ],
        services=[
            ServiceConfig(
                name="wireguard-fast",
                target="127.0.0.1:51820",
                scheduler_path_policy="weighted_round_robin",
                scheduler_allowed_paths=["path-b", "path-c"],
            )
        ],
    )

    runtime = expand_config(config)

    assert runtime.services[0].scheduler_allowed_path_ids == [1, 2]


def test_per_service_path_weights_expand_to_runtime_path_weight_pairs() -> None:
    from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[
            PathConfig(name="path-a", interface="gl-a"),
            PathConfig(name="path-b", interface="gl-b"),
            PathConfig(name="path-c", interface="gl-c"),
        ],
        services=[
            ServiceConfig(
                name="wireguard-fast",
                target="127.0.0.1:51820",
                scheduler_path_policy="weighted_round_robin",
                scheduler_allowed_paths=["path-b", "path-c"],
                scheduler_path_weights={"path-b": 3, "path-c": 7},
            )
        ],
    )

    runtime = expand_config(config)

    assert runtime.services[0].scheduler_path_weights == [(1, 3), (2, 7)]


def test_per_service_path_weights_reject_paths_outside_allowed_set() -> None:
    from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[
            PathConfig(name="path-a", interface="gl-a"),
            PathConfig(name="path-b", interface="gl-b"),
        ],
        services=[
            ServiceConfig(
                name="wireguard-fast",
                target="127.0.0.1:51820",
                scheduler_allowed_paths=["path-a"],
                scheduler_path_weights={"path-b": 5},
            )
        ],
    )

    with pytest.raises(ValueError, match="not in scheduler_allowed_paths"):
        expand_config(config)


def test_per_service_allowed_paths_reject_unknown_path_names() -> None:
    from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="wireguard-fast",
                target="127.0.0.1:51820",
                scheduler_allowed_paths=["path-missing"],
            )
        ],
    )

    with pytest.raises(ValueError, match="unknown scheduler_allowed_paths"):
        expand_config(config)
