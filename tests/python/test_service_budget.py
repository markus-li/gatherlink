from __future__ import annotations

from gatherlink.config.expansion import expand_config
from gatherlink.config.models import GatherlinkConfig, PathConfig, ServiceConfig
from gatherlink.scheduling.service_budget import (
    BULK_BYTE_BUDGET_ADJUST_SAMPLES,
    BULK_BYTE_BUDGET_EQUIVALENT_PACKETS,
    BULK_BYTE_BUDGET_MIN_EQUIVALENT_PACKETS,
    BULK_QUANTUM_CEILING_PACKETS,
    CLEAN_SAMPLES_TO_RELEASE,
    MIN_SAMPLE_SECONDS,
    PRESSURE_SAMPLES_TO_ACTIVATE,
    PROTECTED_OUTCOME_BYTE_BUDGET_EQUIVALENT_PACKETS,
    PROTECTED_OUTCOME_PACKET_BUDGET,
    ServiceBudgetController,
    ServiceOutcomeSnapshot,
)
from gatherlink.scheduling.service_priority import (
    service_budget_plan,
    service_poll_plan,
    uses_service_budget_plan,
    uses_service_drain_plan,
)


def _dual_service_runtime():
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="stable",
                listen="127.0.0.1:55180",
                target="127.0.0.1:51820",
                priority="high",
            ),
            ServiceConfig(
                name="fast",
                listen="127.0.0.1:55181",
                target="127.0.0.1:51821",
                priority="bulk",
            ),
        ],
    )
    return expand_config(config)


def _dual_class_runtime():
    config = GatherlinkConfig(
        schema_version=1,
        node="local",
        role="client",
        peer="remote",
        paths=[PathConfig(name="path-a", interface="gl-a")],
        services=[
            ServiceConfig(
                name="stable",
                listen="127.0.0.1:55180",
                target="127.0.0.1:51820",
                traffic_class="tcp_ordered",
            ),
            ServiceConfig(
                name="fast",
                listen="127.0.0.1:55181",
                target="127.0.0.1:51821",
                traffic_class="udp_bulk",
            ),
        ],
    )
    return expand_config(config)


def test_service_budget_controller_activates_only_after_sustained_bulk_pressure() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    now = 0.0

    decision = controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=now,
        batch_size=512,
    )
    assert not decision.active
    assert not decision.changed

    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE - 1):
        now += 1.0
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 600 * (index + 1), "tx_bytes": 800_000 * (index + 1)},
            },
            now=now,
            batch_size=512,
        )
        assert not decision.active

    now += 1.0
    decision = controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 400, "tx_bytes": 400_000},
            "fast": {"tx_packets": 2_400, "tx_bytes": 3_200_000},
        },
        now=now,
        batch_size=512,
    )

    assert decision.changed
    assert decision.packet_budget_overrides == {"fast": BULK_QUANTUM_CEILING_PACKETS}
    assert decision.byte_budget_overrides == {"fast": BULK_BYTE_BUDGET_EQUIVALENT_PACKETS * 1_000}


def test_service_budget_controller_uses_traffic_class_when_priority_is_normal() -> None:
    runtime = _dual_class_runtime()
    controller = ServiceBudgetController()
    now = 0.0
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=now,
        batch_size=512,
    )

    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE):
        now += 1.0
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 600 * (index + 1), "tx_bytes": 800_000 * (index + 1)},
            },
            now=now,
            batch_size=512,
        )

    assert decision.active
    assert decision.packet_budget_overrides == {"fast": BULK_QUANTUM_CEILING_PACKETS}


def test_service_budget_controller_releases_after_sustained_clean_samples() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    now = 0.0
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=now,
        batch_size=512,
    )
    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE):
        now += 1.0
        controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 600 * (index + 1), "tx_bytes": 800_000 * (index + 1)},
            },
            now=now,
            batch_size=512,
        )

    for index in range(CLEAN_SAMPLES_TO_RELEASE + 1):
        now += 1.0
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 500 + (500 * index), "tx_bytes": 500_000 + (500_000 * index)},
                "fast": {"tx_packets": 2_500 + (100 * index), "tx_bytes": 3_300_000 + (100_000 * index)},
            },
            now=now,
            batch_size=512,
        )
        if index < CLEAN_SAMPLES_TO_RELEASE:
            assert decision.active

    assert decision.changed
    assert not decision.active


def test_service_budget_controller_accumulates_short_status_intervals() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=0.0,
        batch_size=512,
    )

    decision = controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 100, "tx_bytes": 100_000},
            "fast": {"tx_packets": 600, "tx_bytes": 800_000},
        },
        now=MIN_SAMPLE_SECONDS / 2,
        batch_size=512,
    )
    assert not decision.samples

    decision = controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 200, "tx_bytes": 200_000},
            "fast": {"tx_packets": 1_200, "tx_bytes": 1_600_000},
        },
        now=MIN_SAMPLE_SECONDS,
        batch_size=512,
    )
    assert [sample.service for sample in decision.samples] == ["stable", "fast"]


def test_service_budget_controller_tightens_budget_after_sustained_extreme_pressure() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=0.0,
        batch_size=512,
    )

    decision = None
    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE + (BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2)):
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 1_000 * (index + 1), "tx_bytes": 1_000_000 * (index + 1)},
            },
            now=float(index + 1),
            batch_size=512,
        )

    assert decision is not None
    assert decision.active
    assert decision.byte_budget_overrides == {"fast": BULK_BYTE_BUDGET_MIN_EQUIVALENT_PACKETS * 1_000}


def test_service_budget_controller_loosen_budget_back_to_baseline_when_pressure_is_mild() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=0.0,
        batch_size=512,
    )

    now = 0.0
    decision = None
    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE + (BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2)):
        now += 1.0
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 1_000 * (index + 1), "tx_bytes": 1_000_000 * (index + 1)},
            },
            now=now,
            batch_size=512,
        )
    assert decision is not None
    assert decision.byte_budget_overrides == {"fast": BULK_BYTE_BUDGET_MIN_EQUIVALENT_PACKETS * 1_000}

    strong_pressure_samples = PRESSURE_SAMPLES_TO_ACTIVATE + (BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2)
    base_stable_packets = 100 * strong_pressure_samples
    base_fast_packets = 1_000 * strong_pressure_samples
    base_stable_bytes = 100_000 * strong_pressure_samples
    base_fast_bytes = 1_000_000 * strong_pressure_samples
    for index in range(BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2):
        now += 1.0
        decision = controller.update(
            runtime.services,
            {
                "stable": {
                    "tx_packets": base_stable_packets + 100 * (index + 1),
                    "tx_bytes": base_stable_bytes + 100_000 * (index + 1),
                },
                "fast": {
                    "tx_packets": base_fast_packets + 260 * (index + 1),
                    "tx_bytes": base_fast_bytes + 260_000 * (index + 1),
                },
            },
            now=now,
            batch_size=512,
        )

    assert decision is not None
    assert decision.active
    assert decision.byte_budget_overrides == {"fast": BULK_BYTE_BUDGET_EQUIVALENT_PACKETS * 1_000}


def test_service_budget_controller_holds_budget_when_protected_service_outcome_is_bad() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=0.0,
        batch_size=512,
    )

    decision = None
    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE + (BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2)):
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 1_000 * (index + 1), "tx_bytes": 1_000_000 * (index + 1)},
            },
            now=float(index + 1),
            batch_size=512,
            outcome=ServiceOutcomeSnapshot.from_mapping({"stable": "tcp retransmits increased"}),
        )

    assert decision is not None
    assert decision.active
    assert decision.packet_budget_overrides == {
        "stable": PROTECTED_OUTCOME_PACKET_BUDGET,
        "fast": BULK_QUANTUM_CEILING_PACKETS,
    }
    assert decision.byte_budget_overrides == {"fast": PROTECTED_OUTCOME_BYTE_BUDGET_EQUIVALENT_PACKETS * 1_000}
    assert "tcp retransmits increased" in decision.reason


def test_service_budget_controller_keeps_tight_budget_when_protected_outcome_degrades() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=0.0,
        batch_size=512,
    )

    now = 0.0
    decision = None
    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE + (BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2)):
        now += 1.0
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 1_000 * (index + 1), "tx_bytes": 1_000_000 * (index + 1)},
            },
            now=now,
            batch_size=512,
        )
    assert decision is not None
    assert decision.byte_budget_overrides == {"fast": BULK_BYTE_BUDGET_MIN_EQUIVALENT_PACKETS * 1_000}

    strong_pressure_samples = PRESSURE_SAMPLES_TO_ACTIVATE + (BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2)
    base_stable_packets = 100 * strong_pressure_samples
    base_fast_packets = 1_000 * strong_pressure_samples
    base_stable_bytes = 100_000 * strong_pressure_samples
    base_fast_bytes = 1_000_000 * strong_pressure_samples
    for index in range(BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2):
        now += 1.0
        decision = controller.update(
            runtime.services,
            {
                "stable": {
                    "tx_packets": base_stable_packets + 100 * (index + 1),
                    "tx_bytes": base_stable_bytes + 100_000 * (index + 1),
                },
                "fast": {
                    "tx_packets": base_fast_packets + 1_000 * (index + 1),
                    "tx_bytes": base_fast_bytes + 1_000_000 * (index + 1),
                },
            },
            now=now,
            batch_size=512,
            outcome=ServiceOutcomeSnapshot.from_mapping({"stable": "protected flow got worse"}),
        )

    assert decision is not None
    assert decision.active
    assert decision.packet_budget_overrides == {
        "stable": PROTECTED_OUTCOME_PACKET_BUDGET,
        "fast": BULK_QUANTUM_CEILING_PACKETS,
    }
    assert decision.byte_budget_overrides == {"fast": PROTECTED_OUTCOME_BYTE_BUDGET_EQUIVALENT_PACKETS * 1_000}
    assert "protected flow got worse" in decision.reason


def test_service_budget_controller_activates_on_protected_outcome_with_bulk_traffic() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=0.0,
        batch_size=512,
    )

    decision = controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 1_000, "tx_bytes": 1_000_000},
            "fast": {"tx_packets": 1_000, "tx_bytes": 1_000_000},
        },
        now=1.0,
        batch_size=512,
        outcome=ServiceOutcomeSnapshot.from_mapping({"stable": "live tcp retransmits increased"}),
    )

    assert decision.active
    assert decision.packet_budget_overrides == {
        "stable": PROTECTED_OUTCOME_PACKET_BUDGET,
        "fast": BULK_QUANTUM_CEILING_PACKETS,
    }
    assert decision.byte_budget_overrides == {"fast": PROTECTED_OUTCOME_BYTE_BUDGET_EQUIVALENT_PACKETS * 1_000}
    assert "protected service degraded" in decision.reason


def test_service_budget_controller_ignores_bulk_service_outcome_for_protected_budgeting() -> None:
    runtime = _dual_service_runtime()
    controller = ServiceBudgetController()
    controller.update(
        runtime.services,
        {
            "stable": {"tx_packets": 0, "tx_bytes": 0},
            "fast": {"tx_packets": 0, "tx_bytes": 0},
        },
        now=0.0,
        batch_size=512,
    )

    decision = None
    for index in range(PRESSURE_SAMPLES_TO_ACTIVATE + (BULK_BYTE_BUDGET_ADJUST_SAMPLES * 2)):
        decision = controller.update(
            runtime.services,
            {
                "stable": {"tx_packets": 100 * (index + 1), "tx_bytes": 100_000 * (index + 1)},
                "fast": {"tx_packets": 1_000 * (index + 1), "tx_bytes": 1_000_000 * (index + 1)},
            },
            now=float(index + 1),
            batch_size=512,
            outcome=ServiceOutcomeSnapshot.from_mapping({"fast": "bulk service loss"}),
        )

    assert decision is not None
    assert decision.active
    assert decision.byte_budget_overrides == {"fast": BULK_BYTE_BUDGET_MIN_EQUIVALENT_PACKETS * 1_000}


def test_service_poll_plan_uses_overrides_without_forcing_default_plan() -> None:
    runtime = _dual_service_runtime()

    assert not uses_service_drain_plan(runtime.services)
    assert uses_service_drain_plan(runtime.services, {"fast": 128})
    assert uses_service_budget_plan(runtime.services, byte_budget_overrides={"fast": 128_000})
    assert service_poll_plan(runtime.services, 512, {"fast": 128}) == [
        ("stable", 512),
        ("stable", 512),
        ("stable", 512),
        ("fast", 128),
    ]
    assert service_budget_plan(runtime.services, 512, {"fast": 128}, {"fast": 128_000}) == [
        ("stable", 512, 0),
        ("stable", 512, 0),
        ("stable", 512, 0),
        ("fast", 128, 128_000),
    ]
