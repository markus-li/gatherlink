from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, PathSchedulerConfig, SchedulerConfig, ServiceConfig
from gatherlink.runtime.reload import (
    hot_reapply_scheduler_from_status,
    recompile_runtime_from_status,
    scheduler_decision_event_from_status,
)
from gatherlink.scheduling.congestion import CongestionFairnessController
from gatherlink.scheduling.coordinator import SchedulerPolicyCoordinator
from gatherlink.scheduling.metrics import SchedulerTelemetrySnapshot
from gatherlink.scheduling.policies import (
    FLOWLET_ADAPTIVE_DEFAULT_IDLE_US,
    FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US,
    FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS,
)
from gatherlink.scheduling.service_paths import ServicePathAllocator
from gatherlink.scheduling.smoothing import SchedulerTelemetrySmoother


def _config() -> GatherlinkConfig:
    return GatherlinkConfig(
        schema_version=1,
        node="reload-client",
        role="client",
        peer="reload-server",
        paths=[
            PathConfig(
                name="path-a",
                interface="lo",
                scheduler=PathSchedulerConfig(mtu=1200, tx_capacity_bps=1_000_000),
            ),
            PathConfig(
                name="path-b",
                interface="lo",
                scheduler=PathSchedulerConfig(mtu=1200, tx_capacity_bps=1_000_000),
            ),
        ],
        services=[ServiceConfig(name="udp-main", listen="127.0.0.1:0", target="127.0.0.1:51820")],
    )


def test_recompile_runtime_from_status_updates_scheduler_primitives_without_renumbering_paths() -> None:
    config = _config()
    runtime = expand_config(config)
    status = {
        "control_metadata": {
            "path_capacity": {
                "path-a": {"tx_bps": 3_000_000},
                "path-b": {"tx_bps": 1_000_000},
            }
        },
        "path_stats": {
            "path-a": {
                "packets": 100,
                "missed_packets": 0,
                "qdisc_dropped_packets": 0,
                "queue_depth_packets": 3,
                "queue_depth_bytes": 4096,
                "queue_oldest_age_us": 2500,
                "send_failed_packets": 2,
                "packets_needing_reorder": 4,
                "reorder_depth_packets": 3,
                "security_drop_packets": 1,
            },
            "path-b": {"packets": 50, "missed_packets": 10, "qdisc_dropped_packets": 5},
        },
    }

    updated = recompile_runtime_from_status(config, runtime, status)

    assert updated.paths[0].scheduler.path_id == runtime.paths[0].scheduler.path_id
    assert updated.paths[1].scheduler.path_id == runtime.paths[1].scheduler.path_id
    assert updated.paths[0].scheduler.tx_capacity_bps == 3_000_000
    assert updated.paths[1].scheduler.tx_capacity_bps == 1_000_000
    assert updated.paths[0].scheduler.loss_ppm == 0
    assert updated.paths[1].scheduler.loss_ppm == 230_769
    assert updated.paths[0].scheduler.queue_depth_packets == 3
    assert updated.paths[0].scheduler.queue_depth_bytes == 4096
    assert updated.paths[0].scheduler.queue_oldest_age_us == 2500


def test_hot_reapply_scheduler_from_status_calls_reapply_with_updated_runtime() -> None:
    config = _config()
    runtime = expand_config(config)
    calls = []

    def fake_reapply(dataplane, runtime_config):
        calls.append((dataplane, runtime_config))

    updated = hot_reapply_scheduler_from_status(
        "dataplane",
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 3_000_000},
                    "path-b": {"tx_bps": 1_000_000},
                }
            }
        },
        reapply=fake_reapply,
    )

    assert updated is calls[0][1]
    assert calls[0][0] == "dataplane"


def test_hot_reapply_scheduler_from_status_skips_noop_scheduler_apply() -> None:
    config = _config()
    runtime = expand_config(config)
    calls = []

    class Dataplane:
        def set_service_scheduler(self, *args):
            calls.append(("service", args))

    def fake_reapply(dataplane, runtime_config):
        calls.append(("core", dataplane, runtime_config))

    updated = hot_reapply_scheduler_from_status(
        Dataplane(),
        config,
        runtime,
        {},
        reapply=fake_reapply,
    )

    assert updated.scheduler == runtime.scheduler
    assert calls == []


def test_hot_reapply_scheduler_from_status_updates_service_scheduler_primitives() -> None:
    config = _config().model_copy(update={"scheduler": SchedulerConfig(mode="coordinated_adaptive")})
    runtime = expand_config(config)
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=1,
        minimum_dwell_seconds=0.0,
        required_confidence_windows=1,
        now=lambda: 30.0,
    )
    service_calls = []

    class Dataplane:
        def set_service_scheduler(
            self,
            service_id,
            fanout,
            fanout_below_bytes,
            flowlet_idle_us,
            flowlet_max_hold_us,
            path_run_datagrams,
            path_policy="inherit",
            allowed_path_ids=None,
            path_weights=None,
        ):
            service_calls.append(
                (
                    service_id,
                    fanout,
                    fanout_below_bytes,
                    flowlet_idle_us,
                    flowlet_max_hold_us,
                    path_run_datagrams,
                    path_policy,
                    allowed_path_ids or [],
                    path_weights or [],
                )
            )

    updated = hot_reapply_scheduler_from_status(
        Dataplane(),
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 500_000_000},
                    "path-b": {"tx_bps": 500_000_000},
                },
                "path_latency": {
                    "path-a": {"tx_mean_us": 5_000, "tx_jitter_us": 5_000},
                    "path-b": {"tx_mean_us": 90_000, "tx_jitter_us": 50_000},
                },
            },
            "path_stats": {
                "path-a": {"packets": 10_000, "missed_packets": 0, "qdisc_dropped_packets": 0},
                "path-b": {"packets": 10_000, "missed_packets": 0, "qdisc_dropped_packets": 0},
            },
        },
        reapply=lambda _dataplane, _runtime_config: None,
        scheduler_coordinator=coordinator,
    )

    assert coordinator.last_decision is not None
    assert coordinator.last_decision.effective_mode == "flowlet_adaptive"
    assert updated.services[0].scheduler_flowlet_idle_us == FLOWLET_ADAPTIVE_DEFAULT_IDLE_US
    assert updated.services[0].scheduler_flowlet_max_hold_us == FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US
    assert updated.services[0].scheduler_path_run_datagrams == FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS
    assert service_calls == [
        (
            updated.services[0].service_id,
            1,
            0,
            FLOWLET_ADAPTIVE_DEFAULT_IDLE_US,
            FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US,
            FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS,
            "inherit",
            [],
            [],
        )
    ]


def test_hot_reapply_coordinated_tcp_fallback_applies_allowed_service_path_id() -> None:
    config = _config().model_copy(
        update={
            "scheduler": SchedulerConfig(mode="coordinated_adaptive", traffic_bias="tcp"),
            "paths": [
                PathConfig(
                    name="path-a",
                    interface="lo",
                    scheduler=PathSchedulerConfig(mtu=1200, tx_capacity_bps=500_000_000),
                ),
                PathConfig(
                    name="path-b",
                    interface="lo",
                    scheduler=PathSchedulerConfig(mtu=1200, tx_capacity_bps=900_000_000),
                ),
            ],
            "services": [
                ServiceConfig(
                    name="wireguard",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51820",
                    traffic_class="tcp_ordered",
                )
            ],
        }
    )
    runtime = expand_config(config)
    stale_service = runtime.services[0].model_copy(
        update={
            "scheduler_path_policy": "inherit",
            "scheduler_allowed_path_ids": [],
            "scheduler_path_weights": [],
        }
    )
    runtime = runtime.model_copy(update={"services": [stale_service]})
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=1,
        minimum_dwell_seconds=0.0,
        required_confidence_windows=1,
        now=lambda: 30.0,
    )
    service_calls = []

    class Dataplane:
        def set_service_scheduler(
            self,
            service_id,
            fanout,
            fanout_below_bytes,
            flowlet_idle_us,
            flowlet_max_hold_us,
            path_run_datagrams,
            path_policy="inherit",
            allowed_path_ids=None,
            path_weights=None,
        ):
            service_calls.append(
                (
                    service_id,
                    fanout,
                    fanout_below_bytes,
                    flowlet_idle_us,
                    flowlet_max_hold_us,
                    path_run_datagrams,
                    path_policy,
                    allowed_path_ids or [],
                    path_weights or [],
                )
            )

    updated = hot_reapply_scheduler_from_status(
        Dataplane(),
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 500_000_000},
                    "path-b": {"tx_bps": 900_000_000},
                }
            },
            "path_stats": {
                "path-a": {"packets": 10_000, "missed_packets": 0, "qdisc_dropped_packets": 0},
                "path-b": {"packets": 10_000, "missed_packets": 0, "qdisc_dropped_packets": 0},
            },
        },
        reapply=lambda _dataplane, _runtime_config: None,
        scheduler_coordinator=coordinator,
    )

    assert coordinator.last_decision is not None
    assert coordinator.last_decision.effective_mode == "single_best_path"
    assert updated.services[0].scheduler_path_policy == "single_best_path"
    assert updated.services[0].scheduler_allowed_path_ids == [1]
    assert service_calls == [
        (
            updated.services[0].service_id,
            1,
            0,
            0,
            0,
            0,
            "single_best_path",
            [1],
            [],
        )
    ]


def test_recompile_runtime_from_status_preserves_explicit_service_scheduler_primitives() -> None:
    config = _config().model_copy(
        update={
            "scheduler": SchedulerConfig(mode="coordinated_adaptive"),
            "services": [
                ServiceConfig(
                    name="udp-main",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51820",
                    scheduler_flowlet_idle_us=10_000,
                    scheduler_flowlet_max_hold_us=20_000,
                    scheduler_path_run_datagrams=64,
                )
            ],
        }
    )
    runtime = expand_config(config)
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=1,
        minimum_dwell_seconds=0.0,
        required_confidence_windows=1,
        now=lambda: 31.0,
    )

    updated = recompile_runtime_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 500_000_000},
                    "path-b": {"tx_bps": 500_000_000},
                },
                "path_latency": {
                    "path-a": {"tx_mean_us": 5_000, "tx_jitter_us": 5_000},
                    "path-b": {"tx_mean_us": 90_000, "tx_jitter_us": 50_000},
                },
            },
            "path_stats": {"path-a": {"packets": 10_000}, "path-b": {"packets": 10_000}},
        },
        scheduler_coordinator=coordinator,
    )

    assert coordinator.last_decision is not None
    assert coordinator.last_decision.effective_mode == "flowlet_adaptive"
    assert updated.services[0].scheduler_flowlet_idle_us == 10_000
    assert updated.services[0].scheduler_flowlet_max_hold_us == 20_000
    assert updated.services[0].scheduler_path_run_datagrams == 64


def test_recompile_runtime_from_status_can_update_service_path_allocation() -> None:
    config = _config().model_copy(
        update={
            "scheduler": SchedulerConfig(mode="coordinated_adaptive"),
            "services": [
                ServiceConfig(
                    name="stable",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51820",
                    priority="high",
                    scheduler_path_policy="single_best_path",
                    scheduler_allowed_paths=["path-a"],
                    scheduler_path_weights={"path-a": 1},
                ),
                ServiceConfig(
                    name="fast",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51821",
                    priority="bulk",
                    scheduler_path_policy="weighted_round_robin",
                    scheduler_allowed_paths=["path-b"],
                    scheduler_path_weights={"path-b": 1},
                ),
            ],
        }
    )
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    allocator.update(
        config,
        runtime,
        SchedulerTelemetrySnapshot(),
        {"service_stats": {"stable": {"tx_bytes": 1_000_000}, "fast": {"tx_bytes": 1_000_000}}},
        now=10.0,
    )

    updated = recompile_runtime_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 180_000_000},
                    "path-b": {"tx_bps": 140_000_000},
                }
            },
            "service_stats": {
                "stable": {"tx_bytes": 2_000_000},
                "fast": {"tx_bytes": 31_000_000},
            },
        },
        service_path_allocator=allocator,
    )

    stable = next(service for service in updated.services if service.name == "stable")
    fast = next(service for service in updated.services if service.name == "fast")
    assert stable.scheduler_allowed_path_ids == [0]
    assert fast.scheduler_allowed_path_ids == [1]
    assert fast.scheduler_path_weights == [(1, 140)]


def test_coordinated_reapply_uses_service_traffic_summary_for_auto_bias() -> None:
    config = _config().model_copy(
        update={
            "scheduler": SchedulerConfig(mode="coordinated_adaptive"),
            "services": [
                ServiceConfig(
                    name="stable",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51820",
                    traffic_class="tcp_ordered",
                ),
                ServiceConfig(
                    name="fast",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51821",
                    traffic_class="udp_bulk",
                ),
            ],
        }
    )
    runtime = expand_config(config)
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=1,
        minimum_dwell_seconds=0.0,
        required_confidence_windows=1,
        now=lambda: 30.0,
    )

    updated = recompile_runtime_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 1_000_000},
                    "path-b": {"tx_bps": 1_000_000},
                }
            },
            "service_stats": {
                "stable": {"tx_bytes": 100_000},
                "fast": {"tx_bytes": 900_000},
            },
        },
        scheduler_coordinator=coordinator,
    )

    assert coordinator.last_decision is not None
    assert coordinator.last_decision.effective_mode == "capacity_aware"
    assert "service_mixed_classes" in coordinator.last_decision.signals
    assert updated.scheduler.mode == "weighted_round_robin"


def test_recompile_runtime_from_status_ignores_invalid_loss_counters() -> None:
    config = _config()
    runtime = expand_config(config)

    updated = recompile_runtime_from_status(
        config,
        runtime,
        {
            "path_stats": {
                "path-a": {
                    "packets": "not-a-number",
                    "missed_packets": -5,
                    "qdisc_dropped_packets": object(),
                }
            }
        },
    )

    assert updated.paths[0].scheduler.loss_ppm == 0


def test_scheduler_telemetry_from_status_merges_runtime_pressure_facts() -> None:
    from gatherlink.runtime.reload import scheduler_telemetry_from_status

    config = _config()
    runtime = expand_config(config)
    telemetry = scheduler_telemetry_from_status(
        {
            "path_stats": {
                "path-a": {
                    "packets": 100,
                    "missed_packets": 2,
                    "qdisc_dropped_packets": 3,
                    "security_drop_packets": 1,
                    "send_failed_packets": 4,
                    "packets_needing_reorder": 5,
                    "reorder_depth_packets": 6,
                    "last_tx_at_us": 12_000,
                    "last_rx_at_us": 13_000,
                    "last_tx_gap_us": 1_500,
                    "last_rx_gap_us": 1_700,
                    "scheduler_in_flight_packets": 8,
                    "scheduler_in_flight_bytes": 16_000,
                    "scheduler_predicted_delivery_us": 9_000,
                    "reorder_buffer_packets": 3,
                    "reorder_buffer_oldest_age_us": 2_000,
                    "socket_receive_buffer_bytes": 65_536,
                    "socket_send_buffer_bytes": 32_768,
                    "socket_drain_quantum": 16,
                }
            }
        },
        runtime_config=runtime,
    )

    metrics = telemetry.paths["path-a"]

    assert metrics.loss_ppm == 47_619
    assert metrics.send_failures == 4
    assert metrics.receive_gaps == 5
    assert metrics.reorder_depth_packets == 6
    assert metrics.local_drops == 4
    assert metrics.last_tx_at_us == 12_000
    assert metrics.last_rx_at_us == 13_000
    assert metrics.last_tx_gap_us == 1_500
    assert metrics.last_rx_gap_us == 1_700
    assert metrics.scheduler_in_flight_packets == 8
    assert metrics.scheduler_predicted_delivery_us == 9_000
    assert metrics.reorder_buffer_packets == 3
    assert metrics.socket_drain_quantum == 16


def test_scheduler_telemetry_preserves_peer_receiver_pressure_when_local_stats_are_quiet() -> None:
    from gatherlink.runtime.reload import scheduler_telemetry_from_status

    config = _config()
    runtime = expand_config(config)
    telemetry = scheduler_telemetry_from_status(
        {
            "control_metadata": {
                "path_pressure": {
                    "path-a": {
                        "loss_ppm": 120_000,
                        "queue_depth_packets": 900,
                        "queue_depth_bytes": 128_000,
                        "queue_oldest_age_us": 80_000,
                        "send_failures": 2,
                        "receive_gaps": 4096,
                        "reorder_depth_packets": 2048,
                        "local_drops": 7,
                    }
                }
            },
            "path_stats": {
                "path-a": {
                    "packets": 10_000,
                    "missed_packets": 0,
                    "qdisc_dropped_packets": 0,
                    "queue_depth_packets": 0,
                    "queue_depth_bytes": 0,
                    "queue_oldest_age_us": 0,
                    "send_failed_packets": 1,
                    "packets_needing_reorder": 0,
                    "reorder_depth_packets": 0,
                    "security_drop_packets": 0,
                }
            },
        },
        runtime_config=runtime,
    )

    metrics = telemetry.paths["path-a"]

    assert metrics.loss_ppm == 120_000
    assert metrics.queue_depth_packets == 900
    assert metrics.queue_depth_bytes == 128_000
    assert metrics.queue_oldest_age_us == 80_000
    assert metrics.send_failures == 3
    assert metrics.receive_gaps == 4096
    assert metrics.reorder_depth_packets == 2048
    assert metrics.local_drops == 7
    assert metrics.observed_packets == 10_000


def test_scheduler_decision_event_from_status_explains_path_health() -> None:
    config = _config()
    runtime = expand_config(config)

    event = scheduler_decision_event_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 3_000_000},
                    "path-b": {"tx_bps": 1_000_000},
                }
            },
            "path_stats": {
                "path-a": {"packets": 100, "missed_packets": 0},
                "path-b": {"packets": 10, "missed_packets": 10, "send_failed_packets": 2},
            },
        },
    )

    assert event.stable_code
    assert event.code == "scheduler.decision"
    assert event.node == "reload-client"
    assert event.details["mode"] == runtime.scheduler.mode
    assert event.details["configured_mode"] == config.scheduler.mode
    assert event.details["selected_path"] == "path-a"
    path_b = next(path for path in event.details["paths"] if path["path"] == "path-b")
    assert path_b["health"] == "degraded"
    assert "send_failures" in path_b["reasons"]


def test_scheduler_decision_event_includes_service_path_outcome_ledger() -> None:
    config = _config()
    runtime = expand_config(config)

    event = scheduler_decision_event_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_control": {
                    "path-a": {
                        "tx": {"frames": 12},
                        "rx": {"frames": 10},
                    }
                },
                "path_latency": {
                    "path-a": {
                        "tx_mean_us": 10_000,
                        "source": "data-traffic-one-way",
                        "confidence": "good",
                    }
                },
                "path_pressure": {
                    "path-a": {
                        "queue_depth_packets": 2,
                        "receive_gaps": 3,
                        "reorder_buffer_packets": 4,
                        "scheduler_in_flight_packets": 5,
                        "scheduler_in_flight_bytes": 6000,
                    }
                },
            },
            "service_stats": {
                "udp-main": {
                    "tx_packets": 11,
                    "tx_bytes": 1200,
                    "rx_packets": 9,
                    "rx_bytes": 900,
                }
            },
            "service_path_stats": {
                "udp-main": {
                    "path-a": {
                        "tx_packets": 7,
                        "tx_bytes": 700,
                        "rx_packets": 6,
                        "rx_bytes": 600,
                        "expected_duplicate_packets": 1,
                    }
                }
            },
        },
    )

    ledger = event.details["service_path_outcomes"][0]
    assert ledger["service"] == "udp-main"
    assert ledger["tx_packets"] == 11
    assert ledger["path_policy"] == runtime.services[0].scheduler_path_policy
    path_row = ledger["paths"][0]
    assert path_row["path"] == "path-a"
    assert path_row["tx_packets"] == 7
    assert path_row["expected_duplicate_packets"] == 1
    assert path_row["receiver_blocking_pressure_packets"] == 14
    assert path_row["ack_control_return_quality"] == "bidirectional"

    path_details = next(path for path in event.details["paths"] if path["path"] == "path-a")
    assert path_details["observed_in_flight_packets"] == 5
    assert path_details["receiver_blocking_pressure_packets"] == 14
    assert path_details["estimated_earliest_delivery_us"] is not None
    assert event.details["promotion_trace"]["reason"] == "coordinator_not_active"


def test_scheduler_decision_event_explains_congestion_fairness_backoff() -> None:
    config = _config().model_copy(update={"scheduler": SchedulerConfig(mode="capacity_aware")})
    runtime = expand_config(config)
    updated = recompile_runtime_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {"path-a": {"tx_bps": 100_000_000}},
                "path_pressure": {
                    "path-a": {
                        "loss_ppm": 100_000,
                        "queue_depth_packets": 1200,
                        "queue_oldest_age_us": 125_000,
                    }
                },
            }
        },
    )

    event = scheduler_decision_event_from_status(
        config,
        updated,
        {
            "control_metadata": {
                "path_capacity": {"path-a": {"tx_bps": 100_000_000}},
                "path_pressure": {
                    "path-a": {
                        "loss_ppm": 100_000,
                        "queue_depth_packets": 1200,
                        "queue_oldest_age_us": 125_000,
                    }
                },
            }
        },
    )

    fairness = event.details["congestion_fairness"][0]
    assert event.details["congestion_policy"] == "adaptive"
    assert fairness["path"] == "path-a"
    assert fairness["pressure_level"] == 2
    assert fairness["reason"] == "high_pressure"
    assert fairness["pacing_budget_bps"] == 65_000_000
    assert fairness["loss_ppm"] == 100_000


def test_recompile_runtime_from_status_holds_congestion_recovery_until_stable() -> None:
    config = _config().model_copy(update={"scheduler": SchedulerConfig(mode="capacity_aware")})
    runtime = expand_config(config)
    controller = CongestionFairnessController(recovery_windows_required=3)
    pressure_status = {
        "control_metadata": {
            "path_capacity": {"path-a": {"tx_bps": 100_000_000}},
            "path_pressure": {"path-a": {"queue_depth_packets": 1200}},
        }
    }
    clean_status = {"control_metadata": {"path_capacity": {"path-a": {"tx_bps": 100_000_000}}}}

    runtime = recompile_runtime_from_status(config, runtime, pressure_status, congestion_controller=controller)
    assert runtime.paths[0].scheduler.pacing_budget_bps == 65_000_000
    runtime = recompile_runtime_from_status(config, runtime, clean_status, congestion_controller=controller)
    assert runtime.paths[0].scheduler.pacing_budget_bps == 65_000_000

    event = scheduler_decision_event_from_status(config, runtime, clean_status)
    fairness = event.details["congestion_fairness"][0]
    assert fairness["reason"] == "held_for_recovery_hysteresis"

    runtime = recompile_runtime_from_status(config, runtime, clean_status, congestion_controller=controller)
    runtime = recompile_runtime_from_status(config, runtime, clean_status, congestion_controller=controller)
    assert runtime.paths[0].scheduler.pacing_budget_bps == 0


def test_scheduler_decision_event_includes_service_path_allocator_details() -> None:
    config = _config().model_copy(update={"scheduler": SchedulerConfig(mode="coordinated_adaptive")})
    runtime = expand_config(config)
    allocator = ServicePathAllocator()
    allocator.update(
        config,
        runtime,
        SchedulerTelemetrySnapshot(),
        {"service_stats": {"udp-main": {"tx_bytes": 1}}},
        now=10.0,
    )

    event = scheduler_decision_event_from_status(
        config,
        runtime,
        {},
        service_path_allocator=allocator,
    )

    assert event.details["service_path_allocator"]["reason"] == "service path plan held"


def test_scheduler_decision_event_includes_service_traffic_summary() -> None:
    config = _config().model_copy(
        update={
            "services": [
                ServiceConfig(
                    name="stable",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51820",
                    traffic_class="tcp_ordered",
                ),
                ServiceConfig(
                    name="fast",
                    listen="127.0.0.1:0",
                    target="127.0.0.1:51821",
                    traffic_class="udp_bulk",
                ),
            ]
        }
    )
    runtime = expand_config(config)

    event = scheduler_decision_event_from_status(
        config,
        runtime,
        {
            "service_stats": {
                "stable": {"tx_bytes": 100},
                "fast": {"tx_bytes": 200},
            },
            "service_outcomes": [{"service": "stable", "degraded": True, "reason": "tcp retransmits"}],
        },
    )

    summary = event.details["service_traffic"]
    assert summary["protected_services"] == ["stable"]
    assert summary["bulk_services"] == ["fast"]
    assert summary["protected_degraded"] == ["stable"]
    assert "service_mixed_classes" in summary["signals"]


def test_coordinated_adaptive_reapply_reports_effective_policy() -> None:
    config = _config().model_copy(update={"scheduler": SchedulerConfig(mode="coordinated_adaptive")})
    runtime = expand_config(config)
    now = [10.0]
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=1,
        minimum_dwell_seconds=0.0,
        required_confidence_windows=1,
        now=lambda: now[0],
    )

    updated = recompile_runtime_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 3_000_000},
                    "path-b": {"tx_bps": 1_000_000},
                }
            },
            "path_stats": {"path-a": {"packets": 100, "packets_needing_reorder": 2048}},
        },
        scheduler_coordinator=coordinator,
    )
    event = scheduler_decision_event_from_status(
        config,
        updated,
        {"path_stats": {"path-a": {"packets": 100, "packets_needing_reorder": 2048}}},
        scheduler_coordinator=coordinator,
    )

    assert updated.scheduler.mode == "ordered_multipath"
    assert event.details["configured_mode"] == "coordinated_adaptive"
    assert event.details["coordinator"]["effective_mode"] == "ordered_multipath_capacity_aware"
    assert event.details["coordinator"]["switched"] is True
    assert event.details["coordinator"]["path_profiles"]
    assert event.details["coordinator"]["path_profiles"][0]["path"] == "path-a"
    assert event.details["coordinator_recent"]


def test_coordinated_adaptive_reacts_to_peer_receiver_queue_pressure() -> None:
    config = _config().model_copy(update={"scheduler": SchedulerConfig(mode="coordinated_adaptive")})
    runtime = expand_config(config)
    coordinator = SchedulerPolicyCoordinator(
        minimum_dwell_packets=1,
        minimum_dwell_seconds=0.0,
        required_confidence_windows=1,
        now=lambda: 20.0,
    )

    updated = recompile_runtime_from_status(
        config,
        runtime,
        {
            "control_metadata": {
                "path_capacity": {
                    "path-a": {"tx_bps": 800_000_000},
                    "path-b": {"tx_bps": 150_000_000},
                },
                "path_latency": {
                    "path-a": {"tx_mean_us": 8_000},
                    "path-b": {"tx_mean_us": 120_000},
                },
                "path_pressure": {
                    "path-b": {
                        "queue_depth_packets": 900,
                        "queue_depth_bytes": 300_000,
                    }
                },
            },
            "path_stats": {
                "path-a": {"packets": 10_000, "missed_packets": 0, "qdisc_dropped_packets": 0},
                "path-b": {"packets": 10_000, "missed_packets": 0, "qdisc_dropped_packets": 0},
            },
        },
        scheduler_coordinator=coordinator,
    )

    assert coordinator.last_decision is not None
    assert coordinator.last_decision.effective_mode == "arrival_guarded_capacity"
    assert "queue_pressure" in coordinator.last_decision.signals
    assert updated.paths[0].scheduler.state == "active"
    assert updated.paths[1].scheduler.state == "drain"


def test_recompile_runtime_from_status_can_smooth_noisy_telemetry() -> None:
    config = _config()
    runtime = expand_config(config)
    smoother = SchedulerTelemetrySmoother()

    recompile_runtime_from_status(
        config,
        runtime,
        {"control_metadata": {"path_capacity": {"path-a": {"tx_bps": 1_000_000}}}},
        telemetry_smoother=smoother,
    )
    updated = recompile_runtime_from_status(
        config,
        runtime,
        {"control_metadata": {"path_capacity": {"path-a": {"tx_bps": 9_000_000}}}},
        telemetry_smoother=smoother,
    )

    assert updated.paths[0].scheduler.tx_capacity_bps == 3_000_000
