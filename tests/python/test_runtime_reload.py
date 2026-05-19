from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, PathSchedulerConfig, ServiceConfig
from gatherlink.runtime.reload import hot_reapply_scheduler_from_status, recompile_runtime_from_status


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
            "path-a": {"packets": 100, "missed_packets": 0, "qdisc_dropped_packets": 0},
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
        {"path_stats": {"path-a": {"packets": 10, "missed_packets": 0}}},
        reapply=fake_reapply,
    )

    assert updated is calls[0][1]
    assert calls[0][0] == "dataplane"
