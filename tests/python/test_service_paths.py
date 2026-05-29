from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, PathSchedulerConfig, SchedulerConfig, ServiceConfig
from gatherlink.config.runtime import RuntimeSchedulerConfig
from gatherlink.scheduling.metrics import PathSchedulerMetrics, SchedulerTelemetrySnapshot
from gatherlink.scheduling.service_paths import ServicePathAllocator


def _config() -> GatherlinkConfig:
    return GatherlinkConfig(
        schema_version=1,
        node="source",
        role="client",
        peer="sink",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(
                name="path-a",
                interface="lo",
                scheduler=PathSchedulerConfig(tx_capacity_bps=180_000_000, rx_capacity_bps=180_000_000),
            ),
            PathConfig(
                name="path-b",
                interface="lo",
                scheduler=PathSchedulerConfig(tx_capacity_bps=140_000_000, rx_capacity_bps=140_000_000),
            ),
            PathConfig(
                name="path-c",
                interface="lo",
                scheduler=PathSchedulerConfig(tx_capacity_bps=90_000_000, rx_capacity_bps=90_000_000),
            ),
        ],
        services=[
            ServiceConfig(
                name="stable",
                listen="127.0.0.1:10000",
                target="127.0.0.1:20000",
                priority="high",
                scheduler_path_policy="single_best_path",
                scheduler_allowed_paths=["path-a"],
                scheduler_path_weights={"path-a": 180},
            ),
            ServiceConfig(
                name="fast",
                listen="127.0.0.1:10001",
                target="127.0.0.1:20001",
                priority="bulk",
                scheduler_path_policy="weighted_round_robin",
                scheduler_allowed_paths=["path-b"],
                scheduler_path_weights={"path-b": 140},
            ),
        ],
    )


def _telemetry(**overrides: PathSchedulerMetrics) -> SchedulerTelemetrySnapshot:
    metrics = {
        "path-a": PathSchedulerMetrics(path_name="path-a", path_id=0, tx_capacity_bps=180_000_000),
        "path-b": PathSchedulerMetrics(path_name="path-b", path_id=1, tx_capacity_bps=140_000_000),
        "path-c": PathSchedulerMetrics(path_name="path-c", path_id=2, tx_capacity_bps=90_000_000),
    }
    metrics.update(overrides)
    return SchedulerTelemetrySnapshot(paths=metrics)


def test_service_path_allocator_keeps_protected_service_sticky_and_bulk_on_spare_path() -> None:
    config = _config()
    runtime = expand_config(config)
    allocator = ServicePathAllocator()

    decision = allocator.update(config, runtime, _telemetry(), {"service_stats": {}}, now=10.0)

    stable = next(service for service in decision.services if service.name == "stable")
    fast = next(service for service in decision.services if service.name == "fast")
    assert stable.scheduler_path_policy == "single_best_path"
    assert stable.scheduler_allowed_path_ids == [0]
    assert fast.scheduler_path_policy == "weighted_round_robin"
    assert fast.scheduler_allowed_path_ids == [1]


def test_service_path_allocator_fails_protected_service_over_when_current_path_is_bad() -> None:
    config = _config()
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    telemetry = _telemetry(
        **{
            "path-a": PathSchedulerMetrics(
                path_name="path-a",
                path_id=0,
                tx_capacity_bps=180_000_000,
                loss_ppm=100_000,
            )
        }
    )

    decision = allocator.update(config, runtime, telemetry, {"service_stats": {}}, now=10.0)

    stable = next(service for service in decision.services if service.name == "stable")
    assert stable.scheduler_allowed_path_ids == [1]
    assert stable.scheduler_path_weights == [(1, 140)]


def test_service_path_allocator_prefers_low_latency_path_for_protected_service() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    priority="high",
                ),
                ServiceConfig(
                    name="fast",
                    listen="127.0.0.1:10001",
                    target="127.0.0.1:20001",
                    priority="bulk",
                    scheduler_allowed_paths=["path-b"],
                ),
            ]
        }
    )
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    telemetry = _telemetry(
        **{
            "path-a": PathSchedulerMetrics(
                path_name="path-a",
                path_id=0,
                tx_capacity_bps=180_000_000,
                tx_latency_mean_us=80_000,
                tx_jitter_us=20_000,
            ),
            "path-b": PathSchedulerMetrics(
                path_name="path-b",
                path_id=1,
                tx_capacity_bps=140_000_000,
                tx_latency_mean_us=12_000,
                tx_jitter_us=2_000,
            ),
            "path-c": PathSchedulerMetrics(
                path_name="path-c",
                path_id=2,
                tx_capacity_bps=90_000_000,
                tx_latency_mean_us=35_000,
                tx_jitter_us=5_000,
            ),
        }
    )

    decision = allocator.update(config, runtime, telemetry, {"service_stats": {}}, now=10.0)

    stable = next(service for service in decision.services if service.name == "stable")
    fast = next(service for service in decision.services if service.name == "fast")
    assert stable.scheduler_path_policy == "single_best_path"
    assert stable.scheduler_allowed_path_ids == [1]
    assert fast.scheduler_allowed_path_ids == [0]


def test_service_path_allocator_moves_protected_service_to_one_sufficient_path_before_ordered_promotion() -> None:
    config = _config().model_copy(
        update={
            "paths": [
                PathConfig(
                    name="path-a",
                    interface="lo",
                    scheduler=PathSchedulerConfig(tx_capacity_bps=180_000_000, rx_capacity_bps=180_000_000),
                ),
                PathConfig(
                    name="path-b",
                    interface="lo",
                    scheduler=PathSchedulerConfig(tx_capacity_bps=500_000_000, rx_capacity_bps=500_000_000),
                ),
                PathConfig(
                    name="path-c",
                    interface="lo",
                    scheduler=PathSchedulerConfig(tx_capacity_bps=90_000_000, rx_capacity_bps=90_000_000),
                ),
            ],
            "services": [
                ServiceConfig(
                    name="stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    priority="high",
                    scheduler_path_policy="single_best_path",
                    scheduler_allowed_paths=["path-a"],
                    scheduler_path_weights={"path-a": 180},
                )
            ],
        }
    )
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    telemetry = SchedulerTelemetrySnapshot(
        paths={
            "path-a": PathSchedulerMetrics(
                path_name="path-a",
                path_id=0,
                tx_capacity_bps=180_000_000,
                tx_latency_mean_us=10_000,
            ),
            "path-b": PathSchedulerMetrics(
                path_name="path-b",
                path_id=1,
                tx_capacity_bps=500_000_000,
                tx_latency_mean_us=14_000,
            ),
            "path-c": PathSchedulerMetrics(
                path_name="path-c",
                path_id=2,
                tx_capacity_bps=90_000_000,
                tx_latency_mean_us=12_000,
            ),
        }
    )
    allocator.update(config, runtime, telemetry, {"service_stats": {"stable": {"tx_bytes": 1_000_000}}}, now=10.0)

    decision = allocator.update(
        config,
        runtime,
        telemetry,
        {"service_stats": {"stable": {"tx_bytes": 161_000_000}}},
        now=16.0,
    )

    stable = next(service for service in decision.services if service.name == "stable")
    assert stable.scheduler_path_policy == "single_best_path"
    assert stable.scheduler_allowed_path_ids == [1]
    assert stable.scheduler_path_weights == [(1, 500)]


def test_service_path_allocator_does_not_steal_paths_for_degraded_outcome_alone() -> None:
    config = _config()
    runtime = expand_config(config)
    allocator = ServicePathAllocator()

    decision = allocator.update(
        config,
        runtime,
        _telemetry(),
        {"service_outcomes": [{"service": "stable", "degraded": True, "reason": "tcp retransmits"}]},
        now=10.0,
    )

    stable = next(service for service in decision.services if service.name == "stable")
    assert stable.scheduler_allowed_path_ids == [0]
    assert stable.scheduler_path_weights == [(0, 180)]
    assert "outcome degraded" in decision.plans[0].reason
    assert allocator.last_decision is decision
    assert allocator.last_decision.export_dict()["changed"] is False


def test_service_path_allocator_moves_bulk_service_off_queued_path() -> None:
    config = _config()
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    telemetry = _telemetry(
        **{
            "path-b": PathSchedulerMetrics(
                path_name="path-b",
                path_id=1,
                tx_capacity_bps=140_000_000,
                queue_depth_packets=600,
            )
        }
    )

    decision = allocator.update(config, runtime, telemetry, {"service_stats": {}}, now=10.0)

    fast = next(service for service in decision.services if service.name == "fast")
    assert fast.scheduler_allowed_path_ids == [2]
    assert fast.scheduler_path_weights == [(2, 90)]


def test_service_path_allocator_expands_bulk_service_when_observed_demand_exceeds_one_path() -> None:
    config = _config()
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    allocator.update(
        config,
        runtime,
        _telemetry(),
        {"service_stats": {"stable": {"tx_bytes": 1_000_000}, "fast": {"tx_bytes": 1_000_000}}},
        now=10.0,
    )

    decision = allocator.update(
        config,
        runtime,
        _telemetry(),
        {
            "service_stats": {
                "stable": {"tx_bytes": 2_000_000},
                "fast": {"tx_bytes": 151_000_000},
            }
        },
        now=16.0,
    )

    fast = next(service for service in decision.services if service.name == "fast")
    assert fast.scheduler_path_policy == "weighted_round_robin"
    assert fast.scheduler_allowed_path_ids == [1, 2]
    assert fast.scheduler_path_weights == [(1, 140), (2, 90)]


def test_service_path_allocator_reserves_capacity_between_bulk_services() -> None:
    config = GatherlinkConfig(
        schema_version=1,
        node="source",
        role="client",
        peer="sink",
        scheduler=SchedulerConfig(mode="coordinated_adaptive"),
        paths=[
            PathConfig(
                name="path-a",
                interface="lo",
                scheduler=PathSchedulerConfig(tx_capacity_bps=200_000_000, rx_capacity_bps=200_000_000),
            ),
            PathConfig(
                name="path-b",
                interface="lo",
                scheduler=PathSchedulerConfig(tx_capacity_bps=100_000_000, rx_capacity_bps=100_000_000),
            ),
            PathConfig(
                name="path-c",
                interface="lo",
                scheduler=PathSchedulerConfig(tx_capacity_bps=100_000_000, rx_capacity_bps=100_000_000),
            ),
        ],
        services=[
            ServiceConfig(
                name="stable",
                listen="127.0.0.1:10000",
                target="127.0.0.1:20000",
                priority="high",
                scheduler_allowed_paths=["path-a"],
            ),
            ServiceConfig(
                name="fast-a",
                listen="127.0.0.1:10001",
                target="127.0.0.1:20001",
                priority="bulk",
                scheduler_allowed_paths=["path-b"],
            ),
            ServiceConfig(
                name="fast-b",
                listen="127.0.0.1:10002",
                target="127.0.0.1:20002",
                priority="bulk",
                scheduler_allowed_paths=["path-b"],
            ),
        ],
    )
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    telemetry = SchedulerTelemetrySnapshot(
        paths={
            "path-a": PathSchedulerMetrics(path_name="path-a", path_id=0, tx_capacity_bps=200_000_000),
            "path-b": PathSchedulerMetrics(path_name="path-b", path_id=1, tx_capacity_bps=100_000_000),
            "path-c": PathSchedulerMetrics(path_name="path-c", path_id=2, tx_capacity_bps=100_000_000),
        }
    )
    allocator.update(
        config,
        runtime,
        telemetry,
        {"service_stats": {"fast-a": {"tx_bytes": 1_000_000}, "fast-b": {"tx_bytes": 1_000_000}}},
        now=10.0,
    )

    decision = allocator.update(
        config,
        runtime,
        telemetry,
        {
            "service_stats": {
                "fast-a": {"tx_bytes": 60_000_000},
                "fast-b": {"tx_bytes": 60_000_000},
            }
        },
        now=16.0,
    )

    fast_a = next(service for service in decision.services if service.name == "fast-a")
    fast_b = next(service for service in decision.services if service.name == "fast-b")
    assert fast_a.scheduler_allowed_path_ids == [1]
    assert fast_b.scheduler_allowed_path_ids == [2]


def test_service_path_allocator_treats_tcp_ordered_class_as_protected_without_priority() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="wg-stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    traffic_class="tcp_ordered",
                    scheduler_allowed_paths=["path-a"],
                ),
                ServiceConfig(
                    name="wg-fast",
                    listen="127.0.0.1:10001",
                    target="127.0.0.1:20001",
                    priority="bulk",
                    scheduler_allowed_paths=["path-b"],
                ),
            ]
        }
    )
    runtime = expand_config(config)
    allocator = ServicePathAllocator()

    decision = allocator.update(config, runtime, _telemetry(), {"service_stats": {}}, now=10.0)

    stable = next(service for service in decision.services if service.name == "wg-stable")
    assert stable.scheduler_path_policy == "single_best_path"
    assert stable.scheduler_allowed_path_ids == [0]


def test_service_path_allocator_promotes_proven_ordered_protected_service() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="wg-stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    traffic_class="tcp_ordered",
                    scheduler_allowed_paths=["path-a"],
                )
            ]
        }
    )
    runtime = expand_config(config)
    runtime = runtime.model_copy(update={"scheduler": RuntimeSchedulerConfig(mode="ordered_multipath")})
    allocator = ServicePathAllocator()
    telemetry = SchedulerTelemetrySnapshot(
        paths={
            "path-a": PathSchedulerMetrics(
                path_name="path-a",
                path_id=0,
                tx_capacity_bps=180_000_000,
                tx_latency_mean_us=10_000,
                tx_jitter_us=1_000,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
            "path-b": PathSchedulerMetrics(
                path_name="path-b",
                path_id=1,
                tx_capacity_bps=140_000_000,
                tx_latency_mean_us=11_000,
                tx_jitter_us=1_500,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
            "path-c": PathSchedulerMetrics(
                path_name="path-c",
                path_id=2,
                tx_capacity_bps=90_000_000,
                tx_latency_mean_us=12_000,
                tx_jitter_us=1_500,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
        }
    )
    allocator.update(config, runtime, telemetry, {"service_stats": {"wg-stable": {"tx_bytes": 1_000_000}}}, now=10.0)

    decision = allocator.update(
        config,
        runtime,
        telemetry,
        {"service_stats": {"wg-stable": {"tx_bytes": 220_000_000}}},
        now=16.0,
    )

    stable = next(service for service in decision.services if service.name == "wg-stable")
    assert stable.scheduler_path_policy == "inherit"
    assert stable.scheduler_allowed_path_ids == [0, 1]
    assert stable.scheduler_path_weights == [(0, 180), (1, 140)]
    assert "ordered path subset has clean proof" in decision.plans[0].reason


def test_service_path_allocator_promotes_only_clean_ordered_subset() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="wg-stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    traffic_class="tcp_ordered",
                    scheduler_allowed_paths=["path-a"],
                )
            ]
        }
    )
    runtime = expand_config(config)
    runtime = runtime.model_copy(update={"scheduler": RuntimeSchedulerConfig(mode="ordered_multipath")})
    allocator = ServicePathAllocator()
    telemetry = SchedulerTelemetrySnapshot(
        paths={
            "path-a": PathSchedulerMetrics(
                path_name="path-a",
                path_id=0,
                tx_capacity_bps=180_000_000,
                tx_latency_mean_us=10_000,
                tx_jitter_us=1_000,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
            "path-b": PathSchedulerMetrics(
                path_name="path-b",
                path_id=1,
                tx_capacity_bps=140_000_000,
                tx_latency_mean_us=11_000,
                tx_jitter_us=1_500,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
            "path-c": PathSchedulerMetrics(
                path_name="path-c",
                path_id=2,
                tx_capacity_bps=90_000_000,
                tx_latency_mean_us=12_000,
                tx_jitter_us=80_000,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
        }
    )
    allocator.update(config, runtime, telemetry, {"service_stats": {"wg-stable": {"tx_bytes": 1_000_000}}}, now=10.0)

    decision = allocator.update(
        config,
        runtime,
        telemetry,
        {"service_stats": {"wg-stable": {"tx_bytes": 220_000_000}}},
        now=16.0,
    )

    stable = next(service for service in decision.services if service.name == "wg-stable")
    assert stable.scheduler_path_policy == "inherit"
    assert stable.scheduler_allowed_path_ids == [0, 1]
    assert stable.scheduler_path_weights == [(0, 180), (1, 140)]


def test_service_path_allocator_keeps_ordered_protected_single_path_when_clean_subset_cannot_cover_demand() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="wg-stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    traffic_class="tcp_ordered",
                    scheduler_allowed_paths=["path-a"],
                )
            ]
        }
    )
    runtime = expand_config(config)
    runtime = runtime.model_copy(update={"scheduler": RuntimeSchedulerConfig(mode="ordered_multipath")})
    allocator = ServicePathAllocator()
    telemetry = SchedulerTelemetrySnapshot(
        paths={
            "path-a": PathSchedulerMetrics(
                path_name="path-a",
                path_id=0,
                tx_capacity_bps=180_000_000,
                tx_latency_mean_us=10_000,
                tx_jitter_us=1_000,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
            "path-b": PathSchedulerMetrics(
                path_name="path-b",
                path_id=1,
                tx_capacity_bps=140_000_000,
                tx_latency_mean_us=11_000,
                tx_jitter_us=1_500,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
            "path-c": PathSchedulerMetrics(
                path_name="path-c",
                path_id=2,
                tx_capacity_bps=90_000_000,
                tx_latency_mean_us=12_000,
                tx_jitter_us=80_000,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
        }
    )
    allocator.update(config, runtime, telemetry, {"service_stats": {"wg-stable": {"tx_bytes": 1_000_000}}}, now=10.0)

    decision = allocator.update(
        config,
        runtime,
        telemetry,
        {"service_stats": {"wg-stable": {"tx_bytes": 260_000_000}}},
        now=16.0,
    )

    stable = next(service for service in decision.services if service.name == "wg-stable")
    assert stable.scheduler_path_policy == "single_best_path"
    assert stable.scheduler_allowed_path_ids == [0]


def test_service_path_allocator_keeps_ordered_protected_single_path_without_clean_proof() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="wg-stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    traffic_class="tcp_ordered",
                    scheduler_allowed_paths=["path-a"],
                )
            ]
        }
    )
    runtime = expand_config(config)
    runtime = runtime.model_copy(update={"scheduler": RuntimeSchedulerConfig(mode="ordered_multipath")})
    allocator = ServicePathAllocator()
    telemetry = SchedulerTelemetrySnapshot(
        paths={
            "path-a": PathSchedulerMetrics(
                path_name="path-a",
                path_id=0,
                tx_capacity_bps=180_000_000,
                tx_latency_mean_us=10_000,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
            ),
            "path-b": PathSchedulerMetrics(
                path_name="path-b",
                path_id=1,
                tx_capacity_bps=140_000_000,
                tx_latency_mean_us=11_000,
                latency_source="data-traffic-one-way",
                latency_confidence="good",
                observed_packets=10_000,
                reorder_depth_packets=512,
            ),
        }
    )
    allocator.update(config, runtime, telemetry, {"service_stats": {"wg-stable": {"tx_bytes": 1_000_000}}}, now=10.0)

    decision = allocator.update(
        config,
        runtime,
        telemetry,
        {"service_stats": {"wg-stable": {"tx_bytes": 40_000_000}}},
        now=16.0,
    )

    stable = next(service for service in decision.services if service.name == "wg-stable")
    assert stable.scheduler_path_policy == "single_best_path"
    assert stable.scheduler_allowed_path_ids == [0]


def test_service_path_allocator_treats_udp_bulk_class_as_bulk_without_priority() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="wg-stable",
                    listen="127.0.0.1:10000",
                    target="127.0.0.1:20000",
                    priority="high",
                    scheduler_allowed_paths=["path-a"],
                ),
                ServiceConfig(
                    name="wg-fast",
                    listen="127.0.0.1:10001",
                    target="127.0.0.1:20001",
                    traffic_class="udp_bulk",
                    scheduler_allowed_paths=["path-b"],
                ),
            ]
        }
    )
    runtime = expand_config(config)
    allocator = ServicePathAllocator()

    decision = allocator.update(config, runtime, _telemetry(), {"service_stats": {}}, now=10.0)

    fast = next(service for service in decision.services if service.name == "wg-fast")
    assert fast.scheduler_path_policy == "weighted_round_robin"
    assert fast.scheduler_allowed_path_ids == [1]


def test_service_path_allocator_shrinks_bulk_service_when_demand_fits_one_path() -> None:
    config = _config()
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    allocator.update(
        config,
        runtime,
        _telemetry(),
        {"service_stats": {"stable": {"tx_bytes": 1_000_000}, "fast": {"tx_bytes": 1_000_000}}},
        now=10.0,
    )
    expanded = allocator.update(
        config,
        runtime,
        _telemetry(),
        {
            "service_stats": {
                "stable": {"tx_bytes": 2_000_000},
                "fast": {"tx_bytes": 151_000_000},
            }
        },
        now=16.0,
    )

    shrunk = allocator.update(
        config,
        runtime.model_copy(update={"services": expanded.services}),
        _telemetry(),
        {
            "service_stats": {
                "stable": {"tx_bytes": 3_000_000},
                "fast": {"tx_bytes": 153_000_000},
            }
        },
        now=22.0,
    )

    fast = next(service for service in shrunk.services if service.name == "fast")
    assert fast.scheduler_allowed_path_ids == [1]


def test_service_path_allocator_does_not_run_outside_coordinated_adaptive() -> None:
    config = _config().model_copy(update={"scheduler": SchedulerConfig(mode="capacity_aware")})
    runtime = expand_config(config)
    allocator = ServicePathAllocator()

    decision = allocator.update(config, runtime, _telemetry(), {"service_stats": {}}, now=10.0)

    assert decision.services == runtime.services
    assert decision.changed is False
    assert "disabled" in decision.reason
