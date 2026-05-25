from __future__ import annotations

from gatherlink.config.models import GatherlinkConfig, PathConfig, SchedulerConfig, ServiceConfig
from gatherlink.scheduling.compiler import compile_scheduler
from gatherlink.scheduling.coordinator import (
    SchedulerPolicyCoordinator,
    choose_candidate_policy,
    known_good_fallback_policy,
)
from gatherlink.scheduling.metrics import path_pressure_from_path_stats, scheduler_metrics_from_control_metadata
from gatherlink.scheduling.scoring import score_path, score_snapshot
from gatherlink.scheduling.service_priority import service_poll_order
from gatherlink.scheduling.smoothing import SchedulerTelemetrySmoother


def test_scheduler_compiles_static_path_primitives() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="capacity_aware"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "weight": 1,
                    "tx_capacity_bps": 3_000_000,
                    "rx_capacity_bps": 1_500_000,
                    "latency_us": 12_000,
                    "loss_ppm": 250,
                    "max_in_flight_packets": 64,
                    "max_in_flight_bytes": 524_288,
                    "pacing_budget_bps": 750_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "weighted_round_robin"
    path = scheduler.paths[0]
    assert path.tx_capacity_bps == 3_000_000
    assert path.rx_capacity_bps == 1_500_000
    assert path.latency_us == 12_000
    assert path.loss_ppm == 250
    assert path.max_in_flight_packets == 64
    assert path.max_in_flight_bytes == 524_288
    assert path.pacing_budget_bps == 750_000
    assert path.weight == 3


def test_adaptive_scheduler_prefers_live_telemetry_over_startup_hints() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="adaptive"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 1_000_000, "latency_us": 50_000},
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 4_000_000, "rx_bps": 2_000_000}},
            "path_latency": {"path-a": {"tx_current_us": 10_000, "tx_mean_us": 8_000}},
        },
        default_path_ids={"path-a": 0},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    path = scheduler.paths[0]
    assert path.tx_capacity_bps == 4_000_000
    assert path.rx_capacity_bps == 2_000_000
    assert path.latency_us == 8_000
    assert path.weight > 1


def test_least_queue_scheduler_compiles_live_queue_primitives() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="least_queue"),
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata({}, default_path_ids={"path-a": 0})
    telemetry.paths["path-a"] = telemetry.paths["path-a"].model_copy(
        update={
            "queue_depth_packets": 7,
            "queue_depth_bytes": 8192,
            "queue_oldest_age_us": 12_000,
        }
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    path = scheduler.paths[0]
    assert path.queue_depth_packets == 7
    assert path.queue_depth_bytes == 8192
    assert path.queue_oldest_age_us == 12_000


def test_ordered_multipath_compiles_reorder_and_in_flight_credits() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1200,
                    "tx_capacity_bps": 1_000_000_000,
                    "latency_us": 10_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    path = scheduler.paths[0]
    assert scheduler.mode == "ordered_multipath"
    assert path.reorder_hold_us == 12_500
    assert path.max_in_flight_bytes == 2_500_000
    assert path.max_in_flight_packets == 2083
    assert path.pacing_budget_bps == 0


def test_ordered_multipath_preserves_explicit_pacing_budget() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1200,
                    "tx_capacity_bps": 1_000_000_000,
                    "latency_us": 10_000,
                    "pacing_budget_bps": 750_000_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    path = compile_scheduler(config).paths[0]

    assert path.pacing_budget_bps == 750_000_000


def test_ordered_multipath_uses_reorder_hold_for_credit_without_latency() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1443,
                    "tx_capacity_bps": 300_000_000,
                    "reorder_hold_us": 50_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    path = compile_scheduler(config).paths[0]

    assert path.latency_us == 50_000
    assert path.reorder_hold_us == 50_000
    assert path.max_in_flight_bytes == 3_750_000
    assert path.max_in_flight_packets == 2598
    assert path.pacing_budget_bps == 0


def test_ordered_multipath_releases_more_credit_for_real_data_latency() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1200,
                    "tx_capacity_bps": 1_000_000_000,
                    "latency_us": 10_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_latency": {
                "path-a": {
                    "tx_mean_us": 10_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                }
            }
        },
        default_path_ids={"path-a": 0},
    )

    path = compile_scheduler(config, telemetry=telemetry).paths[0]

    assert path.max_in_flight_bytes == 5_000_000
    assert path.max_in_flight_packets == 4166


def test_ordered_multipath_uses_receiver_capacity_for_credit_when_narrower() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1200,
                    "tx_capacity_bps": 1_000_000_000,
                    "latency_us": 10_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_capacity": {"path-a": {"rx_bps": 250_000_000}}},
        default_path_ids={"path-a": 0},
    )

    path = compile_scheduler(config, telemetry=telemetry).paths[0]

    assert path.max_in_flight_bytes == 625_000
    assert path.max_in_flight_packets == 520


def test_ordered_multipath_receiver_pressure_reduces_compiled_credit() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1200,
                    "tx_capacity_bps": 1_000_000_000,
                    "latency_us": 10_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_pressure": {
                "path-a": {
                    "loss_ppm": 20_000,
                    "queue_depth_packets": 256,
                    "receive_gaps": 2048,
                    "reorder_depth_packets": 1024,
                    "local_drops": 1,
                }
            }
        },
        default_path_ids={"path-a": 0},
    )

    path = compile_scheduler(config, telemetry=telemetry).paths[0]

    assert path.max_in_flight_bytes == 312_500
    assert path.max_in_flight_packets == 260


def test_ordered_multipath_receiver_pressure_ratio_tightens_credit() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1200,
                    "tx_capacity_bps": 1_000_000_000,
                    "latency_us": 10_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_pressure": {
                "path-a": {
                    "observed_packets": 1_000,
                    "receive_gaps": 40,
                    "local_drops": 10,
                }
            }
        },
        default_path_ids={"path-a": 0},
    )

    path = compile_scheduler(config, telemetry=telemetry).paths[0]

    assert path.max_in_flight_bytes < 2_500_000
    assert path.max_in_flight_bytes == 357_142


def test_ordered_multipath_reorder_hold_includes_jitter_margin() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={
                    "mtu": 1200,
                    "tx_capacity_bps": 100_000_000,
                    "latency_us": 10_000,
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_latency": {"path-a": {"tx_jitter_us": 20_000}}},
        default_path_ids={"path-a": 0},
    )

    path = compile_scheduler(config, telemetry=telemetry).paths[0]

    assert path.reorder_hold_us == 52_500


def test_scheduler_telemetry_ignores_invalid_or_negative_control_values() -> None:
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": "bad", "rx_bps": -1}},
            "path_latency": {"path-a": {"tx_current_us": -10, "tx_mean_us": "also-bad"}},
        },
        default_path_ids={"path-a": 0},
    )

    metrics = telemetry.paths["path-a"]

    assert metrics.tx_capacity_bps is None
    assert metrics.rx_capacity_bps is None
    assert metrics.tx_latency_current_us is None
    assert metrics.tx_latency_mean_us is None


def test_path_pressure_exports_observed_packets_for_ratio_based_scheduler_decisions() -> None:
    pressure = path_pressure_from_path_stats(
        {
            "path-a": {
                "packets": 1234,
                "packets_needing_reorder": 42,
            }
        }
    )
    telemetry = scheduler_metrics_from_control_metadata({"path_pressure": pressure}, default_path_ids={"path-a": 0})

    assert pressure["path-a"]["observed_packets"] == 1234
    assert telemetry.paths["path-a"].observed_packets == 1234
    assert telemetry.paths["path-a"].receive_gaps == 42


def test_python_scheduler_policies_compile_to_distinct_rust_targets() -> None:
    expected = {
        "round_robin": "round_robin",
        "weighted_round_robin": "weighted_round_robin",
        "srtt": "lowest_latency",
        "lowest_latency": "lowest_latency",
        "loss_aware": "loss_aware",
        "capacity_aware": "weighted_round_robin",
        "least_queue": "least_queue",
        "earliest_completion_first": "earliest_completion_first",
        "blocking_estimation": "blocking_estimation",
        "ordered_multipath": "ordered_multipath",
        "ordered_multipath_capacity_aware": "ordered_multipath",
        "single_best_path": "weighted_round_robin",
        "arrival_guarded_capacity": "weighted_round_robin",
        "flowlet_adaptive": "adaptive",
        "latency_guarded_capacity": "weighted_round_robin",
        "balanced": "balanced",
        "adaptive": "adaptive",
        "coordinated_adaptive": "weighted_round_robin",
    }

    for policy, rust_mode in expected.items():
        config = GatherlinkConfig(
            schema_version=1,
            node="local",
            role="client",
            peer="remote",
            scheduler=SchedulerConfig(mode=policy),
            paths=[PathConfig(name="path-a", interface="gl-a")],
            services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
        )

        assert compile_scheduler(config).mode == rust_mode


def test_coordinated_adaptive_fallback_uses_configured_capacity_hints() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert known_good_fallback_policy(config, scheduler_metrics_from_control_metadata({})) == "capacity_aware"
    assert scheduler.mode == "weighted_round_robin"
    assert scheduler.paths[0].weight == 300
    assert scheduler.paths[1].weight == 700


def test_coordinated_adaptive_tcp_bias_compiles_single_best_path_until_telemetry_switch() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "weighted_round_robin"
    assert [path.state for path in scheduler.paths] == ["drain", "active"]


def test_coordinated_adaptive_auto_uses_tcp_ordered_service_class_as_initial_bias() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820", traffic_class="tcp_ordered")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "weighted_round_robin"
    assert [path.state for path in scheduler.paths] == ["drain", "active"]


def test_coordinated_adaptive_candidate_prefers_ordered_when_reorder_is_high() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"path-a": {"receive_gaps": 2048}}},
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="capacity_aware")

    assert candidate == "ordered_multipath_capacity_aware"
    assert "reorder" in reason
    assert "capacity_confident" in signals


def test_coordinated_adaptive_tcp_bias_uses_latency_guard_for_reorder_pressure() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"path-a": {"receive_gaps": 2048}}},
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="capacity_aware")

    assert candidate == "single_best_path"
    assert "reorder pressure favors best path" in reason
    assert "reorder_pressure" in signals


def test_coordinated_adaptive_tcp_bias_uses_single_best_path_until_latency_is_known() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 100_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 100_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata({}, default_path_ids={"path-a": 0, "path-b": 1})

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="adaptive")

    assert candidate == "single_best_path"
    assert "best path" in reason
    assert "capacity_hints" in signals


def test_tcp_biased_known_good_fallback_is_single_best_even_with_latency_spread() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_latency": {
                "path-a": {"tx_mean_us": 10_000},
                "path-b": {"tx_mean_us": 80_000},
            }
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    assert known_good_fallback_policy(config, telemetry) == "single_best_path"


def test_coordinated_adaptive_tcp_bias_keeps_clean_paths_on_best_path() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 220_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 240_000_000}),
            PathConfig(name="path-c", interface="gl-c", scheduler={"tx_capacity_bps": 200_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {
                "path-a": {"tx_bps": 220_000_000},
                "path-b": {"tx_bps": 240_000_000},
                "path-c": {"tx_bps": 200_000_000},
            },
            "path_latency": {
                "path-a": {
                    "tx_mean_us": 20_000,
                    "tx_jitter_us": 2_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
                "path-b": {
                    "tx_mean_us": 22_000,
                    "tx_jitter_us": 3_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
                "path-c": {
                    "tx_mean_us": 24_000,
                    "tx_jitter_us": 2_500,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
            },
            "path_pressure": {
                "path-a": {"observed_packets": 10_000},
                "path-b": {"observed_packets": 10_000},
                "path-c": {"observed_packets": 10_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1, "path-c": 2},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="single_best_path")
    scheduler = compile_scheduler(config, telemetry=telemetry, effective_mode=candidate)

    assert candidate == "single_best_path"
    assert "best path" in reason
    assert scheduler.mode == "weighted_round_robin"
    assert sum(path.state == "active" for path in scheduler.paths) == 1
    assert all(path.pacing_budget_bps == 0 for path in scheduler.paths)
    assert "capacity_confident" in signals


def test_coordinated_adaptive_tcp_bias_rejects_ordered_multipath_under_jitter() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 220_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 240_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 220_000_000}, "path-b": {"tx_bps": 240_000_000}},
            "path_latency": {
                "path-a": {"tx_mean_us": 20_000, "tx_jitter_us": 25_000},
                "path-b": {"tx_mean_us": 70_000, "tx_jitter_us": 4_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="single_best_path")

    assert candidate == "single_best_path"
    assert "best path" in reason
    assert "jitter_pressure" in signals


def test_coordinated_adaptive_tcp_bias_does_not_hold_ordered_for_tcp_tunnels() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 800_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 400_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {
                "path-a": {"tx_bps": 800_000_000},
                "path-b": {"tx_bps": 400_000_000},
            },
            "path_latency": {
                "path-a": {"tx_mean_us": 20_000, "tx_jitter_us": 30_000},
                "path-b": {"tx_mean_us": 90_000, "tx_jitter_us": 20_000},
            },
            "path_pressure": {
                "path-a": {"observed_packets": 500_000, "receive_gaps": 10_000},
                "path-b": {"observed_packets": 20_000, "receive_gaps": 5_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, _signals = choose_candidate_policy(
        config,
        telemetry,
        fallback="ordered_multipath_capacity_aware",
    )

    assert candidate == "single_best_path"
    assert "best path" in reason


def test_coordinated_adaptive_tcp_bias_keeps_proven_bursty_jitter_on_best_path() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 800_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 400_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {
                "path-a": {"tx_bps": 800_000_000},
                "path-b": {"tx_bps": 400_000_000},
            },
            "path_latency": {
                "path-a": {
                    "tx_mean_us": 16_000,
                    "tx_jitter_us": 14_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
                "path-b": {
                    "tx_mean_us": 65_000,
                    "tx_jitter_us": 51_000,
                    "source": "clock-synced-one-way",
                    "confidence": "good",
                    "clock_error_us": 35_000,
                    "rtt_us": 105_000,
                },
            },
            "path_pressure": {
                "path-a": {"observed_packets": 600_000, "receive_gaps": 0},
                "path-b": {"observed_packets": 10_000, "receive_gaps": 0},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="single_best_path")

    assert candidate == "single_best_path"
    assert "best path" in reason
    assert "jitter_pressure" in signals


def test_coordinated_adaptive_tcp_bias_rejects_tiny_slow_paths_for_ordered_promotion() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 800_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 160_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {
                "path-a": {"tx_bps": 800_000_000},
                "path-b": {"tx_bps": 160_000_000},
            },
            "path_latency": {
                "path-a": {
                    "tx_mean_us": 16_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
                "path-b": {
                    "tx_mean_us": 65_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
            },
            "path_pressure": {
                "path-a": {"observed_packets": 600_000},
                "path-b": {"observed_packets": 10_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, _signals = choose_candidate_policy(config, telemetry, fallback="single_best_path")

    assert candidate == "single_best_path"
    assert "known-good fallback" in reason


def test_coordinated_adaptive_tcp_bias_prefers_best_path_for_skewed_jittery_links() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 800_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 160_000_000}),
            PathConfig(name="path-c", interface="gl-c", scheduler={"tx_capacity_bps": 85_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {
                "path-a": {"tx_bps": 800_000_000},
                "path-b": {"tx_bps": 160_000_000},
                "path-c": {"tx_bps": 85_000_000},
            },
            "path_latency": {
                "path-a": {"tx_mean_us": 16_000, "tx_jitter_us": 25_000},
                "path-b": {"tx_mean_us": 60_000, "tx_jitter_us": 35_000},
                "path-c": {"tx_mean_us": 90_000, "tx_jitter_us": 20_000},
            },
            "path_pressure": {
                "path-a": {"observed_packets": 800_000},
                "path-b": {"observed_packets": 10_000},
                "path-c": {"observed_packets": 5_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1, "path-c": 2},
    )

    candidate, reason, _signals = choose_candidate_policy(config, telemetry, fallback="flowlet_adaptive")

    assert candidate == "single_best_path"
    assert "skewed path capacity" in reason


def test_coordinated_adaptive_tcp_bias_uses_reorder_ratio_not_lifetime_total() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 220_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 240_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 220_000_000}, "path-b": {"tx_bps": 240_000_000}},
            "path_latency": {
                "path-a": {
                    "tx_mean_us": 20_000,
                    "tx_jitter_us": 2_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
                "path-b": {
                    "tx_mean_us": 22_000,
                    "tx_jitter_us": 3_000,
                    "source": "data-traffic-one-way",
                    "confidence": "good",
                },
            },
            "path_pressure": {
                "path-a": {"receive_gaps": 1_000, "observed_packets": 1_000_000},
                "path-b": {"receive_gaps": 1_200, "observed_packets": 1_000_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, _signals = choose_candidate_policy(config, telemetry, fallback="single_best_path")

    assert candidate == "single_best_path"
    assert "best path" in reason


def test_coordinated_adaptive_tcp_bias_requires_real_data_latency_for_ordered_promotion() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 220_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 240_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 220_000_000}, "path-b": {"tx_bps": 240_000_000}},
            "path_latency": {
                "path-a": {"tx_mean_us": 20_000, "tx_jitter_us": 2_000, "source": "clock-synced-one-way"},
                "path-b": {"tx_mean_us": 22_000, "tx_jitter_us": 3_000, "source": "clock-synced-one-way"},
            },
            "path_pressure": {
                "path-a": {"receive_gaps": 1_000, "observed_packets": 1_000_000},
                "path-b": {"receive_gaps": 1_200, "observed_packets": 1_000_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, _signals = choose_candidate_policy(config, telemetry, fallback="single_best_path")

    assert candidate == "single_best_path"
    assert "best path" in reason


def test_coordinated_adaptive_tcp_known_good_fallback_is_single_best_for_skewed_capacities() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 800_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 160_000_000}),
            PathConfig(name="path-c", interface="gl-c", scheduler={"tx_capacity_bps": 85_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )

    fallback = known_good_fallback_policy(config, scheduler_metrics_from_control_metadata({}))

    assert fallback == "single_best_path"


def test_coordinated_adaptive_tcp_bias_rejects_high_reorder_ratio() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 220_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 240_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 220_000_000}, "path-b": {"tx_bps": 240_000_000}},
            "path_latency": {
                "path-a": {"tx_mean_us": 20_000, "tx_jitter_us": 2_000},
                "path-b": {"tx_mean_us": 22_000, "tx_jitter_us": 3_000},
            },
            "path_pressure": {
                "path-a": {"receive_gaps": 20_000, "reorder_depth_packets": 20_000, "observed_packets": 1_000_000},
                "path-b": {"receive_gaps": 500, "observed_packets": 1_000_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="single_best_path")

    assert candidate == "single_best_path"
    assert "best path" in reason
    assert "reorder_pressure" in signals


def test_coordinated_adaptive_udp_bias_prefers_capacity_aggregation() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive", traffic_bias="udp"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 100_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 100_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata({}, default_path_ids={"path-a": 0, "path-b": 1})

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="adaptive")

    assert candidate == "capacity_aware"
    assert "udp bias" in reason
    assert "capacity_hints" in signals


def test_coordinated_adaptive_auto_uses_udp_bulk_service_class_for_capacity() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 100_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 100_000_000}),
        ],
        services=[ServiceConfig(name="fast", target="127.0.0.1:51820", traffic_class="udp_bulk")],
    )
    telemetry = scheduler_metrics_from_control_metadata({}, default_path_ids={"path-a": 0, "path-b": 1})

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="adaptive")

    assert candidate == "capacity_aware"
    assert "udp bias" in reason
    assert "service_udp_bulk" in signals


def test_coordinated_adaptive_auto_keeps_mixed_services_on_bulk_capable_baseline() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 100_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 100_000_000}),
        ],
        services=[
            ServiceConfig(name="stable", target="127.0.0.1:51820", traffic_class="tcp_ordered"),
            ServiceConfig(name="fast", target="127.0.0.1:51821", traffic_class="udp_bulk"),
        ],
    )
    telemetry = scheduler_metrics_from_control_metadata({}, default_path_ids={"path-a": 0, "path-b": 1})

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="adaptive")

    assert candidate == "capacity_aware"
    assert "mixed service" in reason
    assert "service_mixed_classes" in signals


def test_coordinated_adaptive_candidate_uses_latency_guard_for_skew_with_pressure() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[PathConfig(name="path-a", interface="gl-a"), PathConfig(name="path-b", interface="gl-b")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_latency": {"path-a": {"tx_mean_us": 10_000}, "path-b": {"tx_mean_us": 80_000}},
            "path_pressure": {"path-b": {"queue_depth_packets": 800}},
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="capacity_aware")

    assert candidate == "latency_guarded_capacity"
    assert "latency" in reason
    assert "latency_spread" in signals


def test_coordinated_adaptive_candidate_uses_arrival_guard_when_capacity_and_queue_are_known() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 800_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 160_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 800_000_000}, "path-b": {"tx_bps": 160_000_000}},
            "path_latency": {"path-a": {"tx_mean_us": 10_000}, "path-b": {"tx_mean_us": 90_000}},
            "path_pressure": {"path-b": {"queue_depth_packets": 800}},
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="capacity_aware")

    assert candidate == "arrival_guarded_capacity"
    assert "arrival guard" in reason
    assert "latency_spread" in signals
    assert "queue_pressure" in signals


def test_coordinated_adaptive_candidate_uses_flowlets_for_jitter_pressure() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 180_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 120_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 180_000_000}, "path-b": {"tx_bps": 120_000_000}},
            "path_latency": {
                "path-a": {"tx_mean_us": 15_000, "tx_jitter_us": 25_000},
                "path-b": {"tx_mean_us": 60_000, "tx_jitter_us": 8_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="capacity_aware")

    assert candidate == "flowlet_adaptive"
    assert "flowlet" in reason
    assert "jitter_pressure" in signals


def test_coordinated_adaptive_jitter_takes_priority_over_ordered_reorder_pressure() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 180_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 120_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 180_000_000}, "path-b": {"tx_bps": 120_000_000}},
            "path_latency": {
                "path-a": {"tx_mean_us": 15_000, "tx_jitter_us": 40_000},
                "path-b": {"tx_mean_us": 60_000},
            },
            "path_pressure": {"path-a": {"receive_gaps": 4096}},
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="capacity_aware")

    assert candidate == "flowlet_adaptive"
    assert "jitter" in reason
    assert "reorder_pressure" in signals


def test_coordinated_adaptive_ignores_brief_jitter_without_latency_spread() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 600_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 900_000_000}),
            PathConfig(name="path-c", interface="gl-c", scheduler={"tx_capacity_bps": 1_300_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {
                "path-a": {"tx_bps": 600_000_000},
                "path-b": {"tx_bps": 900_000_000},
                "path-c": {"tx_bps": 1_300_000_000},
            },
            "path_latency": {
                "path-a": {"tx_mean_us": 10_000, "tx_jitter_us": 30_000},
                "path-b": {"tx_mean_us": 11_000},
                "path-c": {"tx_mean_us": 12_000},
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1, "path-c": 2},
    )

    candidate, reason, signals = choose_candidate_policy(config, telemetry, fallback="capacity_aware")

    assert candidate == "capacity_aware"
    assert "stable" in reason
    assert "jitter_pressure" in signals


def test_coordinated_adaptive_requires_confidence_and_dwell_before_switching() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"path-a": {"receive_gaps": 2048}}},
        default_path_ids={"path-a": 0, "path-b": 1},
    )
    telemetry.paths["path-a"] = telemetry.paths["path-a"].model_copy(update={"observed_packets": 1000})
    now = [0.0]
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=3000,
        minimum_dwell_seconds=0.0,
        required_confidence_windows=2,
        now=lambda: now[0],
    )

    assert coordinator.choose_effective_mode(config, telemetry) == "capacity_aware"
    assert coordinator.last_decision is not None
    assert coordinator.last_decision.blocked_by == "waiting_for_confidence"
    assert coordinator.choose_effective_mode(config, telemetry) == "capacity_aware"
    assert coordinator.last_decision.blocked_by == "minimum_packet_dwell"
    assert coordinator.choose_effective_mode(config, telemetry) == "ordered_multipath_capacity_aware"
    assert coordinator.last_decision.switched is True
    assert coordinator.recent_decisions()[-1]["packets_since_switch"] == 0


def test_coordinated_adaptive_time_guard_blocks_same_second_flap() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"path-a": {"receive_gaps": 2048}}},
        default_path_ids={"path-a": 0, "path-b": 1},
    )
    telemetry.paths["path-a"] = telemetry.paths["path-a"].model_copy(update={"observed_packets": 10_000})
    now = [100.0]
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=1,
        minimum_dwell_seconds=5.0,
        required_confidence_windows=1,
        now=lambda: now[0],
    )

    assert coordinator.choose_effective_mode(config, telemetry) == "capacity_aware"
    assert coordinator.last_decision is not None
    assert coordinator.last_decision.blocked_by == "minimum_time_dwell"
    now[0] += 5.0

    assert coordinator.choose_effective_mode(config, telemetry) == "ordered_multipath_capacity_aware"
    assert coordinator.last_decision.switched is True


def test_flowlet_adaptive_compiles_adaptive_weight_from_telemetry() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="flowlet_adaptive"),
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_capacity": {"path-a": {"tx_bps": 4_000_000}}, "path_latency": {"path-a": {"tx_mean_us": 8_000}}},
        default_path_ids={"path-a": 0},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.mode == "adaptive"
    assert scheduler.paths[0].weight > 1


def test_flowlet_adaptive_jitter_reduces_compiled_weight() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="flowlet_adaptive"),
        paths=[PathConfig(name="path-a", interface="gl-a"), PathConfig(name="path-b", interface="gl-b")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 100_000_000}, "path-b": {"tx_bps": 100_000_000}},
            "path_latency": {"path-a": {"tx_jitter_us": 50_000}, "path-b": {"tx_jitter_us": 0}},
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.paths[0].weight < scheduler.paths[1].weight


def test_capacity_aware_keeps_capacity_share_when_paths_report_expected_loss() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="capacity_aware"),
        paths=[
            PathConfig(name="clean", interface="gl-a", scheduler={"tx_capacity_bps": 100_000_000}),
            PathConfig(name="pressured", interface="gl-b", scheduler={"tx_capacity_bps": 100_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_pressure": {
                "pressured": {
                    "loss_ppm": 120_000,
                    "queue_depth_packets": 512,
                    "queue_oldest_age_us": 1_000_000,
                    "local_drops": 100_000,
                    "receive_gaps": 4096,
                }
            }
        },
        default_path_ids={"clean": 0, "pressured": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.mode == "weighted_round_robin"
    assert scheduler.paths[0].weight == 100
    assert scheduler.paths[1].weight == 100


def test_coordinated_adaptive_capacity_fallback_keeps_capacity_share_under_expected_loss() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(name="clean", interface="gl-a", scheduler={"tx_capacity_bps": 100_000_000}),
            PathConfig(name="pressured", interface="gl-b", scheduler={"tx_capacity_bps": 100_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_pressure": {
                "pressured": {
                    "loss_ppm": 120_000,
                    "queue_depth_packets": 512,
                    "queue_oldest_age_us": 1_000_000,
                    "local_drops": 100_000,
                    "receive_gaps": 4096,
                }
            }
        },
        default_path_ids={"clean": 0, "pressured": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry, effective_mode="capacity_aware")

    assert scheduler.mode == "weighted_round_robin"
    assert scheduler.paths[0].weight == 100
    assert scheduler.paths[1].weight == 100


def test_ordered_multipath_capacity_aware_keeps_ordered_mode_with_capacity_weights() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath_capacity_aware"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 300_000_000, "latency_us": 20_000},
            ),
            PathConfig(
                name="path-b",
                interface="gl-b",
                scheduler={"tx_capacity_bps": 900_000_000, "latency_us": 20_000},
            ),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "ordered_multipath"
    assert scheduler.paths[0].weight == 300
    assert scheduler.paths[1].weight == 900
    assert scheduler.paths[0].max_in_flight_bytes > 0
    assert scheduler.paths[1].max_in_flight_bytes > scheduler.paths[0].max_in_flight_bytes
    assert scheduler.paths[0].reorder_hold_us == 25_000
    assert scheduler.paths[1].reorder_hold_us == 25_000


def test_ordered_multipath_capacity_aware_keeps_small_mtu_reorder_hold_tight() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath_capacity_aware"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={"mtu": 1200, "tx_capacity_bps": 300_000_000, "latency_us": 20_000},
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    path = compile_scheduler(config).paths[0]

    assert path.reorder_hold_us == 25_000


def test_ordered_multipath_capacity_aware_uses_base_hold_for_small_mtu_without_latency() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath_capacity_aware"),
        paths=[PathConfig(name="path-a", interface="gl-a", scheduler={"mtu": 1200, "tx_capacity_bps": 300_000_000})],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    path = compile_scheduler(config).paths[0]

    assert path.reorder_hold_us == 2_500


def test_ordered_multipath_capacity_aware_reduces_pressured_path_weight_and_credit() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath_capacity_aware"),
        paths=[
            PathConfig(
                name="clean",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 300_000_000, "latency_us": 20_000},
            ),
            PathConfig(
                name="pressured",
                interface="gl-b",
                scheduler={"tx_capacity_bps": 300_000_000, "latency_us": 20_000},
            ),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_pressure": {
                "pressured": {
                    "local_drops": 65_536,
                    "queue_depth_packets": 512,
                    "receive_gaps": 4096,
                    "loss_ppm": 80_000,
                }
            },
            "path_latency": {"pressured": {"tx_jitter_us": 40_000}},
        },
        default_path_ids={"clean": 0, "pressured": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.mode == "ordered_multipath"
    assert scheduler.paths[0].weight == 300
    assert scheduler.paths[1].weight < scheduler.paths[0].weight
    assert scheduler.paths[1].max_in_flight_bytes < scheduler.paths[0].max_in_flight_bytes


def test_ordered_multipath_capacity_aware_drains_paths_outside_fast_reorder_window() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="ordered_multipath_capacity_aware"),
        paths=[
            PathConfig(
                name="fiber",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 800_000_000, "latency_us": 12_000},
            ),
            PathConfig(
                name="cellular",
                interface="gl-b",
                scheduler={"tx_capacity_bps": 160_000_000, "latency_us": 55_000},
            ),
            PathConfig(
                name="backup",
                interface="gl-c",
                scheduler={"tx_capacity_bps": 85_000_000, "latency_us": 90_000},
            ),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "ordered_multipath"
    assert [path.state for path in scheduler.paths] == ["active", "drain", "drain"]
    assert [path.weight for path in scheduler.paths] == [800, 1, 1]


def test_single_best_path_drains_lower_capacity_paths_in_python() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="single_best_path"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 300_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 700_000_000}),
            PathConfig(name="path-c", interface="gl-c", scheduler={"tx_capacity_bps": 500_000_000}),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "weighted_round_robin"
    assert [path.state for path in scheduler.paths] == ["drain", "active", "drain"]
    assert [path.weight for path in scheduler.paths] == [1, 700, 1]


def test_single_best_path_uses_latency_tie_breaker() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="single_best_path"),
        paths=[
            PathConfig(
                name="path-a",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 500_000_000, "latency_us": 40_000},
            ),
            PathConfig(
                name="path-b",
                interface="gl-b",
                scheduler={"tx_capacity_bps": 500_000_000, "latency_us": 10_000},
            ),
        ],
        services=[ServiceConfig(name="wireguard", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert [path.state for path in scheduler.paths] == ["drain", "active"]


def test_latency_guarded_capacity_is_python_policy_over_weighted_rust_primitives() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="latency_guarded_capacity"),
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "weighted_round_robin"


def test_latency_guarded_capacity_drains_latency_outliers_in_python() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="latency_guarded_capacity"),
        paths=[
            PathConfig(name="fast", interface="gl-a", scheduler={"latency_us": 10_000, "tx_capacity_bps": 1_000_000}),
            PathConfig(name="slow", interface="gl-b", scheduler={"latency_us": 80_000, "tx_capacity_bps": 10_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "weighted_round_robin"
    assert scheduler.paths[0].state == "active"
    assert scheduler.paths[1].state == "drain"


def test_latency_guarded_capacity_keeps_paths_when_latency_is_unknown() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="latency_guarded_capacity"),
        paths=[
            PathConfig(name="path-a", interface="gl-a"),
            PathConfig(name="path-b", interface="gl-b"),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert [path.state for path in scheduler.paths] == ["active", "active"]


def test_arrival_guarded_capacity_demotes_paths_outside_reorder_budget() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="arrival_guarded_capacity"),
        paths=[
            PathConfig(
                name="fast",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 800_000_000, "latency_us": 10_000, "reorder_hold_us": 20_000},
            ),
            PathConfig(
                name="slow",
                interface="gl-b",
                scheduler={"tx_capacity_bps": 160_000_000, "latency_us": 90_000, "reorder_hold_us": 20_000},
            ),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "weighted_round_robin"
    assert scheduler.paths[0].state == "active"
    assert scheduler.paths[1].state == "drain"
    assert scheduler.paths[1].weight == 1


def test_arrival_guarded_capacity_keeps_slow_path_with_sufficient_reorder_budget() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="arrival_guarded_capacity"),
        paths=[
            PathConfig(
                name="fast",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 800_000_000, "latency_us": 10_000, "reorder_hold_us": 150_000},
            ),
            PathConfig(
                name="slow",
                interface="gl-b",
                scheduler={"tx_capacity_bps": 160_000_000, "latency_us": 90_000, "reorder_hold_us": 150_000},
            ),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert [path.state for path in scheduler.paths] == ["active", "active"]


def test_arrival_guarded_capacity_uses_queue_pressure_in_prediction() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="arrival_guarded_capacity"),
        paths=[
            PathConfig(
                name="clean",
                interface="gl-a",
                scheduler={"tx_capacity_bps": 400_000_000, "latency_us": 10_000, "reorder_hold_us": 50_000},
            ),
            PathConfig(
                name="queued",
                interface="gl-b",
                scheduler={"tx_capacity_bps": 400_000_000, "latency_us": 15_000, "reorder_hold_us": 50_000},
            ),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"queued": {"queue_depth_packets": 1000, "queue_oldest_age_us": 20_000}}},
        default_path_ids={"clean": 0, "queued": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.paths[0].state == "active"
    assert scheduler.paths[1].state == "drain"


def test_scheduler_telemetry_carries_pressure_and_jitter_facts() -> None:
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 4_000_000}},
            "path_latency": {
                "path-a": {
                    "tx_mean_us": 8_000,
                    "tx_jitter_us": 300,
                    "tx_p95_us": 9_000,
                    "rx_jitter_us": 450,
                    "rx_p95_us": 10_000,
                }
            },
            "path_pressure": {
                "path-a": {
                    "loss_ppm": 1200,
                    "queue_depth_packets": 7,
                    "queue_depth_bytes": 8192,
                    "queue_oldest_age_us": 12_000,
                    "send_failures": 2,
                    "receive_gaps": 3,
                    "reorder_depth_packets": 4,
                    "local_drops": 5,
                    "scheduler_in_flight_packets": 6,
                    "scheduler_in_flight_bytes": 7000,
                    "scheduler_predicted_delivery_us": 8000,
                    "reorder_buffer_packets": 9,
                    "reorder_buffer_oldest_age_us": 10_000,
                    "socket_receive_buffer_bytes": 11_000,
                    "socket_send_buffer_bytes": 12_000,
                    "socket_drain_quantum": 16,
                    "stale_control_age_us": 60_000_000,
                }
            },
        },
        default_path_ids={"path-a": 0},
    )

    metrics = telemetry.paths["path-a"]

    assert metrics.tx_jitter_us == 300
    assert metrics.rx_jitter_us == 450
    assert metrics.tx_p95_us == 9_000
    assert metrics.rx_p95_us == 10_000
    assert metrics.loss_ppm == 1200
    assert metrics.queue_depth_packets == 7
    assert metrics.queue_depth_bytes == 8192
    assert metrics.queue_oldest_age_us == 12_000
    assert metrics.send_failures == 2
    assert metrics.receive_gaps == 3
    assert metrics.reorder_depth_packets == 4
    assert metrics.local_drops == 5
    assert metrics.scheduler_in_flight_packets == 6
    assert metrics.scheduler_in_flight_bytes == 7000
    assert metrics.scheduler_predicted_delivery_us == 8000
    assert metrics.reorder_buffer_packets == 9
    assert metrics.reorder_buffer_oldest_age_us == 10_000
    assert metrics.socket_receive_buffer_bytes == 11_000
    assert metrics.socket_send_buffer_bytes == 12_000
    assert metrics.socket_drain_quantum == 16
    assert metrics.stale_control_age_us == 60_000_000
    assert metrics.estimated_earliest_delivery_us(payload_bytes=1000) == 10_000


def test_scheduler_telemetry_ignores_invalid_pressure_values() -> None:
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_pressure": {
                "path-a": {
                    "loss_ppm": "bad",
                    "queue_depth_packets": -1,
                    "send_failures": "nope",
                }
            }
        },
        default_path_ids={"path-a": 0},
    )

    metrics = telemetry.paths["path-a"]

    assert metrics.loss_ppm == 0
    assert metrics.queue_depth_packets == 0
    assert metrics.send_failures == 0


def test_scheduler_ignores_unreasonable_latency_before_rust_compile() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="lowest_latency"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"latency_us": 9_000_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"latency_us": 5_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_latency": {"path-a": {"tx_mean_us": 9_000_000_000}, "path-b": {"tx_mean_us": 5_000}}},
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.paths[0].latency_us is None
    assert scheduler.paths[1].latency_us == 5_000


def test_scheduler_path_scoring_explains_healthy_and_degraded_paths() -> None:
    telemetry = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 4_000_000}, "path-b": {"tx_bps": 4_000_000}},
            "path_pressure": {
                "path-b": {
                    "queue_depth_packets": 300,
                    "queue_depth_bytes": 1024 * 1024,
                    "send_failures": 2,
                }
            },
        },
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    scores = score_snapshot(telemetry.paths)

    assert scores["path-a"].health == "alive"
    assert scores["path-a"].reasons == ("healthy",)
    assert scores["path-a"].capacity_confidence_ppm == 1_000_000
    assert scores["path-b"].health == "degraded"
    assert "queue_pressure" in scores["path-b"].reasons
    assert "send_failures" in scores["path-b"].reasons
    assert scores["path-b"].weight < scores["path-a"].weight
    assert scores["path-b"].capacity_confidence_ppm < scores["path-a"].capacity_confidence_ppm
    assert "capacity_confidence_ppm" in scores["path-b"].export_dict()


def test_scheduler_path_scoring_marks_stale_control_down() -> None:
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"path-a": {"stale_control_age_us": 130_000_000}}},
        default_path_ids={"path-a": 0},
    )

    score = score_path(telemetry.paths["path-a"])

    assert score.health == "down"
    assert score.weight == 1
    assert "control_stale" in score.reasons
    assert score.export_dict()["health"] == "down"


def test_compile_scheduler_drains_paths_with_stale_control_metadata() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="adaptive"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"tx_capacity_bps": 4_000_000}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"tx_capacity_bps": 4_000_000}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"path-b": {"stale_control_age_us": 130_000_000}}},
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.paths[0].state == "active"
    assert scheduler.paths[1].state == "drain"
    assert scheduler.paths[1].weight == 1


def test_compile_scheduler_reduces_weight_for_degraded_paths() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        scheduler=SchedulerConfig(mode="weighted_round_robin"),
        paths=[
            PathConfig(name="path-a", interface="gl-a", scheduler={"weight": 10}),
            PathConfig(name="path-b", interface="gl-b", scheduler={"weight": 10}),
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )
    telemetry = scheduler_metrics_from_control_metadata(
        {"path_pressure": {"path-b": {"send_failures": 2}}},
        default_path_ids={"path-a": 0, "path-b": 1},
    )

    scheduler = compile_scheduler(config, telemetry=telemetry)

    assert scheduler.paths[0].weight == 10
    assert scheduler.paths[1].state == "active"
    assert scheduler.paths[1].weight < scheduler.paths[0].weight


def test_service_priority_poll_order_keeps_low_priority_visible() -> None:
    from gatherlink.config.expansion import expand_config

    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(name="bulk", listen="127.0.0.1:55180", target="127.0.0.1:51820", priority="bulk"),
            ServiceConfig(name="critical", listen="127.0.0.1:55181", target="127.0.0.1:51821", priority="critical"),
            ServiceConfig(name="silent", target="127.0.0.1:51822", priority="high"),
        ],
    )

    order = service_poll_order(expand_config(config).services)

    assert order.count("bulk") == 1
    assert order.count("critical") == 4
    assert "silent" not in order


def test_scheduler_telemetry_smoother_damps_noisy_scalars_but_keeps_pressure_immediate() -> None:
    smoother = SchedulerTelemetrySmoother()
    first = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 1_000_000}},
            "path_latency": {"path-a": {"tx_mean_us": 10_000}},
            "path_pressure": {"path-a": {"loss_ppm": 1000, "queue_depth_packets": 0}},
        },
        default_path_ids={"path-a": 0},
    )
    second = scheduler_metrics_from_control_metadata(
        {
            "path_capacity": {"path-a": {"tx_bps": 9_000_000}},
            "path_latency": {"path-a": {"tx_mean_us": 90_000}},
            "path_pressure": {"path-a": {"loss_ppm": 9000, "queue_depth_packets": 50}},
        },
        default_path_ids={"path-a": 0},
    )

    smoother.smooth(first)
    smoothed = smoother.smooth(second)
    metrics = smoothed.paths["path-a"]

    assert metrics.tx_capacity_bps == 3_000_000
    assert metrics.tx_latency_mean_us == 30_000
    assert metrics.loss_ppm == 3000
    assert metrics.queue_depth_packets == 50
    assert smoother.confidence("path-a") == 2
