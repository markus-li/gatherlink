"""Python-owned congestion fairness policy helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from gatherlink.config.models import SchedulerCongestionPolicy

CONGESTION_MODERATE_LOSS_PPM = 20_000
CONGESTION_HIGH_LOSS_PPM = 80_000
CONGESTION_MODERATE_QUEUE_AGE_US = 25_000
CONGESTION_HIGH_QUEUE_AGE_US = 100_000
CONGESTION_MODERATE_QUEUE_PACKETS = 256
CONGESTION_HIGH_QUEUE_PACKETS = 1024
CONGESTION_MODERATE_SEND_FAILURES = 8
CONGESTION_HIGH_SEND_FAILURES = 64
CONGESTION_POLICY_FACTORS: dict[SchedulerCongestionPolicy, tuple[int, int, int]] = {
    "off": (100, 100, 100),
    "conservative": (90, 75, 55),
    "adaptive": (100, 85, 65),
    "volatile": (85, 70, 50),
}
CongestionPressureLevel = Literal[0, 1, 2]


@dataclass(frozen=True)
class CongestionFairnessDecision:
    """One path's self-limiting decision compiled from Python-visible pressure."""

    pressure_level: CongestionPressureLevel
    pacing_budget_bps: int
    reason: str


@dataclass
class CongestionFairnessPathState:
    """Sticky per-path state used to avoid flapping inferred pacing limits."""

    pressure_level: CongestionPressureLevel = 0
    pacing_budget_bps: int = 0
    reason: str = "clean"
    pressure_windows: int = 0
    recovery_windows: int = 0


@dataclass
class CongestionFairnessController:
    """
    Stabilize inferred congestion fairness decisions across reapply windows.

    Python owns this because the question "when should Gatherlink back off or
    recover?" is policy. Rust only receives the compiled primitive. Pressure is
    applied quickly, while recovery requires repeated cleaner windows so one
    lucky sample does not immediately remove a useful self-limit.
    """

    pressure_windows_required: int = 1
    recovery_windows_required: int = 3
    _paths: dict[str, CongestionFairnessPathState] = field(default_factory=dict)

    def stabilize(self, path_name: str, decision: CongestionFairnessDecision) -> CongestionFairnessDecision:
        """Return the stable decision for one path and remember it for the next pass."""
        state = self._paths.setdefault(path_name, CongestionFairnessPathState())
        if decision.pressure_level > state.pressure_level:
            state.recovery_windows = 0
            state.pressure_windows += 1
            if state.pressure_windows >= max(1, self.pressure_windows_required):
                self._remember(state, decision)
                return decision
            return CongestionFairnessDecision(
                pressure_level=state.pressure_level,
                pacing_budget_bps=state.pacing_budget_bps,
                reason="awaiting_pressure_confirmation",
            )

        if decision.pressure_level == state.pressure_level:
            state.pressure_windows = max(state.pressure_windows, self.pressure_windows_required)
            if decision.pressure_level > 0:
                state.recovery_windows = 0
                self._remember(state, decision)
            else:
                state.recovery_windows += 1
                self._remember(state, decision)
            return decision

        state.pressure_windows = 0
        state.recovery_windows += 1
        if state.recovery_windows < max(1, self.recovery_windows_required):
            return CongestionFairnessDecision(
                pressure_level=state.pressure_level,
                pacing_budget_bps=state.pacing_budget_bps,
                reason="held_for_recovery_hysteresis",
            )

        self._remember(state, decision)
        return decision

    @staticmethod
    def _remember(state: CongestionFairnessPathState, decision: CongestionFairnessDecision) -> None:
        """Persist the stable decision without leaking controller state to Rust."""
        state.pressure_level = decision.pressure_level
        state.pacing_budget_bps = decision.pacing_budget_bps
        state.reason = decision.reason


def compile_congestion_fairness(
    metrics: object,
    *,
    policy: SchedulerCongestionPolicy,
    capacity_bps: int | None,
) -> CongestionFairnessDecision:
    """
    Compile a pressure decision into a primitive pacing budget.

    A zero pacing budget means Rust bypasses pacing entirely. This keeps the
    fast path free of policy branches when Python has no reason to self-limit.
    """
    if policy == "off" or capacity_bps is None or capacity_bps <= 0:
        return CongestionFairnessDecision(pressure_level=0, pacing_budget_bps=0, reason="disabled")
    pressure_level = congestion_pressure_level(metrics)
    if pressure_level <= 0:
        return CongestionFairnessDecision(pressure_level=0, pacing_budget_bps=0, reason="clean")
    factor_percent = CONGESTION_POLICY_FACTORS[policy][pressure_level]
    return CongestionFairnessDecision(
        pressure_level=pressure_level,
        pacing_budget_bps=max(1, capacity_bps * factor_percent // 100),
        reason="high_pressure" if pressure_level == 2 else "moderate_pressure",
    )


def congestion_pressure_level(metrics: object) -> CongestionPressureLevel:
    """
    Return 0 for clean, 1 for moderate pressure, or 2 for high pressure.

    The inputs are normalized scheduler metrics. This helper avoids any
    semantic interpretation in Rust while keeping pressure decisions
    inspectable and unit-testable in Python.
    """
    loss_ppm = int(getattr(metrics, "loss_ppm", 0) or 0)
    queue_age_us = int(getattr(metrics, "queue_oldest_age_us", 0) or 0)
    queue_packets = int(getattr(metrics, "queue_depth_packets", 0) or 0)
    send_failures = int(getattr(metrics, "send_failures", 0) or 0)
    local_drops = int(getattr(metrics, "local_drops", 0) or 0)
    if (
        loss_ppm >= CONGESTION_HIGH_LOSS_PPM
        or queue_age_us >= CONGESTION_HIGH_QUEUE_AGE_US
        or queue_packets >= CONGESTION_HIGH_QUEUE_PACKETS
        or send_failures >= CONGESTION_HIGH_SEND_FAILURES
        or local_drops > 0
    ):
        return 2
    if (
        loss_ppm >= CONGESTION_MODERATE_LOSS_PPM
        or queue_age_us >= CONGESTION_MODERATE_QUEUE_AGE_US
        or queue_packets >= CONGESTION_MODERATE_QUEUE_PACKETS
        or send_failures >= CONGESTION_MODERATE_SEND_FAILURES
    ):
        return 1
    return 0
