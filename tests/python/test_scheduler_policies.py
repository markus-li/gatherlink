from __future__ import annotations

from gatherlink.config.models import GatherlinkConfig, PathConfig, SchedulerConfig, ServiceConfig
from gatherlink.scheduling.compiler import compile_scheduler
from gatherlink.scheduling.metrics import scheduler_metrics_from_control_metadata


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
                },
            )
        ],
        services=[ServiceConfig(name="udp-main", target="127.0.0.1:51820")],
    )

    scheduler = compile_scheduler(config)

    assert scheduler.mode == "capacity_aware"
    path = scheduler.paths[0]
    assert path.tx_capacity_bps == 3_000_000
    assert path.rx_capacity_bps == 1_500_000
    assert path.latency_us == 12_000
    assert path.loss_ppm == 250
    assert path.max_in_flight_packets == 64
    assert path.max_in_flight_bytes == 524_288
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


def test_python_scheduler_policies_compile_to_distinct_rust_targets() -> None:
    expected = {
        "round_robin": "round_robin",
        "weighted_round_robin": "weighted_round_robin",
        "srtt": "lowest_latency",
        "lowest_latency": "lowest_latency",
        "loss_aware": "loss_aware",
        "capacity_aware": "capacity_aware",
        "least_queue": "least_queue",
        "earliest_completion_first": "earliest_completion_first",
        "blocking_estimation": "blocking_estimation",
        "balanced": "balanced",
        "adaptive": "adaptive",
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
