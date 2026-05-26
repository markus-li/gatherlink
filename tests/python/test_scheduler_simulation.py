from __future__ import annotations

import json
from pathlib import Path

from gatherlink.lab.reports import generate_three_path_scheduler_report
from gatherlink.scheduling.simulation import POLICIES_TO_COMPARE, run_congestion_fairness_matrix, run_scheduler_matrix


def test_scheduler_matrix_covers_every_policy() -> None:
    matrix = run_scheduler_matrix()

    assert matrix
    for decisions in matrix.values():
        assert [decision.policy for decision in decisions] == list(POLICIES_TO_COMPARE)
        assert all(decision.selected_path for decision in decisions)


def test_scheduler_matrix_shows_expected_policy_differences() -> None:
    matrix = run_scheduler_matrix()
    clean = {decision.policy: decision.selected_path for decision in matrix["clean-balanced"]}
    lossy = {decision.policy: decision.selected_path for decision in matrix["loss-on-fast-path"]}
    queued = {decision.policy: decision.selected_path for decision in matrix["queue-pressure"]}
    rust_modes = {decision.policy: decision.rust_mode for decision in matrix["clean-balanced"]}

    assert clean["srtt"] == "path-a"
    assert clean["capacity_aware"] == "path-a"
    assert rust_modes["capacity_aware"] == "weighted_round_robin"
    assert lossy["loss_aware"] != "path-a"
    assert queued["least_queue"] != "path-a"


def test_congestion_fairness_matrix_shows_clean_bypass_and_pressure_backoff() -> None:
    matrix = run_congestion_fairness_matrix()
    clean = {decision.policy: decision for decision in matrix["clean-shared-link"]}
    moderate = {decision.policy: decision for decision in matrix["moderate-buffer-growth"]}
    lossy = {decision.policy: decision for decision in matrix["lossy-bufferbloat"]}

    assert clean["adaptive"].pacing_budget_bps == 0
    assert moderate["adaptive"].pacing_budget_bps == 425_000_000
    assert lossy["adaptive"].pacing_budget_bps == 325_000_000
    assert lossy["volatile"].pacing_budget_bps < lossy["adaptive"].pacing_budget_bps
    assert lossy["off"].pacing_budget_bps == 0


def test_scheduler_report_includes_lab_and_policy_sections(tmp_path: Path) -> None:
    sink_snapshot = {
        "bytes": 10,
        "control_metadata": {
            "path_capacity": {"path-a": {"tx_bps": 3_000_000, "rx_bps": 2_000_000}},
            "path_latency": {"path-a": {"tx_mean_us": 1500, "rx_mean_us": 2500}},
            "sink_time": {"ntp_state": "synchronized", "ntp_source": "time.cloudflare.com"},
        },
        "missed_packets": 1,
        "packets": 2,
        "packets_needing_reorder": 1,
        "path_stats": {
            "path-a": {
                "missed_packets": 1,
                "packets": 2,
                "qdisc_dropped_packets": 1,
                "reordered_packets": 0,
                "rx_packets": 2,
                "tx_packets": 3,
            }
        },
        "reordered_packets": 0,
        "reply_bytes": 20,
        "reply_packets": 3,
    }
    (tmp_path / "sample-sink.json").write_text(json.dumps(sink_snapshot), encoding="utf-8")

    report = generate_three_path_scheduler_report(tmp_path)

    assert "Scheduler policy matrix" in report
    assert "Congestion fairness matrix" in report
    assert "lossy-bufferbloat" in report
    assert "Saved lab runs" in report
    assert "sample" in report
    assert "synchronized (time.cloudflare.com)" in report
