from __future__ import annotations

from gatherlink.scheduling.service_outcome import (
    DualWireGuardOutcomeThresholds,
    dual_wireguard_outcome_from_results,
    outcome_snapshot_to_report,
    service_outcome_snapshot_from_json,
)


def test_dual_wireguard_outcome_marks_stable_tcp_degraded_for_retransmits() -> None:
    snapshot = dual_wireguard_outcome_from_results(
        [
            {"name": "dual-wg-stable-mixed-tcp", "mbit_per_second": 120.0, "retransmits": 2500},
            {"name": "dual-wg-fast-mixed-udp", "mbit_per_second": 95.0, "lost_percent": 0.0},
        ],
        thresholds=DualWireGuardOutcomeThresholds(tcp_max_retransmits=1000, udp_max_loss_percent=0.1),
    )

    assert snapshot.degraded_services() == {"wireguard-stable"}
    assert "tcp retransmits 2500 above 1000" in snapshot.reason_for("wireguard-stable")


def test_dual_wireguard_outcome_marks_stable_tcp_degraded_for_low_throughput() -> None:
    snapshot = dual_wireguard_outcome_from_results(
        [{"name": "dual-wg-stable-mixed-tcp", "mbit_per_second": 80.0, "retransmits": 0}],
        thresholds=DualWireGuardOutcomeThresholds(tcp_min_mbit_per_second=100.0, tcp_max_retransmits=1000),
    )

    assert snapshot.degraded_services() == {"wireguard-stable"}
    assert "tcp throughput 80.00 below 100.00 Mbit/s" in snapshot.reason_for("wireguard-stable")


def test_dual_wireguard_outcome_marks_fast_udp_degraded_without_affecting_stable() -> None:
    snapshot = dual_wireguard_outcome_from_results(
        [
            {"name": "dual-wg-stable-mixed-tcp", "mbit_per_second": 120.0, "retransmits": 0},
            {"name": "dual-wg-fast-mixed-udp", "mbit_per_second": 95.0, "lost_percent": 2.5},
        ],
        thresholds=DualWireGuardOutcomeThresholds(tcp_max_retransmits=1000, udp_max_loss_percent=0.1),
    )

    assert snapshot.degraded_services() == {"wireguard-fast"}
    assert "udp loss 2.5 above 0.10%" in snapshot.reason_for("wireguard-fast")


def test_outcome_snapshot_to_report_is_json_friendly() -> None:
    snapshot = dual_wireguard_outcome_from_results(
        [{"name": "dual-wg-stable-mixed-tcp", "mbit_per_second": 80.0, "retransmits": 0}],
        thresholds=DualWireGuardOutcomeThresholds(tcp_min_mbit_per_second=100.0),
    )

    assert outcome_snapshot_to_report(snapshot) == [
        {
            "service": "wireguard-stable",
            "degraded": True,
            "reason": "tcp throughput 80.00 below 100.00 Mbit/s",
        }
    ]


def test_service_outcome_snapshot_from_json_accepts_report_shape() -> None:
    snapshot = service_outcome_snapshot_from_json(
        {
            "outcomes": [
                {"service": "wireguard-stable", "degraded": True, "reason": "tcp retransmits increased"},
                {"service": "wireguard-fast", "degraded": False},
                {"service": "", "degraded": True, "reason": "ignored"},
            ]
        }
    )

    assert snapshot is not None
    assert snapshot.degraded_services() == {"wireguard-stable"}
    assert snapshot.reason_for("wireguard-stable") == "tcp retransmits increased"


def test_service_outcome_snapshot_from_json_accepts_compact_mapping() -> None:
    snapshot = service_outcome_snapshot_from_json({"stable": "tcp degraded", "fast": False, "count": 1})

    assert snapshot is not None
    assert snapshot.degraded_services() == {"stable"}
    assert snapshot.reason_for("stable") == "tcp degraded"
